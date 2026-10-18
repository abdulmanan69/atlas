"""Read call artifacts (transcripts, recordings) from the storage the runtime points to.

The runtime answers artifact requests with a redirect to object storage (its public
download route, see `VoiceRuntimeClient.artifact_location`). ATLAS resolves that
location with the tenant's runtime credentials and then reads the object with a bare
HTTP client, so no credential leaves the platform and nothing about the storage layout
reaches tenants: audio and text are always proxied through ATLAS.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.core.exceptions import NotFoundError
from app.integrations.voice_runtime.client import RuntimeUnavailableError

log = logging.getLogger(__name__)

CHUNK_BYTES = 64 * 1024
# Keys end with the format the runtime wrote (recordings/{run}.wav, transcripts/{run}.txt);
# used when storage does not declare a specific content type.
_TYPES_BY_SUFFIX = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
}
_GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream"}


class ArtifactUnavailableError(RuntimeUnavailableError):
    def __init__(self) -> None:
        super().__init__("Call recordings and transcripts are temporarily unavailable.")


def content_type_for(url: str, declared: str | None) -> str:
    """A specific declared type wins; otherwise infer from the object key's suffix."""
    if declared and declared.split(";")[0].strip().lower() not in _GENERIC_TYPES:
        return declared
    path = urlsplit(url).path.lower()
    for suffix, content_type in _TYPES_BY_SUFFIX.items():
        if path.endswith(suffix):
            return content_type
    return declared or "application/octet-stream"


@dataclass(slots=True)
class ArtifactStream:
    content_type: str
    content_length: int | None
    chunks: AsyncGenerator[bytes, None]


async def open_artifact(url: str, *, request_timeout: float) -> ArtifactStream:
    """Start streaming an artifact. The caller must consume or close `chunks`."""
    if urlsplit(url).scheme not in ("http", "https"):
        log.warning("artifact_location_unusable")
        raise ArtifactUnavailableError
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(request_timeout, connect=5.0), follow_redirects=False
    )
    try:
        resp = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        log.warning("artifact_unreachable", extra={"error_type": type(exc).__name__})
        raise ArtifactUnavailableError from exc
    if resp.status_code != 200:
        status = resp.status_code
        await resp.aclose()
        await client.aclose()
        if status == 404:
            raise NotFoundError("This call artifact is no longer available")
        log.warning("artifact_fetch_failed", extra={"status": status})
        raise ArtifactUnavailableError

    async def _chunks() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in resp.aiter_bytes(CHUNK_BYTES):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    declared_length = resp.headers.get("content-length")
    return ArtifactStream(
        content_type=content_type_for(url, resp.headers.get("content-type")),
        content_length=int(declared_length)
        if declared_length and declared_length.isdigit()
        else None,
        chunks=_chunks(),
    )


async def fetch_artifact(url: str, *, max_bytes: int, request_timeout: float) -> tuple[bytes, bool]:
    """Read an artifact into memory, stopping at `max_bytes`. Returns (content, truncated)."""
    stream = await open_artifact(url, request_timeout=request_timeout)
    buffer = bytearray()
    truncated = False
    try:
        async for chunk in stream.chunks:
            buffer.extend(chunk)
            if len(buffer) >= max_bytes:
                truncated = len(buffer) > max_bytes
                del buffer[max_bytes:]
                break
    finally:
        await stream.chunks.aclose()
    return bytes(buffer), truncated
