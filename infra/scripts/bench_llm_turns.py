#!/usr/bin/env python3
"""Replay a voice agent's conversation against an OpenAI-compatible model server and time
each turn the way a call experiences it. Standard library only.

Each round sends the agent's real first request (system prompt, greeting, tools), then one
caller line per turn with the model's own earlier replies in the history, so the server's
prompt cache behaves as in a call. Per turn it records the time to the first token, to the
end of the first sentence (when the voice can start speaking) and to the end of the reply.

  python infra/scripts/bench_llm_turns.py --base-url http://127.0.0.1:11434/v1 \
      --requests turns.json --model qwen2.5:1.5b --rounds 3 --json out.json

turns.json: {"messages": [system, assistant greeting], "tools": [...], "caller_lines": [...]}
(`messages` and `tools` as the first request of a call; see docs/perf/llm-turns.md).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import time
import urllib.request

SENTENCE_END = re.compile(r"[.!?](\s|$)")


def stream_turn(base: str, model: str, messages: list[dict], tools: list[dict], temperature: float,
                max_tokens: int, token: str | None) -> dict:
    body = {"model": model, "messages": messages, "stream": True, "temperature": temperature,
            "max_tokens": max_tokens, "stream_options": {"include_usage": True}}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    t0 = time.perf_counter()
    ttfb = first_sentence = None
    text, tool_call, usage = "", False, {}
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.strip()
            if not line.startswith(b"data: ") or line == b"data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content") or ""
                if delta.get("tool_calls"):
                    tool_call = True
                if (piece or delta.get("tool_calls")) and ttfb is None:
                    ttfb = time.perf_counter() - t0
                text += piece
                if first_sentence is None and SENTENCE_END.search(text):
                    first_sentence = time.perf_counter() - t0
    total = time.perf_counter() - t0
    ms = lambda s: None if s is None else round(s * 1000)  # noqa: E731
    return {"ttfb_ms": ms(ttfb), "first_sentence_ms": ms(first_sentence if first_sentence is not None else total),
            "total_ms": ms(total), "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"), "tool_call": tool_call, "text": text.strip()}


def pct(values: list[float], q: int) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    return round(statistics.quantiles(vals, n=100, method="inclusive")[q - 1])


def run_model(args: argparse.Namespace, spec: dict, model: str) -> dict:
    prefix, tools, lines = spec["messages"], spec.get("tools") or [], spec["caller_lines"]
    # Warm-up, as the voice service's connect-time warm-up: loads the model, caches the prefix.
    stream_turn(args.base_url, model, prefix + [{"role": "user", "content": "Hello?"}], tools,
                args.temperature, 1, args.token)
    turns = []
    for rnd in range(1, args.rounds + 1):
        history = list(prefix)
        for i, line in enumerate(lines, 1):
            history.append({"role": "user", "content": line})
            res = stream_turn(args.base_url, model, history, tools, args.temperature, args.max_tokens, args.token)
            history.append({"role": "assistant", "content": res["text"]})
            turns.append({"round": rnd, "turn": i, **res})
            print(f"  {model} r{rnd} t{i}: first token {res['ttfb_ms']} ms, first sentence "
                  f"{res['first_sentence_ms']} ms, reply {res['total_ms']} ms | {res['text'][:70]!r}", flush=True)
    summary = {k: {"p50": pct([t[k] for t in turns], 50), "p90": pct([t[k] for t in turns], 90)}
               for k in ("ttfb_ms", "first_sentence_ms", "total_ms")}
    return {"summary": summary, "turns": turns}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base-url", required=True, help="OpenAI-compatible base, e.g. http://127.0.0.1:11434/v1")
    p.add_argument("--requests", required=True, help="JSON with messages, tools and caller_lines")
    p.add_argument("--model", action="append", required=True, help="repeat to compare models")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--temperature", type=float, default=0.3)
    p.add_argument("--max-tokens", type=int, default=120)
    p.add_argument("--label", default="", help="where it ran, for the result file")
    p.add_argument("--token", default=None, help="bearer token, if the server needs one")
    p.add_argument("--json", default=None, help="write the results here")
    args = p.parse_args()
    with open(args.requests, encoding="utf-8") as f:
        spec = json.load(f)
    out = {"measured": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "label": args.label,
           "rounds": args.rounds, "temperature": args.temperature, "models": {}}
    for model in args.model:
        print(f"== {model}", flush=True)
        out["models"][model] = run_model(args, spec, model)
        s = out["models"][model]["summary"]
        print(f"   p50: first token {s['ttfb_ms']['p50']} ms, first sentence {s['first_sentence_ms']['p50']} ms, "
              f"reply {s['total_ms']['p50']} ms", flush=True)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
