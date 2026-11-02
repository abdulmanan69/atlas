# Where the work stands, and how to pick it up again

Written 2026-11-02, when the Oracle server was shut down and deleted. Everything below is in
this repository; nothing needed for development lives only on a server.

## What ATLAS is at this point

A multi-tenant AI voice-agent and CRM platform: React web app (`apps/web`), FastAPI API with
ARQ workers (`services/api`), Postgres and Redis, and a vendored voice runtime behind an
adapter (its product name never appears in user-facing text; see `THIRD_PARTY_NOTICES.md`).
Self-hosted model servers in `infra/docker/`: Pocket TTS (CPU voice, cloning), the expressive
voice (Chatterbox-Turbo on a GPU: laughs, sighs, cloning), the voice-design server (Qwen3-TTS
on a GPU: a voice from a written description; built and unit-tested, not yet measured), live
speech-to-text (sherpa-onnx; Parakeet 110M on CPU, Nemotron Speech Streaming on a GPU), an AI
gateway with a health report, and Ollama for the language model. ATLAS registers a models
host as an "AI server" and creates its connections, warms the agent's prompt before each
call, and checks the host every two minutes. Design notes: `docs/decisions/` (ADR 0001-0007),
deployment layouts and the GPU host: `docs/deployment-layouts.md`, models and measurements:
`docs/local-models.md`, `docs/perf/`.

Done in the last stretch (all tested and pushed): a recording is checked before it is cloned
(clipping, noise, level, silence, the clearest 15 s chosen); voice servers state their
capabilities (`GET /v1/capabilities`); "Design a voice" in the web app when a server offers
it; the CRM automations audited on edge cases with 21 new tests and 10 fixes (double
dialling, lost outcomes, voicemail counted as success, webhook batches, leases); the
GPU speech-to-text default moved to Nemotron (final transcript 257 ms after the caller
stops, nothing heard in silence; `docs/perf/gpu-stt-streaming.md`).

## Resume on this PC

```bash
docker compose up -d postgres redis          # or start Docker Desktop first
cd services/api && uv sync && uv run alembic upgrade head && uv run python -m app.seed
uv run uvicorn app.main:app --port 8080      # API;  cd apps/web && npm run dev  # web
```

Tests: `cd services/api && uv run pytest -q` (needs Postgres and Redis), or on a server with
`bash infra/scripts/remote_api_tests.sh tests/<file>` (edit the host at the top; it needs the
stack's Docker network and `~/atlas/.env` there). Web: `cd apps/web && npx vitest run`,
`npx tsc --noEmit -p tsconfig.json`, `npx eslint src`. Model servers have their own unit
tests with fake models (`python -m unittest discover -s infra/docker/<server> -t ...`).

## Deploy to a server again

`infra/deploy/oci/README.md` and `deploy.sh`: an all-in-one CPU server
(`bash infra/deploy/oci/deploy.sh --public-ip <ip>`), a models host (`--role models`), or a
GPU models host (`--role models --gpu`). The deploy creates `.env` with the demo password and
prints it once. The demo agent for tests is created by `infra/scripts/auto_warranty_agent.py`
(`E2E_PASSWORD=<demo password> python infra/scripts/auto_warranty_agent.py https://<host>`);
a cloned real voice for it was built from the VCTK corpus (CC BY 4.0, speaker p311, five
clips concatenated at 24 kHz, 19 s) and uploaded on the AI providers page.

Measured call latency on the Oracle CPU server (4 cores) was about 2-3.6 s per reply with
the small model; with the three models on one T4 (Kaggle) 0.7-1.5 s, greeting under 1 s.

## GPU tests on Kaggle

`python infra/scripts/kaggle_gpu.py start --voice --stt --hours 3 --api https://<atlas>`
runs Ollama (qwen3:8b), the expressive voice and live speech-to-text on a Kaggle T4 behind a
gateway-shaped proxy and registers it in ATLAS as the AI server "GPU test (Kaggle)"; `stop`
puts everything back. It needs a Kaggle API token (`KAGGLE_API_TOKEN`; the one used before
has expired) and the Kaggle CLI (`pip install kaggle`). A session takes about 25 minutes to
come up. Kaggle allows two GPU sessions at a time and 30 GPU hours a week.

## Not finished

- Voice-design server (`infra/docker/voice-design`): measurements on a T4 (the README says
  what the first run found and how to collect the numbers); its Docker image was not built;
  not wired into `docker-compose.gpu.yml`, the gateway route or the Kaggle launcher.
- Nemotron on the GPU: measured through the server's protocol, not yet in an end-to-end call
  through the voice service; `STT_FINALIZE_PAD_MS` below 600 for the 160 ms export untried.
- Ideas from the research pass (`docs/perf` has none of these yet): a provenance watermark
  on Pocket TTS output with a verify endpoint, an MCP server over the CRM for external
  agents, a per-agent delivery direction for voices that take instructions, the "snappy"
  response preset switched to the turn analyzer (measured faster on Oracle).
- Repeating the Pakistani-accent speech-to-text evaluation needs consented recordings; the
  previous ones were deleted with the server and were never copied off it.

## Secrets and where they are

None are in the repository. The Oracle SSH key and known_hosts were kept at
`D:\dev-cache\ssh\` on the development PC; the demo password lives only in the server's
`.env`; Kaggle and Hugging Face tokens were under `D:\dev-cache\` (both should be rotated
before reuse). A new server needs its own key and a fresh deploy.
