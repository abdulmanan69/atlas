# Third-Party Notices

This product includes software developed by third parties. The licenses below
must be retained in all source and binary distributions.

## Voice runtime engine

The real-time voice agent runtime used by this platform (deployed as the
internal "voice runtime" service, source pinned under `vendor/voice-runtime`)
is distributed under the BSD 2-Clause License:

```
BSD 2-Clause License

Copyright (c) 2025, Zansat Technologies Private Limited

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

The runtime itself bundles further open-source components (including the
Pipecat framework, BSD 2-Clause). Their notices ship inside the runtime's own
distribution and container images.

## Self-hosted speech models (optional `local-stt` server)

The live transcription server (`infra/docker/streaming-stt`) downloads one speech model
and a voice activity detector on first start; none of them is stored in this repository.
Details and the measurements behind the default: `docs/local-models.md`.

- **Parakeet TDT-CTC 110M** (default, `STT_MODEL=parakeet-110m`), by NVIDIA, licensed
  under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
  ([nvidia/parakeet-tdt_ctc-110m](https://huggingface.co/nvidia/parakeet-tdt_ctc-110m)).
  Used as the int8 ONNX export published by the sherpa-onnx project; ATLAS does not
  modify the model.
- **Silero VAD**, MIT License, Copyright (c) 2020-present Silero Team
  ([snakers4/silero-vad](https://github.com/snakers4/silero-vad)), as published by the
  sherpa-onnx project.
- **sherpa-onnx** (Apache License 2.0, [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)),
  which bundles ONNX Runtime (MIT).
- Optional alternatives, only downloaded when selected:
  - Nemotron Speech Streaming EN 0.6B (`nemotron-en-560`, `nemotron-en-160`): Licensed by
    NVIDIA Corporation under the NVIDIA Open Model License
    ([agreement](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/)).
  - Parakeet TDT 0.6B v2 (`parakeet-tdt-v2`), by NVIDIA, CC BY 4.0.
  - Streaming Zipformer EN 2023-06-26 (`zipformer-en`), k2-fsa, Apache License 2.0.

## Application dependencies

Python and JavaScript dependencies are listed with exact versions in
`services/api/uv.lock` and `apps/web/pnpm-lock.yaml`. A full license report can
be generated with `uv run pip-licenses` and `pnpm licenses list`.
