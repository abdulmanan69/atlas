"""Knowledge base: documents agents can consult during calls.

ATLAS validates the file (type by content, size), hands the bytes to the voice service's
storage, and asks the voice service to index it. Parsing and chunking happen in an
external document-processing service operated with the voice service (see
docs/knowledge.md); the document's text therefore leaves ATLAS. Status is always read
from the voice service; nothing is stored here.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.config import get_settings
from app.core.exceptions import (
    AppError,
    IntegrationUnavailableError,
    NotFoundError,
    ValidationFailedError,
)
from app.integrations.voice_runtime import knowledge_api as api
from app.integrations.voice_runtime.client import RuntimeRejectedError
from app.schemas.knowledge import (
    DocumentOut,
    DocumentStatus,
    SearchHit,
    SearchRequest,
    SearchResult,
)
from app.services import ai_settings, audit
from app.services.runtime_accounts import runtime_client

log = logging.getLogger(__name__)

MIME_BY_EXTENSION: dict[str, str] = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
ACCEPTED_TYPES = "PDF, Word (.docx), text, Markdown or CSV"
_STATUSES: frozenset[str] = frozenset({"pending", "processing", "completed", "failed"})
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STEM = 100
EMBEDDINGS_REQUIRED = (
    "Choose an embeddings model under AI providers before using the knowledge base."
)
DISABLED = "The knowledge base is not enabled on this deployment."


class FileTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


def require_enabled() -> None:
    """Endpoint dependency: the whole feature answers 404 when switched off."""
    if not get_settings().knowledge_enabled:
        raise NotFoundError(DISABLED)


def max_upload_bytes() -> int:
    return get_settings().knowledge_max_upload_bytes


# ---------------------------------------------------------------- file validation


def safe_filename(raw: str | None) -> str:
    """A name safe to put in a storage key: no paths, no odd characters, extension kept."""
    name = PurePosixPath((raw or "").replace("\\", "/")).name
    stem, ext = PurePosixPath(name).stem, PurePosixPath(name).suffix.lower()
    if ext not in MIME_BY_EXTENSION:
        raise ValidationFailedError(f"Unsupported file type. Upload a {ACCEPTED_TYPES} file.")
    stem = _UNSAFE.sub("_", stem).strip("._")[:_MAX_STEM] or "document"
    return f"{stem}{ext}"


def sniff_type(filename: str, data: bytes) -> str:
    """The MIME type for ``filename``, after checking the bytes really are that type."""
    ext = PurePosixPath(filename).suffix.lower()
    mime = MIME_BY_EXTENSION[ext]
    if not data:
        raise ValidationFailedError("The file is empty")
    if ext == ".pdf":
        ok = data.startswith(b"%PDF-")
    elif ext == ".docx":
        ok = _is_docx(data)
    else:
        ok = _is_text(data)
    if not ok:
        raise ValidationFailedError(
            f"The file does not look like a {ext[1:].upper()} file. Upload a {ACCEPTED_TYPES} file."
        )
    return mime


def _is_docx(data: bytes) -> bool:
    if not data.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return "word/document.xml" in archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def _is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


async def read_upload(file: UploadFile) -> tuple[str, bytes]:
    """Read at most the configured limit (+1 byte to detect overflow) and validate."""
    limit = max_upload_bytes()
    name = safe_filename(file.filename)
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise FileTooLargeError(f"Files must be {limit // (1024 * 1024)} MB or smaller")
    return name, data


# ---------------------------------------------------------------- documents


def _status(raw: str) -> DocumentStatus:
    if raw in _STATUSES:
        return raw  # type: ignore[return-value]
    return "pending"


def _to_out(doc: api.Document) -> DocumentOut:
    status = _status(doc.processing_status)
    return DocumentOut(
        id=doc.document_uuid,
        filename=doc.filename,
        status=status,
        error=doc.processing_error if status == "failed" else None,
        size_bytes=doc.file_size_bytes,
        mime_type=doc.mime_type,
        chunk_count=doc.total_chunks,
        ready=doc.has_live_content,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


async def _require_embeddings(db: AsyncSession, ctx: TenantContext) -> None:
    selection = await ai_settings.get_selection(db, ctx)
    if selection.embeddings is None:
        raise ValidationFailedError(EMBEDDINGS_REQUIRED)


async def _put_bytes(upload_url: str, data: bytes, mime: str) -> None:
    """PUT the file to the signed storage URL the voice service issued.

    The URL comes from the trusted voice service, not from the request. Redirects are
    not followed and the URL (which carries a signature) is never logged.
    """
    parts = urlsplit(upload_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        log.error("knowledge_upload_url_invalid", extra={"scheme": parts.scheme})
        raise IntegrationUnavailableError("Document storage is not available. Try again shortly.")
    timeout = get_settings().knowledge_upload_timeout_seconds
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5.0), follow_redirects=False
        ) as http:
            resp = await http.put(upload_url, content=data, headers={"Content-Type": mime})
    except httpx.HTTPError as exc:
        log.warning(
            "knowledge_upload_failed",
            extra={"host": parts.hostname, "error_type": type(exc).__name__},
        )
        raise IntegrationUnavailableError(
            "Document storage is not available. Try again shortly."
        ) from exc
    if not resp.is_success:
        log.warning(
            "knowledge_upload_rejected", extra={"host": parts.hostname, "status": resp.status_code}
        )
        raise IntegrationUnavailableError("Document storage did not accept the file. Try again.")


async def upload(
    db: AsyncSession, ctx: TenantContext, *, filename: str, data: bytes
) -> DocumentOut:
    await _require_embeddings(db, ctx)
    mime = sniff_type(filename, data)
    async with runtime_client(db, ctx.org_id) as client:
        target = await api.request_upload(client, filename=filename, mime_type=mime)
        await _put_bytes(target.upload_url, data, mime)
        doc = await api.process_document(
            client, document_uuid=target.document_uuid, s3_key=target.s3_key
        )
    await audit.record(
        db,
        action="knowledge.document_uploaded",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="knowledge_document",
        resource_id=doc.document_uuid,
        changes={"filename": filename, "size_bytes": len(data), "mime_type": mime},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return _to_out(doc)


async def list_documents(db: AsyncSession, ctx: TenantContext) -> list[DocumentOut]:
    async with runtime_client(db, ctx.org_id) as client:
        docs = await api.list_documents(client)
    return [_to_out(d) for d in docs if d.is_active]


async def get_document(db: AsyncSession, ctx: TenantContext, document_id: str) -> DocumentOut:
    async with runtime_client(db, ctx.org_id) as client:
        try:
            doc = await api.get_document(client, document_id)
        except RuntimeRejectedError as exc:
            if exc.runtime_status == 404:
                raise NotFoundError("Document not found") from exc
            raise
    if not doc.is_active:
        raise NotFoundError("Document not found")
    return _to_out(doc)


async def delete_document(db: AsyncSession, ctx: TenantContext, document_id: str) -> None:
    async with runtime_client(db, ctx.org_id) as client:
        try:
            await api.delete_document(client, document_id)
        except RuntimeRejectedError as exc:
            if exc.runtime_status == 404:
                raise NotFoundError("Document not found") from exc
            raise
    await audit.record(
        db,
        action="knowledge.document_deleted",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="knowledge_document",
        resource_id=document_id,
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )


async def search(db: AsyncSession, ctx: TenantContext, data: SearchRequest) -> SearchResult:
    await _require_embeddings(db, ctx)
    async with runtime_client(db, ctx.org_id) as client:
        chunks = await api.search(
            client, query=data.query, limit=data.limit, document_uuids=data.document_ids
        )
    return SearchResult(
        query=data.query,
        results=[
            SearchHit(
                document_id=c.document_uuid,
                filename=c.filename,
                text=c.chunk_text,
                chunk_index=c.chunk_index,
                score=c.similarity,
            )
            for c in chunks
        ],
    )
