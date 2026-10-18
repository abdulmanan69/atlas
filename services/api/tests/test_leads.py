"""Lead CRUD, dedup, search, timeline, tasks, CSV import/export."""

from __future__ import annotations

import httpx

from tests.conftest import Account


async def _create(client: httpx.AsyncClient, acct: Account, **body: object) -> httpx.Response:
    return await client.post(
        "/api/v1/leads", headers=acct.headers, json={"first_name": "Lead", **body}
    )


async def test_phone_is_normalized_and_deduplicated(
    client: httpx.AsyncClient, owner: Account
) -> None:
    r = await _create(client, owner, phone="020 7946 0010", email="Mixed@Example.com")
    # Org default country is US; a UK national-format number is not valid there.
    assert r.status_code == 422
    r = await _create(client, owner, phone="+44 (0)20 7946 0010", email="Mixed@Example.com")
    assert r.status_code == 201, r.text
    lead = r.json()
    assert lead["phone_e164"] == "+442079460010"
    assert lead["email"] == "mixed@example.com"

    dup = await _create(client, owner, phone="+442079460010")
    assert dup.status_code == 409
    assert dup.json()["error"]["details"]["existing_lead_id"] == lead["id"]


async def test_invalid_input_rejected(client: httpx.AsyncClient, owner: Account) -> None:
    assert (await _create(client, owner, phone="12")).status_code == 422
    assert (await _create(client, owner, email="not-an-email")).status_code == 422
    assert (await _create(client, owner, custom_fields={"x": {"nested": 1}})).status_code == 422
    r = await client.post("/api/v1/leads", headers=owner.headers, json={"first_name": ""})
    assert r.status_code == 422


async def test_search_filter_sort_paginate(client: httpx.AsyncClient, owner: Account) -> None:
    for i, (name, company) in enumerate(
        [("Ali", "Northwind"), ("Bilal", "Contoso"), ("Chen", "Northwind Labs")]
    ):
        r = await _create(
            client,
            owner,
            first_name=name,
            company=company,
            phone=f"+44 20 7946 01{i:02d}",
            tags=["Q4", "vip"] if i == 0 else [],
        )
        assert r.status_code == 201
    base = "/api/v1/leads"
    found = (await client.get(base, headers=owner.headers, params={"q": "northwind"})).json()
    assert found["total"] == 2
    tagged = (await client.get(base, headers=owner.headers, params={"tag": "q4"})).json()
    assert [i["first_name"] for i in tagged["items"]] == ["Ali"]
    page = (
        await client.get(
            base, headers=owner.headers, params={"sort": "first_name", "page": 2, "page_size": 2}
        )
    ).json()
    assert page["total"] == 3 and [i["first_name"] for i in page["items"]] == ["Chen"]
    by_phone = (await client.get(base, headers=owner.headers, params={"q": "79460101"})).json()
    assert by_phone["total"] == 1


async def test_stage_change_and_notes_build_timeline(
    client: httpx.AsyncClient, owner: Account
) -> None:
    lead = (await _create(client, owner)).json()
    stages = {
        s["key"]: s["id"]
        for s in (await client.get("/api/v1/pipeline-stages", headers=owner.headers)).json()
    }
    assert lead["stage_id"] == stages["new"]

    r = await client.patch(
        f"/api/v1/leads/{lead['id']}", headers=owner.headers, json={"stage_id": stages["converted"]}
    )
    assert r.status_code == 200
    assert r.json()["qualification_status"] == "qualified"
    await client.post(
        f"/api/v1/leads/{lead['id']}/notes",
        headers=owner.headers,
        json={"body": "Interested in a 3-bed unit\nCall back Friday"},
    )

    timeline = (
        await client.get(f"/api/v1/leads/{lead['id']}/activities", headers=owner.headers)
    ).json()
    types = [a["type"] for a in timeline["items"]]
    assert types[0] == "note" and "stage_change" in types and types[-1] == "system"
    change = next(a for a in timeline["items"] if a["type"] == "stage_change")
    assert change["data"]["from"] == "new" and change["data"]["to"] == "converted"
    assert change["actor_user_id"] == owner.user_id


async def test_revoking_consent_forces_do_not_call(
    client: httpx.AsyncClient, owner: Account
) -> None:
    lead = (await _create(client, owner)).json()
    r = await client.patch(
        f"/api/v1/leads/{lead['id']}", headers=owner.headers, json={"consent_status": "revoked"}
    )
    assert r.json()["do_not_call"] is True


async def test_soft_delete_hides_lead_and_frees_phone(
    client: httpx.AsyncClient, owner: Account
) -> None:
    lead = (await _create(client, owner, phone="+442079460200")).json()
    assert (
        await client.delete(f"/api/v1/leads/{lead['id']}", headers=owner.headers)
    ).status_code == 204
    assert (
        await client.get(f"/api/v1/leads/{lead['id']}", headers=owner.headers)
    ).status_code == 404
    assert (await _create(client, owner, phone="+442079460200")).status_code == 201
    audit = (
        await client.get(
            "/api/v1/audit-logs", headers=owner.headers, params={"action": "lead.deleted"}
        )
    ).json()
    assert audit["total"] == 1


async def test_owner_assignment_must_be_member(client: httpx.AsyncClient, owner: Account) -> None:
    r = await _create(client, owner, owner_user_id="00000000-0000-4000-8000-000000000000")
    assert r.status_code == 422
    assert (await _create(client, owner, owner_user_id=owner.user_id)).status_code == 201


async def test_bulk_tag_and_stage(client: httpx.AsyncClient, owner: Account) -> None:
    ids = [
        (await _create(client, owner, phone=f"+44 20 7946 03{i:02d}")).json()["id"]
        for i in range(3)
    ]
    r = await client.post(
        "/api/v1/leads/bulk",
        headers=owner.headers,
        json={"lead_ids": ids, "action": "add_tags", "tags": ["Campaign-1"]},
    )
    assert r.json() == {"affected": 3}
    listing = (
        await client.get("/api/v1/leads", headers=owner.headers, params={"tag": "campaign-1"})
    ).json()
    assert listing["total"] == 3


async def test_csv_import_dry_run_then_commit(client: httpx.AsyncClient, owner: Account) -> None:
    await _create(client, owner, phone="+442079460400")
    csv_bytes = (
        b"first_name,last_name,phone,email,company,tags\n"
        b"Sara,Ahmed,+442079460401,sara@example.com,Example Co,vip;q4\n"
        b"Dup,Existing,+442079460400,,,\n"
        b"Bad,Phone,123,,,\n"
        b",NoName,+442079460402,,,\n"
        b"Same,File,+442079460401,,,\n"
        b"Omar,Farooq,+44 20 7946 0403,omar@example.com,,\n"
    )
    files = {"file": ("leads.csv", csv_bytes, "text/csv")}

    dry = await client.post("/api/v1/leads/import", headers=owner.headers, files=files)
    report = dry.json()
    assert report == {
        **report,
        "total_rows": 6,
        "created": 0,
        "skipped_duplicates": 2,
        "failed": 2,
        "dry_run": True,
    }
    assert {e["row"] for e in report["errors"]} == {3, 4}
    assert (await client.get("/api/v1/leads", headers=owner.headers)).json()["total"] == 1

    real = await client.post(
        "/api/v1/leads/import", headers=owner.headers, files=files, params={"dry_run": "false"}
    )
    assert real.json()["created"] == 2
    leads = (
        await client.get("/api/v1/leads", headers=owner.headers, params={"source": "csv_import"})
    ).json()
    assert {lead["first_name"] for lead in leads["items"]} == {"Sara", "Omar"}
    sara = next(lead for lead in leads["items"] if lead["first_name"] == "Sara")
    assert sara["tags"] == ["q4", "vip"]


async def test_csv_import_requires_expected_columns(
    client: httpx.AsyncClient, owner: Account
) -> None:
    files = {"file": ("x.csv", b"name,number\nA,1\n", "text/csv")}
    r = await client.post("/api/v1/leads/import", headers=owner.headers, files=files)
    assert r.status_code == 422


async def test_csv_export_neutralizes_formulas(client: httpx.AsyncClient, owner: Account) -> None:
    await _create(
        client, owner, first_name='=HYPERLINK("http://evil")', company="+cmd", phone="+442079460500"
    )
    r = await client.get("/api/v1/leads/export", headers=owner.headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("first_name,last_name,phone")
    assert "'=HYPERLINK" in lines[1] and "'+cmd" in lines[1]


async def test_tasks_lifecycle_logs_to_timeline(client: httpx.AsyncClient, owner: Account) -> None:
    lead = (await _create(client, owner)).json()
    r = await client.post(
        "/api/v1/tasks",
        headers=owner.headers,
        json={"title": "Send brochure", "lead_id": lead["id"], "due_at": "2030-01-01T09:00:00Z"},
    )
    assert r.status_code == 201
    task = r.json()
    assert task["assignee_user_id"] == owner.user_id
    done = await client.patch(
        f"/api/v1/tasks/{task['id']}", headers=owner.headers, json={"status": "done"}
    )
    assert done.json()["completed_at"] is not None
    mine = (
        await client.get(
            "/api/v1/tasks", headers=owner.headers, params={"mine": "true", "status": "done"}
        )
    ).json()
    assert mine["total"] == 1
    timeline = (
        await client.get(f"/api/v1/leads/{lead['id']}/activities", headers=owner.headers)
    ).json()
    summaries = [a["summary"] for a in timeline["items"]]
    assert "Task completed: Send brochure" in summaries


async def test_crm_overview_metrics(client: httpx.AsyncClient, owner: Account) -> None:
    empty = (await client.get("/api/v1/analytics/crm-overview", headers=owner.headers)).json()
    assert empty["total_leads"] == 0 and empty["conversion_rate"] is None
    assert len(empty["daily_new_leads"]) == 30

    stages = {
        s["key"]: s["id"]
        for s in (await client.get("/api/v1/pipeline-stages", headers=owner.headers)).json()
    }
    a = (await _create(client, owner, phone="+442079460600", source="web_form")).json()
    await _create(client, owner, phone="+442079460601", source="web_form", do_not_call=True)
    await client.patch(
        f"/api/v1/leads/{a['id']}", headers=owner.headers, json={"stage_id": stages["converted"]}
    )
    await client.post(
        "/api/v1/tasks",
        headers=owner.headers,
        json={"title": "Late", "due_at": "2000-01-01T00:00:00Z"},
    )

    o = (
        await client.get(
            "/api/v1/analytics/crm-overview", headers=owner.headers, params={"range_days": 7}
        )
    ).json()
    assert (o["total_leads"], o["new_leads"], o["converted_leads"]) == (2, 2, 1)
    assert o["conversion_rate"] == 0.5 and o["do_not_call"] == 1
    assert (o["open_tasks"], o["overdue_tasks"]) == (1, 1)
    assert o["by_source"] == [{"source": "web_form", "count": 2}]
    assert sum(d["count"] for d in o["daily_new_leads"]) == 2
    assert {s["key"]: s["count"] for s in o["by_stage"]}["converted"] == 1
