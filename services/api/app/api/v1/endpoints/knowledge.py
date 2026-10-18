"""Knowledge base: documents agents consult during calls.

Parsing and chunking of uploaded documents run in an external document-processing
service operated with the voice service; the document's text leaves ATLAS for that.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile, status

from app.api.v1.deps import DB, AgentsManage, AgentsRead
from app.schemas.common import ERROR_RESPONSES
from app.schemas.knowledge import DocumentOut, SearchRequest, SearchResult
from app.services import knowledge as service

PROCESSING_NOTE = (
    "Uploaded documents are parsed and split into passages by an external "
    "document-processing service before they are indexed; their text leaves this platform "
    "for that step."
)

router = APIRouter(
    prefix="/knowledge",
    tags=["Knowledge"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(service.require_enabled)],
)


@router.get(
    "/documents",
    response_model=list[DocumentOut],
    summary="Documents and their processing status",
)
async def list_documents(ctx: AgentsRead, db: DB) -> list[DocumentOut]:
    return await service.list_documents(db, ctx)


@router.post(
    "/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document (PDF, Word, text, Markdown or CSV)",
    description=(
        "Requires an embeddings model under AI providers. The file type is checked from its "
        "content, not just its name; see the documentation for the size limit. " + PROCESSING_NOTE
    ),
    responses={413: {"description": "File is larger than the configured limit"}},
)
async def upload_document(
    file: Annotated[UploadFile, File(description="PDF, .docx, .txt, .md or .csv")],
    ctx: AgentsManage,
    db: DB,
) -> DocumentOut:
    filename, data = await service.read_upload(file)
    return await service.upload(db, ctx, filename=filename, data=data)


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(document_id: uuid.UUID, ctx: AgentsRead, db: DB) -> DocumentOut:
    return await service.get_document(db, ctx, str(document_id))


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: uuid.UUID, ctx: AgentsManage, db: DB) -> None:
    await service.delete_document(db, ctx, str(document_id))


@router.post(
    "/search",
    response_model=SearchResult,
    summary="Find the passages an agent would retrieve for a question",
    description="Semantic search over processed documents. " + PROCESSING_NOTE,
)
async def search(data: SearchRequest, ctx: AgentsRead, db: DB) -> SearchResult:
    return await service.search(db, ctx, data)
