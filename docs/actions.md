# Actions

An **action** is an HTTP call an AI agent can make during a conversation: look up
availability, book a viewing, update a record, send a confirmation. The agent decides
when to call it from the action's name and description, fills in the parameters you
declared, and the voice service makes the request and hands the response back to the
model.

Actions are defined once per organization and attached to flow steps (the step's
action list, `tool_uuids`, on the flow canvas; see ADR 0005). Only the AI agent uses
them; there is no human-transfer action.

## Endpoints

Managers and above (`agents:manage`) create, change, delete and test actions; every
member with `agents:read` can list them.

| Method & path | Purpose |
|---|---|
| `GET /actions` | Actions available to this organization |
| `POST /actions` | Create an action (`201`) |
| `GET /actions/{id}` | One action |
| `PATCH /actions/{id}` | Partial update; `"auth": null` removes the credential, omitting `auth` keeps it |
| `DELETE /actions/{id}` | Remove the action (`204`) |
| `POST /actions/{id}/test` | Run it now with sample values; returns status, latency and a response excerpt |

```json
{
  "name": "Book a viewing",
  "description": "Books a property viewing once the caller has confirmed a date and time.",
  "method": "POST",
  "url": "https://api.example.com/viewings",
  "headers": { "X-Source": "atlas" },
  "parameters": [
    { "name": "date", "type": "string", "description": "Viewing date as YYYY-MM-DD" },
    { "name": "guests", "type": "number", "description": "Party size", "required": false }
  ],
  "preset_parameters": [
    { "name": "phone", "type": "string", "value": "{{initial_context.phone_number}}" }
  ],
  "timeout_ms": 8000,
  "body_format": "json",
  "say_while_running": "Let me check that for you.",
  "auth": { "type": "bearer", "token": "..." }
}
```

The response adds `id` (opaque; use it in a step's action list), `function_name`,
`auth` (type, header and `"status": "configured"`, never the secret) and timestamps.

## Fields

| Field | Notes |
|---|---|
| `name` | What the model sees. The **function name** the model calls is derived from it: lower-case, every run of characters other than `a-z 0-9 _` becomes one `_`, leading and trailing `_` removed (`Book a viewing!` becomes `book_a_viewing`). It must be unique among the actions on a step and must not equal one of that step's path names; the canvas uses `function_name` to flag collisions. |
| `description` | When the model should use the action and what it gets back. Write it as an instruction. |
| `method`, `url` | `GET`, `POST`, `PUT`, `PATCH` or `DELETE`. The URL must pass the [outbound destination policy](security.md#outbound-destination-policy). |
| `headers` | Static headers sent with every request. Authentication and framing headers (`Authorization`, `Proxy-Authorization`, `Cookie`, `Host`, `Content-Length`, `Transfer-Encoding`, `Connection`) are refused, as is the header used by the action's credential. Up to 20. |
| `parameters` | Values the model supplies: `name` (identifier), `type` (`string`, `number`, `boolean`, `object`, `array`), `description`, `required` (default true). Up to 30. |
| `preset_parameters` | Values filled in from the call, never asked of the model. `value` is a fixed value or a template: `{{initial_context.x}}` reads call-start context (lead fields, phone number), `{{gathered_context.x}}` reads values captured during the call. `required` fails the call if the value is empty. Names must not repeat across both parameter lists. |
| `timeout_ms` | 100 to 15000, default 5000. |
| `body_format` | `json` (default) or `form` for `POST`, `PUT` and `PATCH`. `GET` and `DELETE` send parameters as the query string. |
| `say_while_running` | A short sentence the agent speaks while the request runs. |
| `auth` | `api_key` (`header_name`, `api_key`), `bearer` (`token`), `basic` (`username`, `password`) or `custom_header` (`header_name`, `header_value`). Stored by the voice service as a credential; ATLAS never returns or logs it. Updating `auth` replaces the credential; deleting the action deletes it. |

Parameters are sent as a flat JSON object (or form fields / query string) keyed by
parameter name, presets first and model-supplied values on top.

## Testing an action

`POST /actions/{id}/test` with sample values:

```json
{ "parameters": { "date": "2026-11-02" }, "preset_parameters": { "phone": "+15550100" } }
```

```json
{
  "ok": true,
  "status_code": 200,
  "latency_ms": 87,
  "request_method": "POST",
  "request_url": "https://api.example.com/viewings",
  "response_excerpt": "{\"slots\": [\"10:00\", \"14:30\"]}",
  "error": null,
  "hint": null
}
```

`hint` explains common failures (401: check the credential; 404 or 405: check URL and
method; 415: body format). The URL is re-checked against the destination policy right
before each test because DNS answers change.

## Limits and behaviour

* Up to 100 actions per organization.
* Every create, update, delete and test is written to the audit log with the action's
  name, function name, method, URL, header *names* and credential *type*; never header
  values or secrets.
* Actions live in the organization's own voice-service account, so they are never
  visible to another organization.
* Deleting an action that a flow step still references leaves that step without the
  action on its next publish; remove the reference and publish the agent again.
