# Webhooks

Webhooks notify your systems when something happens in ATLAS: a lead is created, moves
stage, a call completes. Each event is delivered as a signed HTTPS `POST` to a URL you
register.

## Registering an endpoint

Owners and admins manage endpoints under **Settings → API & webhooks** or with the
`/api/v1/developer/webhooks` endpoints:

| Method & path | Purpose |
|---|---|
| `GET /developer/events` | Event types you can subscribe to |
| `POST /developer/webhooks` | Register an endpoint; returns the signing **secret once** |
| `GET` / `PATCH` / `DELETE /developer/webhooks/{id}` | Inspect, change or remove an endpoint |
| `POST /developer/webhooks/{id}/test` | Send a `ping` now and see the response code and latency |
| `POST /developer/webhooks/{id}/rotate-secret` | Issue a new secret (returned once) |
| `GET /developer/webhooks/{id}/deliveries` | Delivery history with status, attempts and errors |
| `POST /developer/deliveries/{id}/retry` | Queue a failed or dead delivery again |

```json
{
  "url": "https://hooks.example.com/atlas",
  "description": "CRM sync",
  "events": ["lead.created", "lead.stage_changed"]
}
```

Use `["*"]` to receive every event type, including ones added later.

### URL requirements

* `https://` only. The certificate must be valid.
* The host must resolve to a public address. Private, loopback, link-local and cloud
  metadata addresses are refused when the endpoint is saved and again before every
  delivery.
* No credentials in the URL. Redirects are not followed; a `3xx` response counts as a
  failure.

## The request

```http
POST /atlas HTTP/1.1
Content-Type: application/json
User-Agent: ATLAS-Webhooks/1.0
X-Atlas-Event: lead.stage_changed
X-Atlas-Event-Id: 6d1f9d2e-5c0a-4d5e-9f3b-2c1a0b7e8f90
X-Atlas-Delivery-Id: 0b4c3a2d-…
X-Atlas-Timestamp: 1760745600
X-Atlas-Signature: v1=5f1a…c0de
```

```json
{
  "id": "6d1f9d2e-5c0a-4d5e-9f3b-2c1a0b7e8f90",
  "type": "lead.stage_changed",
  "created_at": "2026-10-18T09:00:00+00:00",
  "organization_id": "…",
  "data": {
    "lead_id": "…",
    "from_stage": "contacted",
    "to_stage": "qualified",
    "from_stage_id": "…",
    "to_stage_id": "…"
  }
}
```

Respond with any `2xx` status within **10 seconds**. Do your processing after
responding; a slow handler is treated as a failure and retried.

### Events and payloads

Lead events carry identifiers, pipeline position and name/company only. Phone numbers,
email addresses, notes and custom fields are never included; fetch them through the API
with an API key if you need them.

| Event | `data` |
|---|---|
| `lead.created` | `lead_id`, `first_name`, `last_name`, `company`, `stage`, `stage_id`, `owner_user_id`, `source`, `tags`, `qualification_status`, `do_not_call` |
| `lead.updated` | the fields above plus `changed_fields` (list of names) |
| `lead.stage_changed` | `lead_id`, `from_stage`, `to_stage`, `from_stage_id`, `to_stage_id` |
| `call.started`, `call.completed` | call identifiers, lead id, `mode` (`live` or `simulated`), outcome and captured fields. Browser test calls from the agent editor are never sent |
| `campaign.started`, `campaign.completed` | campaign identifiers and counts |
| `ping` | sent by the **test** action: `endpoint_id`, `message` |

## Verifying the signature

Every delivery is signed with your endpoint's secret:

```
signature = HMAC-SHA256(secret, "<X-Atlas-Timestamp>.<raw request body>")
X-Atlas-Signature: v1=<lowercase hex of signature>
```

Always sign the **raw bytes** you received, not a re-serialized copy. Reject requests
whose timestamp is older than five minutes to prevent replays, and compare signatures
with a constant-time function.

**Python**

```python
import hmac, hashlib, time

def verify(secret: str, headers: dict, body: bytes, tolerance: int = 300) -> bool:
    timestamp = headers["X-Atlas-Timestamp"]
    if abs(time.time() - int(timestamp)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(headers["X-Atlas-Signature"], f"v1={expected}")
```

**Node.js**

```js
const crypto = require("node:crypto");

function verify(secret, headers, rawBody, tolerance = 300) {
  const timestamp = headers["x-atlas-timestamp"];
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > tolerance) return false;
  const expected = "v1=" + crypto
    .createHmac("sha256", secret)
    .update(`${timestamp}.`)
    .update(rawBody) // Buffer of the raw request body
    .digest("hex");
  const given = headers["x-atlas-signature"] || "";
  return given.length === expected.length &&
    crypto.timingSafeEqual(Buffer.from(given), Buffer.from(expected));
}
```

With Express, keep the raw body with `express.raw({ type: "application/json" })` on
the webhook route so the bytes are not altered before verification.

### Rotating the secret

`POST /developer/webhooks/{id}/rotate-secret` returns a new secret and invalidates the
old one immediately. Update your receiver first, then rotate.

## Delivery, retries and failures

* Events are delivered **at least once**, usually within 15 seconds. Use `id` (also in
  `X-Atlas-Event-Id`) as an idempotency key; the same event may arrive twice after a
  retry or network timeout.
* Order is not guaranteed across events. Use `created_at` when sequence matters.
* A delivery that fails (no `2xx`, timeout, connection error) is retried with
  exponential backoff: about 1, 2, 4, 8, 16, 32 and 64 minutes (±20% jitter), **8 attempts
  in total**. After the last attempt the delivery is marked `dead`.
* Dead deliveries stay visible for 30 days and can be re-queued with
  `POST /developer/deliveries/{id}/retry` or from the delivery history screen.
* After **50 consecutive failures** an endpoint is disabled automatically; an audit entry
  records why. Fix the receiver, then re-enable it with `PATCH … {"is_active": true}`:
  the failure counter resets and parked deliveries resume.
* Events that occur while an endpoint is disabled are not queued for it.

## Testing

`POST /developer/webhooks/{id}/test` sends a signed `ping` event right away and returns
the status code, latency and any error, so you can confirm the receiver and signature
check before relying on real events.
