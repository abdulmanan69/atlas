# ADR 0005 — Flow canvas: ATLAS agent flows compiled to runtime workflows

**Status:** accepted (design) · 2026-10-18 · implementation in Milestone 5b

## Context
The guided agent editor (greeting → instructions → wrap-up) covers most lead-calling
agents. Enterprise customers also need branching conversations (qualify → book a viewing
or → hand to a follow-up step), per-step actions and knowledge, and global rules. The
runtime executes a graph; ATLAS exposes a visual **flow canvas** that compiles to it.

## Verified runtime facts the design depends on (runtime v1.49 source, `api/`)

| Fact | Where |
|---|---|
| Node types: `startCall`, `agentNode`, `endCall`, `globalNode`, `trigger`, `webhook`, `qa`. Unknown `data` keys are dropped on save. | `services/workflow/dto.py:1185-1226` |
| Every node: `name` (min 1). Prompt nodes: `prompt` (non-blank), `allow_interrupt` (agentNode default true), `add_global_prompt` (default true; endCall default false). | `dto.py:119-142`, `1016-1019` |
| Extraction on start/agent/end: `extraction_enabled`, `extraction_prompt`, `extraction_variables[{name,type:string\|number\|boolean,prompt}]`. Runs on every outgoing transition (background) and once at call end. Results land in `gathered_context` and `gathered_context.extracted_variables`; **not available in later node prompts**. | `dto.py:145-169`, `pipecat_engine.py:442-446`, `624-752`, `925-936` |
| Start/agent only: `tool_uuids`, `document_uuids`. Documents add a `retrieve_from_knowledge_base(query)` tool scoped to those documents. | `dto.py:172-190`, `pipecat_engine.py:542-622` |
| startCall extras: `greeting`, `greeting_type` text\|audio, `allow_greeting_interrupt`, `greeting_interrupt_min_words` (≥1), `delayed_start`, `delayed_start_duration` (0–10). Exactly one; no incoming edges. | `dto.py:353-408`, `:216-221` |
| agentNode: ≥1 incoming edge. endCall: ≥1 incoming, 0 outgoing, several allowed; bot speaks one reply then hangs up. | `dto.py:473`, `:545`, `pipecat_engine.py:467-490` |
| globalNode: at most one, no edges; its prompt is **prepended** to every node with `add_global_prompt`. | `dto.py:631-666`, `pipecat_engine_context_composer.py:56-92` |
| Edge `data`: `label` (min 1), `condition` (min 1), optional `transition_speech`(+type/recording). The LLM picks an edge by calling a function named `re.sub("[^a-z0-9]","_",label.lower())` whose description is `condition`. Function names must be unique per source node and must not collide with that node's tool names. | `dto.py:1116-1128`, `workflow_graph.py:20-55`, `tool_name_validation.py:90-124` |
| Graph checks: instance counts, per-type edge counts, unique transition names. **No reachability, orphan or cycle checks** (cycles allowed). | `workflow_graph.py:331-358` |
| Validate: `POST /workflow/{id}/validate` → `{is_valid, errors:[{kind:node\|edge\|workflow, id, field, message}]}`; 422 on failure. | `routes/workflow.py:403-465`, `services/workflow/errors.py` |
| Prompt templates: `{{x}}`, `{{initial_context.x}}`, `{{x \| default}}`, `{{x \| fallback:v}}`, built-ins `current_time[_TZ]`, `current_weekday[_TZ]`. Rendered against initial context only. | `utils/template_renderer.py:14,131-203`, `pipecat_engine.py:416-419` |
| `workflow_configurations`: `call_dispositions` (≤50, code `^[A-Za-z][A-Za-z0-9_-]*$`), `max_call_duration` (s, ≤1200, default 300), `max_user_idle_timeout` (10.0), `turn_start_strategy` default\|min_words, `turn_start_min_words`, `turn_stop_strategy` transcription\|turn_analyzer, `smart_turn_stop_secs`, `dictionary` (STT key terms), `ambient_noise_configuration{enabled,volume}`, `model_configuration_override`, plus out-of-schema `voicemail_detection{…, voicemail_action: hangup\|leave_message, voicemail_message}`. | `schemas/workflow_configurations.py:125-180`, `run_pipeline.py:960`, `schemas/answer_supervisor.py:36-80` |
| Tool categories include `transfer_call`/`transfer_agent`. | `schemas/tool.py:30-39` |
| Runtime UI conventions: numeric string node ids; edge id `${source}-${target}`; saved shape `{nodes, edges, viewport}`; dagre layout 350×120, nodesep 400, ranksep 300. | `ui/src/lib/utils.ts:17-24`, `useWorkflowState.ts:508-516`, `layoutNodes.ts` |

## Decisions

1. **Two authoring modes on one agent.** `AgentConfig.mode: "guided" | "flow"`. Guided keeps
   today's form and compiles to start → end. Flow stores an ATLAS-owned `FlowDefinition`
   (not the runtime's JSON) and compiles to it at publish. Guided → Flow conversion is
   one-click and lossless; Flow → Guided is not offered.
2. **ATLAS vocabulary** (never the runtime's): *Start* (startCall), *Step* (agentNode),
   *End* (endCall), *Global rules* (globalNode, edited as a panel, not a node), *Paths*
   (edges). Trigger, webhook and QA nodes are never shown: ATLAS injects trigger + CRM
   report webhook at compile time.
3. **Paths are explicit decisions.** Each path has a short *name* (becomes the function
   name; ATLAS derives it, shows the derived name, and enforces uniqueness per step and
   no collision with the step's actions) and a *when* sentence (condition). Optional
   *say while moving* (transition speech, text only).
4. **Our validation is stricter than the runtime's**: unreachable steps and ends are
   errors; a step with no outgoing path is an error unless it is an End; cycles are
   allowed but shown as warnings; empty prompt/name/path text errors are reported with
   the ATLAS node/path id and field. Publish still runs the runtime's validation and
   maps its `{kind,id,field}` errors back to ATLAS ids.
5. **Captured data** (extraction) is configured per step and on End. The editor explains
   that captured values feed outcomes and the CRM, not later prompts.
6. **Call settings** tab (agent level): max call length, silence hang-up, turn-taking
   sensitivity, STT key terms, voicemail handling (hang up / leave message + text),
   background ambience. Mapped 1:1 to `workflow_configurations`.
7. **No human transfer.** `transfer_call` / `transfer_agent` tools are never created or
   exposed by ATLAS.
8. **Layout**: ATLAS stores node positions; "Tidy up" runs dagre (LR) with the runtime's
   proven spacing. Node ids are ATLAS UUID strings; compile maps them to runtime ids
   deterministically (`n1`, `n2`, …) and keeps a map for error translation.

## Consequences
- The flow canvas needs `@xyflow/react` (route-chunked) and `@dagrejs/dagre`.
- `workflow_builder.py` gains `compile_workflow` (flow → runtime graph + id maps); the guided path is
  reimplemented as a two-node flow so both modes share one compiler.
- Contract tests publish a multi-step flow to the live runtime (`test_runtime_live.py`).
