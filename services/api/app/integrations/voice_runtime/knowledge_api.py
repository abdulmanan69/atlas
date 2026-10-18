"""Runtime calls for the knowledge base.

Verified against the runtime source (routes/knowledge_base.py, schemas/knowledge_base.py).
Upload is three steps: ask for a signed upload URL, PUT the bytes there, then ask the
runtime to process the stored object. Parsing and chunking happen in the runtime's
external document-processing service (tasks/knowledge_base_processing.py).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.integrations.voice_runtime.client import VoiceRuntimeClient

KB = "/knowledge-base"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")


class UploadTarget(_Payload):
    """``POST /knowledge-base/upload-url`` -> signed PUT URL valid for 30 minutes."""

    upload_url: str
    document_uuid: str
    s3_key: str


class Document(_Payload):
    """DocumentResponseSchema. ``processing_status``: pending | processing | completed | failed."""

    document_uuid: str
    filename: str
    file_size_bytes: int = 0
    mime_type: str = ""
    processing_status: str
    processing_error: str | None = None
    total_chunks: int = 0
    retrieval_mode: str = "chunked"
    is_active: bool = True
    has_live_content: bool = False
    created_at: str | None = None
    updated_at: str | None = None


class Chunk(_Payload):
    """ChunkResponseSchema."""

    chunk_text: str
    chunk_index: int
    filename: str
    document_uuid: str
    similarity: float


async def request_upload(
    client: VoiceRuntimeClient, *, filename: str, mime_type: str
) -> UploadTarget:
    body = await client._call(
        "POST", f"{KB}/upload-url", json={"filename": filename, "mime_type": mime_type}
    )
    return UploadTarget.model_validate(body)


async def process_document(
    client: VoiceRuntimeClient, *, document_uuid: str, s3_key: str
) -> Document:
    """``POST /knowledge-base/process-document`` creates the record and queues processing."""
    body = await client._call(
        "POST",
        f"{KB}/process-document",
        json={"document_uuid": document_uuid, "s3_key": s3_key, "retrieval_mode": "chunked"},
    )
    return Document.model_validate(body)


async def list_documents(client: VoiceRuntimeClient, *, limit: int = 100) -> list[Document]:
    body = await client._call("GET", f"{KB}/documents", params={"limit": limit, "offset": 0})
    return [Document.model_validate(d) for d in body.get("documents", [])]


async def get_document(client: VoiceRuntimeClient, document_uuid: str) -> Document:
    body = await client._call("GET", f"{KB}/documents/{document_uuid}")
    return Document.model_validate(body)


async def delete_document(client: VoiceRuntimeClient, document_uuid: str) -> None:
    await client._call("DELETE", f"{KB}/documents/{document_uuid}")


async def search(
    client: VoiceRuntimeClient,
    *,
    query: str,
    limit: int,
    document_uuids: list[str] | None,
) -> list[Chunk]:
    payload: dict[str, Any] = {"query": query, "limit": limit}
    if document_uuids:
        payload["document_uuids"] = document_uuids
    body = await client._call("POST", f"{KB}/search", json=payload)
    return [Chunk.model_validate(c) for c in body.get("chunks", [])]
