"""Shared API contracts."""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Page(APIModel, Generic[T]):
    items: list[T]
    total: int = Field(description="Total matching records across all pages")
    page: int
    page_size: int


class ErrorDetail(APIModel):
    code: str = Field(examples=["not_found"])
    message: str
    request_id: str | None = None
    details: Any | None = None


class ErrorResponse(APIModel):
    error: ErrorDetail


class Message(APIModel):
    message: str


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid credentials"},
    403: {"model": ErrorResponse, "description": "Authenticated but not permitted"},
    404: {"model": ErrorResponse, "description": "Resource not found in this organization"},
    422: {"model": ErrorResponse, "description": "Validation failed"},
}
