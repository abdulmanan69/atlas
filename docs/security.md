# Security notes

What ATLAS does with secrets and data that leave the platform. Complements
[architecture](architecture.md) and ADR 0002 (auth and tenancy).

## Secret handling

| Secret | Where it lives | What ATLAS returns |
|---|---|---|
| Tenant passwords | ATLAS, hashed | Nothing |
| Session refresh tokens | ATLAS, hashed; browser cookie | Nothing after issue |
| Tenant API keys (`/developer/api-keys`) | ATLAS, hashed | The key once, at creation |
| Webhook signing secrets (`/developer/webhooks`) | ATLAS, encrypted at rest | The secret once, at creation or rotation |
| Voice service account key per organization | ATLAS, encrypted at rest (`ATLAS_SECRETS_ENCRYPTION_KEY`) | Nothing; internal only |
| AI provider keys (LLM, STT, TTS, embeddings) | Voice service, after it verified them with the provider | `configured_credentials`: field names only |
| Calling provider credentials (Twilio, Vonage, Plivo, Telnyx, SIP) | Voice service | `configured_fields`: field names only |

Rules that apply everywhere:

* A secret is accepted on write and never echoed: not in responses, not in logs, not in the
  audit log. Audit entries record *which* fields changed (`credential_fields`,
  `rotated_credentials`), never values. The log formatter additionally redacts any key that
  looks like a secret (`password`, `secret`, `token`, `api_key`, `authorization`, `cookie`,
  `signature`).
* Rotating one calling-provider credential sends only that field in clear; the other fields
  travel as the mask the voice service handed out, and the voice service restores their
  stored values. ATLAS therefore never holds a provider secret it was not just given.
* Unknown credential fields are refused before anything reaches the voice service, so a
  request cannot smuggle extra data into a provider configuration.
* The voice service is addressed only by ATLAS's own server-to-server URL
  (`ATLAS_VOICE_RUNTIME_BASE_URL`) with a per-organization key. Every organization is a
  separate account inside the voice service, so credentials, agents, numbers, documents and
  call data are isolated there as well as in ATLAS.

## Data that leaves the platform

* **Call audio and transcripts** go to the AI providers the organization configured.
* **Knowledge-base documents** are parsed and chunked by an external document-processing
  service operated alongside the voice service, and each passage is sent to the selected
  embeddings provider. ATLAS keeps no copy. Set `ATLAS_KNOWLEDGE_ENABLED=false` where
  document text must not leave your systems. See [knowledge](knowledge.md).
* **Calling providers** receive the numbers dialled and the webhook URLs of the voice
  service. See [telephony](telephony.md).

## Uploads

Knowledge-base uploads are validated by content, not by file name or declared type: a PDF
must start with a PDF header, a `.docx` must be a ZIP containing `word/document.xml`, text
formats must be valid UTF-8 with no NUL bytes. Empty files and files above
`ATLAS_KNOWLEDGE_MAX_UPLOAD_BYTES` are refused before any upload happens. File names are
reduced to a safe character set. The bytes are then PUT to a short-lived signed URL issued
by the voice service; ATLAS does not follow redirects on that request, uses a bounded
timeout, and never logs the signed URL.

## Webhooks

Outbound webhooks to customer systems are signed and only delivered to public `https`
addresses; see [webhooks](webhooks.md). Inbound call events from the voice service carry a
per-organization shared secret header that is verified on every request.

## Outbound destination policy

Every URL ATLAS calls on a customer's behalf (webhook endpoints, action requests) is checked
when it is saved and again before each request: `https://` only, no credentials in the URL,
the host must resolve to a public address (private, loopback, link-local and cloud metadata
ranges are refused unless `ATLAS_ALLOW_PRIVATE_WEBHOOK_URLS` is set for local development),
and redirects are not followed. The knowledge-base upload is the one deliberate exception:
its destination is a signed URL issued by the voice service, not by a tenant, and it may
point at internal object storage.
