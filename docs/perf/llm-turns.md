# Language model per turn: Oracle CPU vs a GPU

Measured 2026-10-20 with [`infra/scripts/bench_llm_turns.py`](../../infra/scripts/bench_llm_turns.py):
the demo agent's real first request (system prompt with the opener rule, greeting, the
`wrap_up` tool; [llm-turns-demo.json](llm-turns-demo.json)), then the automated caller's 4
lines, the model's own replies kept in the history so the prompt cache behaves as in a call.
3 rounds, temperature 0.3, Ollama 0.40.6 on both machines. "First sentence" is when the voice
can start speaking (the reply's first `.`, `!` or `?`).

| Where | Model | First token p50 / p90 | First sentence p50 / p90 | Whole reply p50 |
|---|---|---:|---:|---:|
| Oracle VM.Standard.E2.4 (EPYC 7551, 4 threads), alone | qwen2.5:1.5b | 774 / 1009 ms | 838 / 1083 ms | 1372 ms |
| Kaggle notebook, Tesla T4 | qwen2.5:1.5b | **26 / 34 ms** | **35 / 60 ms** | 137 ms |
| Kaggle notebook, Tesla T4 | qwen2.5:3b | 36 / 40 ms | 56 / 80 ms | 186 ms |
| Kaggle notebook, Tesla T4 | qwen2.5:7b | 58 / 67 ms | 84 / 91 ms | 444 ms |

Raw results: [llm-turns-oracle-e2.json](llm-turns-oracle-e2.json),
[llm-turns-kaggle-t4.json](llm-turns-kaggle-t4.json). On the T4 the first measured turn of
qwen2.5:1.5b took 13.5 s to its first sentence (first token 36 ms): a one-off stall right after
the model loaded, left in the data. The GPU run was a private Kaggle notebook with the model on
127.0.0.1 only; nothing was exposed and no call went through it.

In calls the CPU model is slower than alone (first sentence about 1.1 s instead of 0.84 s,
[oracle-latency.md](oracle-latency.md)), because speech-to-text, the voice and the voice service
share the same 4 cores.

## What a GPU would give a call (estimate, not measured end to end)

Taking the measured call waterfall with live speech-to-text, the turn analyzer and temperature
0.3 (reply heard p50 1.94 s, [oracle-turn-settings.json](oracle-turn-settings.json)) and
replacing the in-call language-model stage (LLM request to first sentence, about 1.1 s) with
the T4's first sentence plus about 30 ms to reach a GPU in the same region:

| Model on a GPU | Reply heard p50, estimated |
|---|---:|
| qwen2.5:1.5b | about 0.9 s |
| qwen2.5:7b (follows instructions better) | about 0.95 s |

What is then left (p50, measured): 0.56 s until the final transcript (0.2 s of silence that
ends speech, then Parakeet on the CPU), 0.13 s turn analyzer, about 0.15 s from the first
audio to the caller's ear. Getting near **0.5 s** would also need speech-to-text on the GPU
(final text in tens of ms instead of about 0.35 s) and the caller close to the server's region;
the 0.2 s end-of-speech silence and the playout stay.

Kaggle is for measurements like this one, not for serving calls: sessions are limited (30 GPU
hours a week, 12 hours each) and its acceptable-use policy does not allow running a service's
backend there.
