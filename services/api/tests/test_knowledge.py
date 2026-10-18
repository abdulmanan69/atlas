"""Knowledge base against a contract-faithful fake voice runtime and its object storage."""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.audit import AuditLog
from tests.conftest import Account, invite_and_join, signup
from tests.fake_runtime import VALID_KEY, FakeRuntime, Org, _json

STORAGE = "http://kb-storage.test"
TS = "2026-10-18T10:00:00+00:00"
KB = "/knowledge-base"


class KnowledgeRuntime(FakeRuntime):
    """Adds routes/knowledge_base.py plus the signed-URL object storage it hands out."""

    def __init__(self) -> None:
        super().__init__()
        self.docs: dict[str, dict[str, Any]] = {}
        self.blobs: dict[str, bytes] = {}
        self.blob_types: dict[str, str | None] = {}
        # What the background processing "did" by the time the document is read back.
        self.outcome: tuple[str, str | None] = ("completed", None)
        self.storage_down = False

    def storage(self, request: httpx.Request) -> httpx.Response:
        if self.storage_down:
            return httpx.Response(500)
        if request.method != "PUT" or "X-Amz-Signature" not in str(request.url.query):
            return httpx.Response(403)
        key = request.url.path.lstrip("/")
        self.blobs[key] = request.content
        self.blob_types[key] = request.headers.get("content-type")
        return httpx.Response(200)

    def _authed(
        self, org: Org, m: str, path: str, body: Any, request: httpx.Request
    ) -> httpx.Response:
        if path.startswith(KB):
            return self._kb(org, m, path, body)
        return super()._authed(org, m, path, body, request)

    def _public_doc(self, doc: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in doc.items() if not k.startswith("_")}

    def _kb(self, org: Org, m: str, path: str, body: Any) -> httpx.Response:
        if m == "POST" and path == f"{KB}/upload-url":
            duid = str(uuid.uuid4())
            key = f"knowledge_base/{org.id}/{duid}/{body['filename']}"
            return _json(
                200,
                {
                    "upload_url": f"{STORAGE}/{key}?X-Amz-Signature=abc",
                    "document_uuid": duid,
                    "s3_key": key,
                },
            )
        if m == "POST" and path == f"{KB}/process-document":
            key = body["s3_key"]
            blob = self.blobs.get(key)
            status, error = self.outcome if blob is not None else ("failed", "download failed")
            doc = {
                "_org": org.id,
                "id": next(self._ids),
                "document_uuid": body["document_uuid"],
                "filename": key.split("/")[-1],
                "file_size_bytes": len(blob or b""),
                "file_hash": "h",
                "mime_type": self.blob_types.get(key) or "application/octet-stream",
                "processing_status": status,
                "processing_error": error,
                "total_chunks": 3 if status == "completed" else 0,
                "retrieval_mode": body.get("retrieval_mode", "chunked"),
                "custom_metadata": {"s3_key": key},
                "docling_metadata": {},
                "source_url": None,
                "created_at": TS,
                "updated_at": TS,
                "organization_id": org.id,
                "created_by": 1,
                "is_active": True,
                "has_live_content": status == "completed",
            }
            self.docs[doc["document_uuid"]] = doc
            # The real route answers before the background task runs.
            return _json(
                200,
                {
                    **self._public_doc(doc),
                    "processing_status": "pending",
                    "processing_error": None,
                    "total_chunks": 0,
                    "has_live_content": False,
                },
            )
        if m == "GET" and path == f"{KB}/documents":
            docs = [
                self._public_doc(d)
                for d in self.docs.values()
                if d["_org"] == org.id and d["is_active"]
            ]
            return _json(200, {"documents": docs, "total": len(docs), "limit": 100, "offset": 0})
        if mm := re.fullmatch(rf"{KB}/documents/([\w-]+)", path):
            doc = self.docs.get(mm[1])
            if doc is None or doc["_org"] != org.id or not doc["is_active"]:
                return _json(404, {"detail": "Document not found"})
            if m == "DELETE":
                doc["is_active"] = False
                return _json(200, {"success": True, "message": "Document deleted successfully"})
            return _json(200, self._public_doc(doc))
        if m == "POST" and path == f"{KB}/search":
            query = body["query"].lower()
            wanted = body.get("document_uuids")
            hits = []
            for d in self.docs.values():
                if d["_org"] != org.id or not d["has_live_content"]:
                    continue
                if wanted and d["document_uuid"] not in wanted:
                    continue
                text = self.blobs[d["custom_metadata"]["s3_key"]].decode("utf-8", "ignore")
                if query in text.lower():
                    hits.append(
                        {
                            "id": d["id"] * 10,
                            "document_id": d["id"],
                            "chunk_text": text[:80],
                            "contextualized_text": None,
                            "chunk_index": 0,
                            "chunk_metadata": {},
                            "filename": d["filename"],
                            "document_uuid": d["document_uuid"],
                            "similarity": 0.91,
                        }
                    )
            hits = hits[: body.get("limit", 5)]
            return _json(200, {"chunks": hits, "query": body["query"], "total_results": len(hits)})
        return _json(404, {"detail": "Not Found"})


@pytest.fixture
def runtime() -> Iterator[KnowledgeRuntime]:
    fake = KnowledgeRuntime()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="runtime.test").mock(side_effect=fake.handle)
        router.route(host="kb-storage.test").mock(side_effect=fake.storage)
        yield fake


async def _enable_embeddings(client: httpx.AsyncClient, acct: Account) -> None:
    ids = {}
    for provider in ("openai", "deepgram", "elevenlabs"):
        r = await client.post(
            "/api/v1/ai/connections",
            headers=acct.headers,
            json={"name": provider, "provider": provider, "credentials": {"api_key": VALID_KEY}},
        )
        assert r.status_code == 201, r.text
        ids[provider] = r.json()["id"]
    r = await client.put(
        "/api/v1/ai/models",
        headers=acct.headers,
        json={
            "llm": {"connection_id": ids["openai"], "settings": {"model": "gpt-4.1-mini"}},
            "stt": {"connection_id": ids["deepgram"]},
            "tts": {"connection_id": ids["elevenlabs"]},
            "embeddings": {"connection_id": ids["openai"]},
        },
    )
    assert r.status_code == 200, r.text


async def _upload(
    client: httpx.AsyncClient, acct: Account, name: str, data: bytes, mime: str = "text/plain"
) -> httpx.Response:
    return await client.post(
        "/api/v1/knowledge/documents", headers=acct.headers, files={"file": (name, data, mime)}
    )


def _docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
    return buf.getvalue()


# ------------------------------------------------------------------ tests


async def test_upload_requires_an_embeddings_model(
    client: httpx.AsyncClient, owner: Account, runtime: KnowledgeRuntime
) -> None:
    r = await _upload(client, owner, "faq.txt", b"Opening hours are 9 to 5.")
    assert r.status_code == 422
    assert "embeddings model" in r.json()["error"]["message"]
    assert runtime.count("POST", f"^{KB}/upload-url$") == 0
    r = await client.post(
        "/api/v1/knowledge/search", headers=owner.headers, json={"query": "hours"}
    )
    assert r.status_code == 422 and "embeddings model" in r.json()["error"]["message"]


async def test_upload_round_trip(
    client: httpx.AsyncClient, owner: Account, runtime: KnowledgeRuntime
) -> None:
    await _enable_embeddings(client, owner)
    r = await _upload(client, owner, "../Pricing & FAQ (2026).md", b"# Pricing\nBasic is $10.")
    assert r.status_code == 201, r.text
    body = r.json()
    uuid.UUID(body["id"])
    assert body["status"] == "pending" and body["filename"] == "Pricing_FAQ_2026.md"
    assert body["ready"] is False and body["error"] is None
    assert set(body) == {
        "id", "filename", "status", "error", "size_bytes", "mime_type", "chunk_count",
        "ready", "created_at", "updated_at",
    }  # fmt: skip

    asked = runtime.last("POST", f"^{KB}/upload-url$")
    assert asked == {"filename": "Pricing_FAQ_2026.md", "mime_type": "text/markdown"}
    key = next(iter(runtime.blobs))
    assert (
        key.endswith("/Pricing_FAQ_2026.md") and runtime.blobs[key] == b"# Pricing\nBasic is $10."
    )
    assert runtime.blob_types[key] == "text/markdown"
    processed = runtime.last("POST", f"^{KB}/process-document$")
    assert processed == {"document_uuid": body["id"], "s3_key": key, "retrieval_mode": "chunked"}

    got = await client.get(f"/api/v1/knowledge/documents/{body['id']}", headers=owner.headers)
    assert got.status_code == 200
    assert (
        got.json()["status"] == "completed"
        and got.json()["ready"]
        and got.json()["chunk_count"] == 3
    )
    assert got.json()["size_bytes"] == len(b"# Pricing\nBasic is $10.")
    listed = (await client.get("/api/v1/knowledge/documents", headers=owner.headers)).json()
    assert [d["id"] for d in listed] == [body["id"]]
    assert "s3_key" not in str(listed) and "knowledge_base/" not in str(listed)

    found = await client.post(
        "/api/v1/knowledge/search", headers=owner.headers, json={"query": "basic", "limit": 3}
    )
    assert found.status_code == 200, found.text
    hit = found.json()["results"][0]
    assert hit["document_id"] == body["id"] and hit["score"] == 0.91 and "Basic" in hit["text"]
    assert runtime.last("POST", f"^{KB}/search$") == {"query": "basic", "limit": 3}
    nothing = await client.post(
        "/api/v1/knowledge/search",
        headers=owner.headers,
        json={"query": "basic", "document_ids": [str(uuid.uuid4())]},
    )
    assert nothing.json()["results"] == []

    r = await client.delete(f"/api/v1/knowledge/documents/{body['id']}", headers=owner.headers)
    assert r.status_code == 204
    assert (await client.get("/api/v1/knowledge/documents", headers=owner.headers)).json() == []
    assert (
        await client.get(f"/api/v1/knowledge/documents/{body['id']}", headers=owner.headers)
    ).status_code == 404

    async with get_sessionmaker()() as db:
        logs = (
            await db.execute(select(AuditLog).where(AuditLog.action.like("knowledge.%")))
        ).scalars()
        actions = {log.action: log for log in logs}
    assert set(actions) == {"knowledge.document_uploaded", "knowledge.document_deleted"}
    uploaded = actions["knowledge.document_uploaded"]
    assert uploaded.changes["filename"] == "Pricing_FAQ_2026.md"
    assert "Basic is" not in str(uploaded.changes), "content never audited"


async def test_failed_processing_is_reported_with_its_reason(
    client: httpx.AsyncClient, owner: Account, runtime: KnowledgeRuntime
) -> None:
    await _enable_embeddings(client, owner)
    runtime.outcome = ("failed", "This content is identical to 'faq.txt'.")
    r = await _upload(client, owner, "faq2.txt", b"Opening hours are 9 to 5.")
    assert r.status_code == 201
    doc = (
        await client.get(f"/api/v1/knowledge/documents/{r.json()['id']}", headers=owner.headers)
    ).json()
    assert doc["status"] == "failed" and doc["error"].startswith("This content is identical")
    assert doc["ready"] is False


async def test_file_type_is_checked_from_content(
    client: httpx.AsyncClient, owner: Account, runtime: KnowledgeRuntime
) -> None:
    await _enable_embeddings(client, owner)
    rejected = [
        ("setup.exe", b"MZ\x90\x00", "application/octet-stream"),
        ("notes", b"plain", "text/plain"),
        ("report.pdf", b"not a pdf at all", "application/pdf"),
        ("contract.docx", b"PK\x03\x04garbage", "application/vnd.ms-word"),
        ("binary.txt", b"abc\x00def", "text/plain"),
        ("latin.csv", b"caf\xe9", "text/csv"),
        ("empty.md", b"", "text/markdown"),
    ]
    for name, data, mime in rejected:
        r = await _upload(client, owner, name, data, mime)
        assert r.status_code == 422, (name, r.text)
    assert runtime.count("POST", f"^{KB}/upload-url$") == 0

    ok = [
        ("manual.pdf", b"%PDF-1.7\n%...", "application/pdf"),
        ("contract.docx", _docx(), "application/octet-stream"),
        ("prices.csv", b"sku,price\nA,1\n", "text/csv"),
    ]
    for name, data, mime in ok:
        r = await _upload(client, owner, name, data, mime)
        assert r.status_code == 201, (name, r.text)
    sent = {c[2]["mime_type"] for c in runtime.calls if c[1] == f"{KB}/upload-url"}
    assert sent == {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/csv",
    }


async def test_size_limit_is_enforced_before_upload(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: KnowledgeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _enable_embeddings(client, owner)
    monkeypatch.setattr(get_settings(), "knowledge_max_upload_bytes", 1024)
    r = await _upload(client, owner, "big.txt", b"x" * 1025)
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
    assert runtime.count("POST", f"^{KB}/upload-url$") == 0
    assert (await _upload(client, owner, "fits.txt", b"x" * 1024)).status_code == 201


async def test_storage_failure_leaves_no_document_behind(
    client: httpx.AsyncClient, owner: Account, runtime: KnowledgeRuntime
) -> None:
    await _enable_embeddings(client, owner)
    runtime.storage_down = True
    r = await _upload(client, owner, "faq.txt", b"hello")
    assert r.status_code == 503 and r.json()["error"]["code"] == "integration_unavailable"
    assert runtime.count("POST", f"^{KB}/process-document$") == 0
    assert (await client.get("/api/v1/knowledge/documents", headers=owner.headers)).json() == []


async def test_disabled_switch_hides_the_feature(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: KnowledgeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "knowledge_enabled", False)
    r = await client.get("/api/v1/knowledge/documents", headers=owner.headers)
    assert r.status_code == 404 and "not enabled" in r.json()["error"]["message"]
    r = await _upload(client, owner, "faq.txt", b"hello")
    assert r.status_code == 404
    r = await client.post("/api/v1/knowledge/search", headers=owner.headers, json={"query": "x"})
    assert r.status_code == 404
    assert runtime.count("POST", "^/auth/signup$") == 0, "nothing touches the runtime"


async def test_roles_and_tenant_isolation(
    client: httpx.AsyncClient, owner: Account, runtime: KnowledgeRuntime
) -> None:
    await _enable_embeddings(client, owner)
    doc = (await _upload(client, owner, "faq.txt", b"Opening hours are 9 to 5.")).json()

    viewer = await invite_and_join(client, owner, "viewer@example.com", "viewer")
    assert (
        await client.get("/api/v1/knowledge/documents", headers=viewer.headers)
    ).status_code == 200
    r = await client.post(
        "/api/v1/knowledge/search", headers=viewer.headers, json={"query": "hours"}
    )
    assert r.status_code == 200 and len(r.json()["results"]) == 1
    assert (await _upload(client, viewer, "x.txt", b"x")).status_code == 403
    assert (
        await client.delete(f"/api/v1/knowledge/documents/{doc['id']}", headers=viewer.headers)
    ).status_code == 403
    manager = await invite_and_join(client, owner, "mgr@example.com", "manager")
    assert (await _upload(client, manager, "x.txt", b"x")).status_code == 201

    other = await signup(client, "other@example.com", "Other Org")
    assert (await client.get("/api/v1/knowledge/documents", headers=other.headers)).json() == []
    assert (
        await client.get(f"/api/v1/knowledge/documents/{doc['id']}", headers=other.headers)
    ).status_code == 404
    assert (
        await client.delete(f"/api/v1/knowledge/documents/{doc['id']}", headers=other.headers)
    ).status_code == 404
    assert (
        await client.get("/api/v1/knowledge/documents/not-a-uuid", headers=owner.headers)
    ).status_code == 422
    assert runtime.docs[doc["id"]]["is_active"]
