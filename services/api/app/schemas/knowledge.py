"""Knowledge base: documents agents can consult during calls."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.common import APIModel

DocumentStatus = Literal["pending", "processing", "completed", "failed"]


class DocumentOut(APIModel):
    id: str = Field(description="Opaque document id; attach it to flow steps")
    filename: str
    status: DocumentStatus
    error: str | None = Field(default=None, description="Why processing failed, if it did")
    size_bytes: int = 0
    mime_type: str = ""
    chunk_count: int = 0
    ready: bool = Field(default=False, description="Agents can consult this document now")
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SearchRequest(APIModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=20)
    document_ids: list[str] | None = Field(
        default=None, max_length=50, description="Restrict to these documents"
    )


class SearchHit(APIModel):
    document_id: str
    filename: str
    text: str
    chunk_index: int
    score: float = Field(description="Similarity, higher is closer")


class SearchResult(APIModel):
    query: str
    results: list[SearchHit]
