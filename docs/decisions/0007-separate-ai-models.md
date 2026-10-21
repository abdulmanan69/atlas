# ADR 0007 — ATLAS and the AI models as separate, connectable parts

**Status:** accepted · 2026-10-22 · phase A in progress

## Context
ATLAS has three kinds of work with very different needs:

| Part | What runs | Needs |
|---|---|---|
| **App** (the CRM) | web app, API, worker, PostgreSQL, Redis | little CPU; must be always on; holds the tenants' data |
| **Voice** (call engine) | voice service with its database, Redis and storage, TURN relay | moderate CPU; real-time; public signalling and TURN ports |
| **Models** (AI) | language model (Ollama), Whisper, Pocket TTS, live speech-to-text | most of the compute; a GPU makes replies about 25 times faster ([perf/llm-turns.md](../perf/llm-turns.md)) |

Today all three run on one host from one Compose file, and the model servers are reached
by Docker name, on a private network, without any authentication. An operator who wants
the CRM on a small server and the models on a GPU server cannot do it safely.

Facts that shape the design (voice service v1.49, `vendor/voice-runtime/api`):
- Every model connection is checked when it is saved (`services/configuration/model_connections.py:215`).
  OpenAI-compatible ("speaches") connections are checked against their own `base_url`;
  Deepgram-type connections, which ATLAS uses for live speech-to-text, are always checked
  against Deepgram's cloud with the stored key (`check_validity.py:360`), whatever the base URL.
- OpenAI-compatible clients send the connection's key as `Authorization: Bearer <key>`; the
  Deepgram client sends `Authorization: Token <key>` (the Deepgram key, not ours).
- A Deepgram base URL keeps its path: the WebSocket URL is the base URL with `/v1/listen`
  appended (`service_factory.py:338-375`).
- ATLAS warms the language model up at call start only for self-hosted (private) addresses
  (`app/services/llm_warmup.py`).

## Decision
1. **Three roles, one code base.** `app`, `voice` and `models` can each run on any host;
   all three on one host stays the default and needs no change. Supported layouts:

   | Layout | Hosts | When |
   |---|---|---|
   | All in one | 1 | demos, small teams (today) |
   | App · voice + models | 2 | **recommended with a GPU**: the voice service sits next to the models, so no request crosses the network |
   | App + voice · models | 2 | a GPU host in the same region as the app; one network round trip per model request |
   | App · voice · models | 3 | large deployments |

2. **AI gateway on the models host.** One HTTPS entry point (Caddy) in front of the model
   servers, the only port that host opens:

   | Path | Goes to | Protocol |
   |---|---|---|
   | `/llm/v1/*` | language model, OpenAI-compatible API only (`/api/*` management is not reachable) | HTTP, streaming |
   | `/whisper/v1/*` | Whisper speech-to-text | HTTP |
   | `/tts/v1/*` | Pocket TTS, including voices and voice cloning | HTTP, streaming |
   | `/live-stt/<secret>/v1/listen` | live speech-to-text, Deepgram protocol | WebSocket |
   | `/health` | which services answer, with versions and models | HTTP |

   HTTP paths require `Authorization: Bearer <server token>` (a random 256-bit token,
   generated on the models host and shown once). Live speech-to-text carries a separate
   secret in the path, because the voice service sends the Deepgram key in that header.
   TLS is automatic (Let's Encrypt) for a public host name, or the operator's own
   certificate; plain HTTP is accepted only on private networks, as today. No access log
   (paths carry a secret).

3. **"AI servers" in ATLAS.** An owner registers a models host once (name, address,
   token). ATLAS checks `/health`, shows what the server offers, and creates the model
   connections for it (language model, Whisper, Pocket TTS, and live speech-to-text when a
   Deepgram key is given for the cloud check above). The token is stored encrypted, never
   shown again, and can be rotated; the server's status and round-trip time are checked
   regularly and shown under AI providers. ATLAS trusts registered servers for the warm-up.
   Removing a server is refused while the organisation's models or an agent use it.

4. **Edge cases handled, not assumed away.**
   - Models host down or slow: the model configuration can name a fallback language model
     (the voice service's `llm_fallback` rule: no output within N ms, or an error).
   - Token leaked: rotate on the models host, then in ATLAS (one place updates every
     connection of that server).
   - Gateway reachable but a service missing (for example no GPU memory for a model): the
     health check names the missing service; its connection is marked unavailable.
   - Large uploads (voice samples up to 10 MB) and long streams (calls up to 20 minutes):
     the gateway sets no request timeout on streams and limits bodies to 12 MB.
   - Same host: the internal Docker names keep working; the gateway is optional there.

5. **Phases.** A: models role, AI gateway, AI servers in ATLAS, deploy option, docs, a
   cross-host test (local ATLAS using the Oracle server's models). B: the voice role on its
   own host (voice service API reachable only from the app host, callbacks to the app's
   public address, TURN on the voice host).

## Consequences
- A GPU host can serve several ATLAS installations; each registers it with its own token
  (one token per ATLAS installation is recommended, so one can be revoked alone).
- Calls through a remote models host send the conversation's text and audio to that host:
  it must be operated with the same care as the app host (documented).
- Live speech-to-text on a remote host still needs a Deepgram key once, for the voice
  service's save-time check; ATLAS asks for it only when registering a server that offers it.
