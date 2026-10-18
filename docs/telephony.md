# Phone numbers

Agents answer and place calls over a calling provider account that you own. ATLAS stores
nothing about the account itself: credentials are handed to the voice service, which keeps
them and talks to the provider. ATLAS shows which fields are on file, never their values,
and records every change in the audit log.

Only AI agents answer calls. There is no transfer to a human anywhere in the product.

## Supported providers

| Provider | Credential fields | Notes |
|---|---|---|
| Twilio | Account SID, Auth Token | Numbers can be imported from the account |
| Vonage | API key, API secret, Application ID, Private key, Signature secret (optional) | Numbers are routed through the Vonage application |
| Plivo | Auth ID, Auth Token, Application ID (optional) | An application is created for you when the ID is left empty |
| Telnyx | API key, Call Control App ID (optional), Webhook public key (optional) | A Call Control application is created for you when the ID is left empty |
| SIP trunk (Asterisk ARI) | ARI endpoint, ARI username, ARI password, websocket_client.conf name (optional), Dial string template (optional) | Bring your own Asterisk connected to any SIP trunk |

`GET /telephony/providers/catalog` returns exactly these fields, with `secret` and
`required` flags, so the UI renders the form from the API.

## Endpoints

Owners and admins (`integrations:manage`) connect providers and manage numbers. Managers
(`agents:manage`) choose which agent answers a number. Everyone with `agents:read` can see
providers, numbers and setup instructions.

| Method & path | Purpose |
|---|---|
| `GET /telephony/providers/catalog` | Providers you can connect and the fields each needs |
| `GET /telephony/providers` | Connected providers; `configured_fields` names what is on file |
| `POST /telephony/providers` | Connect a provider: `{name, provider, credentials, is_default_outbound}` |
| `GET` / `PATCH` / `DELETE /telephony/providers/{id}` | Inspect, rename or rotate some credentials, disconnect |
| `POST /telephony/providers/{id}/set-default-outbound` | Use this account for outbound calls |
| `GET /telephony/setup-instructions/{provider}` | Webhook URLs and console steps for that provider |
| `GET /telephony/numbers` | All numbers with the agent answering each (`?provider_id=` to filter) |
| `GET` / `POST /telephony/providers/{id}/numbers` | Numbers on one provider; add one the account owns |
| `POST /telephony/providers/{id}/numbers/sync` | Import the numbers the account holds (providers that support it) |
| `DELETE /telephony/numbers/{id}` | Remove a number |
| `PUT /telephony/numbers/{id}/inbound-agent` | `{agent_id}` or `{agent_id: null}`: who answers calls to this number |

Rotating credentials sends only the fields you supply; the rest stay as they are. The
voice service checks on `PATCH` that the result is a complete, valid credential set.

Numbers are accepted in any common format and stored in E.164 (`+14155550100`). The voice
service confirms the number is owned by the provider account before it is added; a number
that is not is refused with the provider's reason.

## Which agent answers a number

Routing is per number. When a call arrives, the voice service looks up the called number
under the provider account it came from and starts the agent assigned to that number. A
number with no agent gets a polite "not configured" response from the provider.

`PUT /telephony/numbers/{id}/inbound-agent` requires a **published** agent from the same
organization. Publishing creates the agent's call flow in the voice service; the assignment
points the number at that flow. Republishing the agent does not change the assignment. An
archived agent cannot be assigned; set `agent_id` to `null` to stop answering.

The response carries `provider_sync`: whether the voice service managed to write the inbound
webhook into the provider account. `ok: false` is a warning, not a failure: the assignment
is saved, and the manual steps below make the number ring through.

## Connecting a provider: what to paste where

`GET /telephony/setup-instructions/{provider}` returns the exact URLs for your deployment.
They are built from `ATLAS_VOICE_RUNTIME_PUBLIC_URL`, which must be the voice service's
public HTTPS address; the response warns when it still points at localhost.

* **Inbound webhook** — `{public url}/api/v1/telephony/inbound/run`. One URL for every
  number and every agent; the voice service works out the agent from the called number.
* **Fallback webhook** (Twilio) — `{public url}/api/v1/telephony/inbound/fallback`.
* **Event webhook** (Vonage) — `{public url}/api/v1/telephony/vonage/events`.

### Twilio

1. Connect the provider with the Account SID and Auth Token from Console → Account Info.
2. Add the numbers (or click **Import numbers**), then assign an agent to each.
3. The voice service sets each number's **A call comes in** webhook for you. If
   `provider_sync` reports a problem, open Phone Numbers → Active numbers → the number and
   paste the inbound webhook (HTTP POST) and the fallback webhook yourself.

Outbound calls need no console setup.

### Vonage

1. Create a Voice application in the Vonage dashboard; download its private key.
2. Connect the provider with the API key, API secret, Application ID and the private key
   file contents. Add the signature secret to have webhooks verified.
3. In the application set **Answer URL** to the inbound webhook (POST) and **Event URL** to
   the event webhook (POST), then link each number to the application.
4. Add the numbers in ATLAS and assign an agent. The voice service also updates the
   application's Answer URL when an agent is assigned.

### Plivo

1. Connect the provider with the Auth ID and Auth Token. Leave Application ID empty to have
   one created, or give the ID of an existing Voice application.
2. In Plivo Console attach each number to that application.
3. Add the numbers in ATLAS and assign an agent. The application's **Answer URL** is updated
   for you; paste the inbound webhook (POST) yourself if `provider_sync` reports a problem.

### Telnyx

1. Connect the provider with an API key. Leave Call Control App ID empty to have an
   application created, or give an existing one. Add the webhook public key from Mission
   Control Portal → Keys & Credentials to have webhooks verified.
2. In Mission Control Portal assign each number to that Call Control application.
3. Add the numbers in ATLAS and assign an agent. The application's **Webhook URL** is updated
   for you; paste the inbound webhook yourself if `provider_sync` reports a problem.

### SIP trunk (Asterisk ARI)

1. Enable ARI on your Asterisk and create an ARI user. Make the ARI endpoint reachable from
   the voice service.
2. Connect the provider with the endpoint, username and password. Set the dial string
   template to how your trunk dials out, for example `PJSIP/{number}@my-trunk`.
3. Route inbound DIDs from the trunk into the Stasis application, add each DID as a number
   in ATLAS and assign an agent. No webhooks are involved; signalling runs over ARI.

## Audit trail

`telephony.provider_created`, `telephony.provider_updated` (names of rotated fields only),
`telephony.provider_deleted`, `telephony.default_outbound_set`, `telephony.number_added`,
`telephony.number_removed`, `telephony.numbers_synced`,
`telephony.inbound_agent_assigned`. No credential value is ever written to the audit log,
the application log or an API response.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ATLAS_VOICE_RUNTIME_PUBLIC_URL` | `http://localhost:8000` | Public address of the voice service; the base of every webhook URL above |
