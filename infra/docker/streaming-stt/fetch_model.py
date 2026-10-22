"""Speech models the live transcription server can run, and their pinned, checksummed download.

Choose one with STT_MODEL (default below). Every file is fetched from a fixed commit of a
Hugging Face repository (or a fixed release asset) and checked against its SHA-256 before
it is used; a file that does not match is deleted and the server refuses to start.

Models (licences checked at the source, see docs/local-models.md):

  parakeet-110m     NVIDIA Parakeet TDT-CTC 110M (transducer head), int8. Not streaming:
                    the server cuts the audio at pauses (voice activity detection) and
                    transcribes each utterance when the caller pauses; live words come
                    from re-reading the growing utterance. Punctuation and capitals.
                    CC BY 4.0. Default: the most accurate on real callers' speech in
                    ATLAS's tests, and the final text about 0.2 s after Finalize on CPU.
  nemotron-en-560   NVIDIA Nemotron Speech Streaming EN 0.6B, cache-aware streaming
                    FastConformer RNN-T, 560 ms chunks, int8. Truly streaming, but about
                    0.8 s of CPU time per second of speech on 2 threads, and about 1 GB
                    of memory. NVIDIA Open Model License (commercial use allowed).
  nemotron-en-160   The same model exported with 160 ms chunks.
  parakeet-tdt-v2   NVIDIA Parakeet TDT 0.6B v2, int8, used like parakeet-110m. More
                    accurate on benchmarks, about 1 s per utterance on CPU. CC BY 4.0.
  parakeet-0.6b-v3  NVIDIA Parakeet TDT 0.6B v3, used like parakeet-110m. The most
                    accurate on South Asian English and phone audio in ATLAS's GPU tests
                    (docs/perf/gpu-model-eval.md); for a GPU (STT_PROVIDER=cuda, the image
                    from Dockerfile.gpu). int8 weights on CPU (about 0.7 s per 5 s of speech
                    on 2 threads: too slow for calls); with STT_PROVIDER=cuda the
                    full-precision export of the same model (CUDA_VARIANTS). CC BY 4.0.
  zipformer-en      k2-fsa streaming Zipformer (LibriSpeech only), int8. Apache 2.0. The
                    first model: fast, but it failed on real callers (docs/local-models.md).

The voice activity detector (Silero VAD, MIT) is fetched the same way for every model.

Where the model runs: STT_PROVIDER, `cpu` (default) or `cuda` (an NVIDIA GPU; needs the
sherpa-onnx CUDA build of Dockerfile.gpu).

Run directly to pre-fetch: `python fetch_model.py` (uses STT_MODEL, STT_PROVIDER and
STT_MODEL_DIR).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("streaming_stt.fetch")

DEFAULT_MODEL = "parakeet-110m"
PROVIDERS = ("cpu", "cuda")
DEFAULT_PROVIDER = "cpu"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    key: str
    model_id: str  # name reported in results and /health
    kind: str  # "online" (streaming transducer) | "offline" (whole-utterance transducer)
    arch: str
    repo: str
    revision: str
    # role (encoder, decoder, joiner, tokens) -> (file name in the repository or the
    # archive, sha256 of that file)
    files: dict[str, tuple[str, str]]
    licence: str
    cased: bool  # the model writes capitals and punctuation itself
    tail_pad_s: float = 0.2  # silence fed before input_finished() on a final (online)
    min_confidence: float = 0.0  # text below this mean token probability is dropped
    model_type: str = ""  # sherpa-onnx model_type for offline transducers
    # Models published only as a release archive: its URL and SHA-256 (then `repo` is the
    # archive's name and `revision` its checksum, for the version string).
    archive: str = ""
    archive_sha256: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/{self.revision}"

    @property
    def version(self) -> str:
        return f"{self.repo.split('/')[-1]}@{self.revision[:8]}"


MODELS: dict[str, ModelSpec] = {
    "nemotron-en-560": ModelSpec(
        key="nemotron-en-560",
        model_id="nemotron-streaming-en",
        kind="online",
        arch="fastconformer-rnnt",
        repo="csukuangfj2/sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25",
        revision="52056fdc070914a48dcd68b31b44d6a6f5b85902",
        files={
            "encoder": (
                "encoder.int8.onnx",
                "7d932213491ad355c6e5576705dc3494731a52af87d7a1b954559340147909d8",
            ),
            "decoder": (
                "decoder.int8.onnx",
                "0be9702c2f427a2b6bb241d298e0d3836a558de1f5b9fd3018f1cce6e2b3fa98",
            ),
            "joiner": (
                "joiner.int8.onnx",
                "a35eac38a22ebceb04d230ed7afe0d68f446ba6914a036b97f14fece95967e23",
            ),
            # Plain (non-LFS) file, so the Hub lists no hash: computed from the file.
            "tokens": (
                "tokens.txt",
                "dc0b4584ab2e4ddbf888425c076c61b736e7356a015250db7d307e6f1a8188ff",
            ),
        },
        licence="NVIDIA Open Model License",
        cased=True,
        # With 0.2 s the last 560 ms chunk was cut short through the voice service's client
        # and most utterances lost their last word (docs/local-models.md).
        tail_pad_s=0.6,
    ),
    "nemotron-en-160": ModelSpec(
        key="nemotron-en-160",
        model_id="nemotron-streaming-en-160ms",
        kind="online",
        arch="fastconformer-rnnt",
        repo="csukuangfj2/sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-int8-2026-04-25",
        revision="237e551abd7a411ef92d3595454d9f6ab5fe7d6c",
        files={
            "encoder": (
                "encoder.int8.onnx",
                "71111f61b18e1e65e01e369434a5c0434868d2f44892742ae54240600c681209",
            ),
            "decoder": (
                "decoder.int8.onnx",
                "0be9702c2f427a2b6bb241d298e0d3836a558de1f5b9fd3018f1cce6e2b3fa98",
            ),
            "joiner": (
                "joiner.int8.onnx",
                "a35eac38a22ebceb04d230ed7afe0d68f446ba6914a036b97f14fece95967e23",
            ),
            "tokens": (
                "tokens.txt",
                "dc0b4584ab2e4ddbf888425c076c61b736e7356a015250db7d307e6f1a8188ff",
            ),
        },
        licence="NVIDIA Open Model License",
        cased=True,
        # With 0.2 s the last 560 ms chunk was cut short through the voice service's client
        # and most utterances lost their last word (docs/local-models.md).
        tail_pad_s=0.6,
    ),
    "parakeet-tdt-v2": ModelSpec(
        key="parakeet-tdt-v2",
        model_id="parakeet-tdt-en",
        kind="offline",
        arch="fastconformer-tdt",
        repo="csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
        revision="1ab9323565ddb038682214b292f588070a538ce2",
        files={
            "encoder": (
                "encoder.int8.onnx",
                "a32b12d17bbbc309d0686fbbcc2987b5e9b8333a7da83fa6b089f0a2acd651ab",
            ),
            "decoder": (
                "decoder.int8.onnx",
                "b6bb64963457237b900e496ee9994b59294526439fbcc1fecf705b31a15c6b4e",
            ),
            "joiner": (
                "joiner.int8.onnx",
                "7946164367946e7f9f29a122407c3252b680dbae9a51343eb2488d057c3c43d2",
            ),
            "tokens": (
                "tokens.txt",
                "ec182b70dd42113aff6c5372c75cac58c952443eb22322f57bbd7f53977d497d",
            ),
        },
        licence="CC BY 4.0",
        cased=True,
        model_type="nemo_transducer",
    ),
    "parakeet-0.6b-v3": ModelSpec(
        key="parakeet-0.6b-v3",
        model_id="parakeet-en-0.6b-v3",
        kind="offline",
        arch="fastconformer-tdt",
        # The sherpa-onnx maintainers' export of nvidia/parakeet-tdt-0.6b-v3.
        repo="csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
        revision="2bda32ec70b097a55adaa07d9a7173915b43cc78",
        files={
            "encoder": (
                "encoder.int8.onnx",
                "acfc2b4456377e15d04f0243af540b7fe7c992f8d898d751cf134c3a55fd2247",
            ),
            "decoder": (
                "decoder.int8.onnx",
                "179e50c43d1a9de79c8a24149a2f9bac6eb5981823f2a2ed88d655b24248db4e",
            ),
            "joiner": (
                "joiner.int8.onnx",
                "3164c13fc2821009440d20fcb5fdc78bff28b4db2f8d0f0b329101719c0948b3",
            ),
            # Plain (non-LFS) file, so the Hub lists no hash: computed from the file.
            "tokens": (
                "tokens.txt",
                "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d",
            ),
        },
        licence="CC BY 4.0",
        cased=True,
        model_type="nemo_transducer",
    ),
    "parakeet-0.6b-v3-fp32": ModelSpec(
        key="parakeet-0.6b-v3-fp32",
        model_id="parakeet-en-0.6b-v3",
        kind="offline",
        arch="fastconformer-tdt",
        # The same model in full precision (the encoder's weights are a separate file).
        repo="csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3",
        revision="1a468a35cbba69418f126de829e75261dea4a4e4",
        files={
            "encoder": (
                "encoder.onnx",
                "3eed7ce424bf8339ad09233533c687e2dbd07e74ccf5027b5e7344019ea373b0",
            ),
            # Read by ONNX Runtime from the encoder's directory, not passed to sherpa-onnx.
            "encoder_weights": (
                "encoder.weights",
                "3af3f51af5f2d01dbbf5af47d42c7962a2c205f11004254bb4f2b979862f39a8",
            ),
            "decoder": (
                "decoder.onnx",
                "d593cdb0e571f5a457ec2219af9968cbf6b0e8198e8f7839b40a8754593bf68c",
            ),
            "joiner": (
                "joiner.onnx",
                "b9b0bcf88ac571902e69a6536223ed2d94885e981b85045410f1403d53121a63",
            ),
            "tokens": (
                "tokens.txt",
                "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d",
            ),
        },
        licence="CC BY 4.0",
        cased=True,
        model_type="nemo_transducer",
    ),
    "parakeet-110m": ModelSpec(
        key="parakeet-110m",
        model_id="parakeet-en-110m",
        kind="offline",
        arch="fastconformer-tdt",
        # Published as int8 only in the sherpa-onnx release (the Hugging Face copy of the
        # int8 export is empty), so the archive itself is pinned by its SHA-256.
        repo="k2-fsa/sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8",
        revision="f628312e9fdf8686374cb01a69425c41732529d540860311f16f37cbc32cfe9b",
        archive=(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8.tar.bz2"
        ),
        archive_sha256="f628312e9fdf8686374cb01a69425c41732529d540860311f16f37cbc32cfe9b",
        # Measured on the caller recordings: clear English 0.71 and up (median 0.9; one
        # exception, transcribed wrongly anyway); other languages, mumbling and noise
        # mostly below 0.6 (docs/local-models.md).
        min_confidence=0.6,
        files={
            "encoder": (
                "encoder.int8.onnx",
                "0f35509ddeb9b39002fb077d979a9fe74f06eb0bc4dd5c34f512f82e5111d657",
            ),
            "decoder": (
                "decoder.int8.onnx",
                "f7c331c5504c2e593c76ed22b728e3f554af6c4a383dde862e719ced08b1da19",
            ),
            "joiner": (
                "joiner.int8.onnx",
                "bf7dff69e9f2cdbe9943d70da358f38b361c115ba0105bae7e908e0d6ec782f6",
            ),
            "tokens": (
                "tokens.txt",
                "450e56bd2f036fe5b6aa821865838cc5aa9d8b0106134ce9a9ba0664abe6cd10",
            ),
        },
        licence="CC BY 4.0",
        cased=True,
        model_type="nemo_transducer",
    ),
    "zipformer-en": ModelSpec(
        key="zipformer-en",
        model_id="zipformer-en-streaming",
        kind="online",
        arch="zipformer",
        repo="csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26",
        revision="672fbf1b30579d6585301139bb363f42a0ad4a24",
        # int8 encoder/joiner (fast on CPU, small accuracy cost); the decoder is tiny, so
        # the float version is kept for accuracy.
        files={
            "encoder": (
                "encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
                "563fde436d16cf7607cf408cd6b30909819d03162652ef389c2450ced3f45ac1",
            ),
            "decoder": (
                "decoder-epoch-99-avg-1-chunk-16-left-128.onnx",
                "7bf787f90b194b307e5a4ad6a34fadb4e748304c35f78a8d66358a05b13ee6ef",
            ),
            "joiner": (
                "joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
                "d944208d660d67c8d72cd2acaeac971fa5ceb8c80e76c1968148846fedd6e297",
            ),
            "tokens": (
                "tokens.txt",
                "49e3c2646595fd907228b3c6787069658f67b17377c60aeb8619c4551b2316fb",
            ),
        },
        licence="Apache-2.0",
        cased=False,
        # The model emits a word's last sound up to about half a second late; with less
        # padding the last letter of a sentence was measured spilling into the next turn.
        tail_pad_s=0.8,
    ),
}

# Silero VAD as published by the sherpa-onnx maintainers (MIT, snakers4/silero-vad).
VAD_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
VAD_FILE = "silero_vad.onnx"
VAD_SHA256 = "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6"

# Files of the first release, stored directly in the model directory; removed once a model
# is kept in its own sub-directory, so the volume does not hold two copies.
_LEGACY_FILES = [name for name, _ in MODELS["zipformer-en"].files.values()]


# STT_PROVIDER=cuda runs these models from another export: int8 weights are made for CPUs.
# Measured on a T4 (whole clips, no server): Parakeet 0.6B v3 int8 decodes 5 s in 731 ms on
# the GPU against 61 ms in full precision, and misses more words of accented speech (EdAcc
# South Asian 14.8 % against 12.0 %; full precision matches NVIDIA's own model).
CUDA_VARIANTS = {"parakeet-0.6b-v3": "parakeet-0.6b-v3-fp32"}


def provider_from_env() -> str:
    provider = os.environ.get("STT_PROVIDER", DEFAULT_PROVIDER).strip().lower() or DEFAULT_PROVIDER
    if provider not in PROVIDERS:
        raise SystemExit(f"STT_PROVIDER={provider!r} is not one of: {', '.join(PROVIDERS)}")
    return provider


def spec_from_env() -> ModelSpec:
    key = os.environ.get("STT_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if key not in MODELS:
        raise SystemExit(f"STT_MODEL={key!r} is not one of: {', '.join(MODELS)}")
    if provider_from_env() == "cuda":
        key = CUDA_VARIANTS.get(key, key)
    return MODELS[key]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fetch(url: str, target: Path, expected: str) -> None:
    if target.exists() and _sha256(target) == expected:
        return
    logger.info("Downloading %s", url)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - fixed https URL
            for block in iter(lambda: resp.read(1 << 20), b""):
                tmp.write(block)
    actual = _sha256(tmp_path)
    if actual != expected:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Checksum mismatch for {target.name}: expected {expected}, got {actual}"
        )
    tmp_path.replace(target)


def _fetch_from_archive(spec: ModelSpec, target_dir: Path) -> None:
    """Download the release archive once, check it, and keep only the files listed."""
    with tempfile.TemporaryDirectory(dir=target_dir) as tmp:
        archive = Path(tmp) / "model.tar.bz2"
        _fetch(spec.archive, archive, spec.archive_sha256)
        with tarfile.open(archive, "r:bz2") as tar:
            # Members are "./<archive name>/<file>": index them by <file>.
            members = {
                m.name.removeprefix("./").split("/", 1)[-1]: m
                for m in tar.getmembers()
                if m.isfile()
            }
            for name, expected in spec.files.values():
                member = members.get(name)
                if member is None:
                    raise RuntimeError(f"{name} is missing from {spec.archive}")
                source = tar.extractfile(member)
                assert source is not None
                part = Path(tmp) / Path(name).name
                with source, part.open("wb") as out:  # streamed: no path from the archive
                    shutil.copyfileobj(source, out, 1 << 20)
                actual = _sha256(part)
                if actual != expected:
                    raise RuntimeError(
                        f"Checksum mismatch for {name}: expected {expected}, got {actual}"
                    )
                part.replace(target_dir / Path(name).name)


def ensure_model(model_dir: Path, spec: ModelSpec) -> dict[str, Path]:
    """Fetch any missing or corrupt file; return role -> path (plus "vad"). Roles other than
    encoder, decoder, joiner and tokens are data files the model reads by itself.

    Raises on a bad download.
    """
    model_dir.mkdir(parents=True, exist_ok=True)
    for name in _LEGACY_FILES:
        (model_dir / name).unlink(missing_ok=True)
    target_dir = model_dir / spec.key
    target_dir.mkdir(exist_ok=True)
    paths = {role: target_dir / Path(name).name for role, (name, _) in spec.files.items()}
    if spec.archive:
        missing = [
            role
            for role, (_, expected) in spec.files.items()
            if not paths[role].exists() or _sha256(paths[role]) != expected
        ]
        if missing:
            _fetch_from_archive(spec, target_dir)
    else:
        for role, (name, expected) in spec.files.items():
            _fetch(f"{spec.base_url}/{name}", paths[role], expected)
    paths["vad"] = model_dir / VAD_FILE
    _fetch(VAD_URL, paths["vad"], VAD_SHA256)
    return paths


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_model(Path(os.environ.get("STT_MODEL_DIR", "/models")), spec_from_env())
    sys.exit(0)
