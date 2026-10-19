# ADR 0006 — Low-latency voice: measure every turn, then tune

**Status:** accepted · 2026-10-19 · implemented (Milestone 6b)

## Context
Callers judge an AI agent by how fast it answers. The runtime pipeline is
VAD → STT → LLM → TTS → transport; ATLAS does not run audio itself, so its levers are
(1) which providers and models a tenant selects, (2) the turn-taking and context options
compiled into each workflow, (3) prompt and tool design, and (4) showing tenants what
their calls actually measured so they can tune. Milestone 6b builds the measurement
first (nothing is claimed that was not measured), then presets and guidance.

## Verified runtime facts (runtime v1.49 source, `vendor/voice-runtime/api`)

| Fact | Where |
|---|---|
| Run detail `GET /workflow/{id}/runs/{run_id}` returns `logs`, a dict that includes `realtime_feedback_events`: the full per-turn event stream saved at call end. | `routes/workflow.py:1502`, `services/pipecat/event_handlers.py:524-528` |
| Each event: `{type, timestamp (ISO-8601 UTC, ms), turn?, node_id?, node_name?, payload}`, sorted by timestamp. | `realtime_feedback_events.py:154-180`, `in_memory_buffers.py:150-185` |
| `type` values (`pipecat.utils.enums.RealtimeFeedbackType`, read from the running image): `rtf-user-transcription`, `rtf-bot-text`, `rtf-function-call-start`, `rtf-function-call-end`, `rtf-ttfb-metric`, `rtf-node-transition`, `rtf-latency-measured`, `rtf-pipeline-error`, `rtf-bot-started-speaking`, `rtf-bot-stopped-speaking`, `rtf-user-mute-started`, `rtf-user-mute-stopped`. | image: `python -c "from pipecat.utils.enums import RealtimeFeedbackType"` |
| `rtf-user-transcription` payload: `text, final, timestamp (user speech start), end_timestamp (user speech end, from VAD stop), user_id?`. `rtf-bot-text` payload: `text, timestamp (bot speech start), end_timestamp (bot speech end)`. Both stamped with `turn`. | `realtime_feedback_events.py:28-67`, `transcript_log_coordinator.py:117-161, 260-280` |
| `rtf-ttfb-metric` payload: `ttfb_seconds, processor ("<Vendor>STTService#3" style), model, kind ("stt"\|"llm"\|"tts"; missing → "llm")`. Classify by suffix `...STTSERVICE/...TTSSERVICE/...LLMSERVICE`, never substring-first (every STT name contains "TTS"). | `realtime_feedback_events.py:110-131`, `observability/call_events/metrics.py:1-30` |
| `rtf-function-call-start/end` payload: `function_name, tool_call_id, arguments?/result?`. Match by `tool_call_id`. `retrieve_from_knowledge_base` is the knowledge tool; ATLAS actions use their own names. | `realtime_feedback_events.py:69-108`, `tools/knowledge_base.py:349` |
| Runtime's "response latency" = time from user silence until the bot starts speaking (`dograh_response_latency{kind="user_to_bot"}`); `rtf-latency-measured` carries the per-turn value. | `observability/pipeline_metrics.py:40-55`, `call_events/recorder.py:455-465` |
| Per-workflow knobs (`workflow_configurations`): `turn_start_strategy default\|min_words` (+`turn_start_min_words`), `turn_stop_strategy transcription\|turn_analyzer` (+`smart_turn_stop_secs`), `max_user_idle_timeout`, `context_compaction_enabled` (default false), `tts_cache_enabled` (no TTS provider uses it in v1.49: `service_factory.py:757`; ATLAS keeps the switch disabled), `transcript_configuration.include_end_timestamps`. VAD stop is fixed at 0.2 s. | `schemas/workflow_configurations.py:125-172`, `run_pipeline.py:181, 205-260, 701-704` |
| STT services with built-in end-of-turn (Deepgram Flux, Cartesia turns, the vendor-managed STT) make `turn_stop_strategy: transcription` the fast path; otherwise `turn_analyzer` runs a local smart-turn model with `smart_turn_stop_secs`. | `service_factory.py:283-290`, `run_pipeline.py:247-256` |
| LLM fallback on the model configuration: `llm_fallback: {version: 1, rules: [{condition: {type: "no_output", after_ms 100–10000} \| {type: "error"}, target: LLMConfig}]}` (≤ 2 rules). | `schemas/llm_fallback.py`, `schemas/ai_model_configuration.py:18` |
| Prometheus at `GET /metrics` per runtime worker, enabled by `ENABLE_PROMETHEUS_METRICS=true`, guarded by header `X-Dograh-Devops-Secret` = env `DOGRAH_DEVOPS_SECRET` (503 when unset). Histograms `dograh_ai_latency{service,model,kind,conversation_type}` and `dograh_response_latency{kind="user_to_bot"}`, seconds, buckets to 60 s. **Process-wide, all tenants.** | `routes/main.py:155-262`, `observability/metrics.py:30-80`, `constants.py:78-80` |
| Call-event sinks are packages auto-discovered from `api/services/integrations/*` (`pkgutil.iter_modules`); only BigQuery ships. A real-time HTTP sink = a mounted package, no image rebuild. | `integrations/loader.py:10-19`, `integrations/bigquery/__init__.py` |
| Pre-recorded greetings need uploaded audio (`/recordings/upload-url` + create); the runtime does not synthesise them. | `routes/workflow_recording.py:62-170` |
| Self-hosted STT (`speaches`) is OpenAI-compatible HTTP transcription (segmented after VAD stop), not streaming. | image `pipecat/services/speaches/stt.py` |
| ATLAS today: `WorkflowRun` in `app/integrations/voice_runtime/client.py` has no `logs` field; sync in `app/services/calls.py` reads `usage_info`, `cost_info`, `gathered_context`, `transcript_url`. `CallSettings` in `app/schemas/voice.py:322` compiles in `workflow_builder.py:398-414`. | ATLAS source |

## Decisions
1. **Measure every real call.** On run sync, parse `logs.realtime_feedback_events` into per-turn latency and a summary stored on the call (`calls.latency` JSON). Response latency per turn = bot speech start − user speech end of the same turn, using `rtf-latency-measured` when present. Simulated calls get synthetic latency labelled `source: "simulated"`, never mixed into real averages.
2. **Show it where decisions are made:** call detail (per-turn waterfall), Reports (p50/p95 response, by agent), call settings (what the preset does).
3. **Response speed presets** on call settings: `snappy`, `balanced` (default), `patient`, `custom`. `transcript_configuration.include_end_timestamps` always on. Context compaction defaults on. TTS cache offered only when the TTS provider supports it.
4. **Fast stack guidance** on AI providers: latency guide with *typical, vendor-stated* figures labelled as not measured by ATLAS, plus one-click presets applied when the connections exist.
5. **Pipeline health** panel from the runtime's Prometheus endpoint (owner/admin only), labelled platform-wide, process lifetime.
6. **Prompt budget**: flow validation warns when a node prompt or the global rules exceed an estimated token budget.
7. Deferred: real-time per-turn streaming to ATLAS (mounted sink package); pre-recorded greetings.

## API contract (both sides code against this)

```text
LatencySummary { source: "runtime"|"simulated", turns, measured_turns: int,
  response_p50_ms, response_p95_ms, response_max_ms, first_response_ms: int|null,
  stt_ttfb_p50_ms, llm_ttfb_p50_ms, tts_ttfb_p50_ms: int|null,
  tool_time_ms: int, slow_turns: int, interruptions: int }
LatencyTurn { turn: int, node_name, user_text, agent_text: str|null,
  user_started_at, user_ended_at, agent_started_at, agent_ended_at: datetime|null,
  response_ms, stt_ttfb_ms, llm_ttfb_ms, tts_ttfb_ms: int|null,
  tools: [{ name, kind: "knowledge"|"action", duration_ms: int|null }], slow: bool }
CallLatencyDetail { summary, turns[], thresholds: { target_ms: 800, slow_ms: 1500 } }

CallDetail.latency_summary: LatencySummary | null
GET  /api/v1/calls/{call_id}/latency → CallLatencyDetail (404 when not measured)
CallsOverview: + response_p50_ms, response_p95_ms: int|null, measured_calls: int; by_agent[] + response_p50_ms

CallSettings: + response_speed "snappy"|"balanced"|"patient"|"custom" = "balanced"
              + context_compaction: bool = true, + tts_cache: bool = false
  snappy   → turn_start min_words/1, turn_stop transcription, smart_turn_stop 0.8, idle 8
  balanced → min_words/2, transcription, 1.5, 10
  patient  → default, turn_analyzer, 2.5, 15
  custom   → explicit fields as today

GET  /api/v1/ai-settings/latency-guide → { providers: [{kind, provider, model_hint, typical_ms, streaming, turn_aware, note}],
       presets: [{id, title, description, choices: {stt, llm, tts}, requires: [provider ids]}], disclaimer }
POST /api/v1/ai-settings/presets/{preset_id}/apply → ModelSelection | 409 { missing: [provider ids] }
GET  /api/v1/ai-settings/pipeline-health → { available, scope: "platform", window: "process lifetime",
       services: [{service, p50_ms, p95_ms, samples}], response: {p50_ms, p95_ms, samples} | null, note }

Flow validation warnings: prompt_long (node, estimated_tokens, budget 1500), global_rules_long (flow, budget 800)
```

## Workstreams (none started; run as four parallel agents, per ADR 0004 conventions)
- **WS-A backend**: `app/services/latency.py` parser + `calls.latency` column (migration) + sync + simulated latency + endpoints + call-settings presets + latency guide/presets/pipeline-health + prompt-budget warnings + tests (`tests/test_latency.py`); regenerate web types early (`python -m app.openapi_export`, `pnpm gen:api`). Test DB suffix `a`, Redis 10.
- **WS-B web**: Response times card (CSS waterfall, no chart lib), Reports KPIs, Response speed segmented control, AI providers Speed section, inspector token meter. Local types from this contract until generated types land.
- **WS-C local models**: `infra/scripts/bench_local_models.py`, compose tuning, `make local-ai-warm`, honest numbers in `docs/local-models.md`; run benchmark only with ≥ 3.5 GB free RAM.
- **WS-D platform speed**: `infra/scripts/bench_api.py` before/after (`docs/perf/`), Server-Timing + gzip, composite indexes migration, hover prefetch. Test DB suffix `b`, Redis 11.
- Machine has ~1 GB free RAM while dev servers run: agents run only their own test files, one at a time.

## Consequences
- New settings: `ATLAS_VOICE_RUNTIME_METRICS_SECRET` (= runtime `DOGRAH_DEVOPS_SECRET`); compose enables `ENABLE_PROMETHEUS_METRICS` on the runtime.
- Latency fixtures in tests follow the event shapes above, with source citations.
- Local-model numbers come from the benchmark on the target machine; if it cannot run, the docs say so.
