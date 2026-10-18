# API keys

API keys let your own systems call the ATLAS API without a user logging in. Each key
belongs to one organization and acts with one role.

## Creating a key

Owners and admins create keys under **Settings → API & webhooks**, or with
`POST /api/v1/developer/api-keys`:

```json
{ "name": "Zapier sync", "role": "manager", "expires_at": "2027-01-01T00:00:00Z" }
```

* **name** — how the key appears in lists and audit entries.
* **role** — the permissions the key has (`viewer`, `agent`, `manager`, `admin`, `owner`).
  You cannot create a key with a role above your own.
* **expires_at** — optional. Keys without an expiry stay valid until revoked.

The response contains the full key **once** (`atlas_…`). Store it in a secret manager;
ATLAS keeps only a hash and shows just the first characters afterwards.

## Using a key

Send the key in the `X-API-Key` header instead of a bearer token:

```http
GET /api/v1/leads?page_size=50
X-API-Key: atlas_k3Xw…
```

* `X-Organization-Id` is not required: the key already names its organization. If you
  send it, it must match, otherwise the request is refused with `403`.
* Keys work for every organization-scoped endpoint. They do not work for account
  endpoints under `/api/v1/auth/*` or for creating organizations.
* Every request is checked against the key's role exactly like a user session.

## What a key can do

A key's effective role is its own role, capped at the current role of the member who
created it. If that member's role is reduced, keys they created are reduced with it; if
they leave the organization, their keys stop working. For long-lived integrations,
create keys from an account that will stay in the organization.

## Rate limits

Each key may make **600 requests per minute**. Above that, requests return `429` with
`error.code = "rate_limited"` and a `retry_after` in seconds. Spread work across keys
or slow down; limits are per key, not per organization.

## Audit trail

Actions performed with a key appear in the audit log with the creating member as the
actor and the key recorded under `changes.actor` (`{"type": "api_key", "id": "…"}`).
Key creation and revocation are audited too.

## Revoking a key

`DELETE /api/v1/developer/api-keys/{id}` revokes a key immediately and permanently.
Revoke keys you no longer use, rotate keys on a schedule, and give each integration its
own key so one can be revoked without affecting the others.

## Good practice

* Use the lowest role that works: `viewer` for reporting, `agent` or `manager` for
  CRM sync, `admin` only for automation that manages settings.
* Never put a key in a browser or mobile app. Keys are for server-to-server use.
* Set `expires_at` for keys issued to contractors or for one-off migrations.
