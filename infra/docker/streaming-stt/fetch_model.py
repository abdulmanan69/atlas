"""Download the streaming speech model into the cache volume, pinned and checksummed.

The model is k2-fsa's `sherpa-onnx-streaming-zipformer-en-2023-06-26` (Apache-2.0 on its
model card), published by the sherpa-onnx maintainers both as a release archive
(https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2)
and as a Hugging Face repository. Only the four files the server needs are fetched from
the repository, at a fixed commit, and each is checked against its SHA-256: about 75 MB
instead of the 300+ MB archive.

Run directly to pre-fetch: `python fetch_model.py` (uses STT_MODEL_DIR).
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

logger = logging.getLogger("streaming_stt.fetch")

REPO = "csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26"
REVISION = "672fbf1b30579d6585301139bb363f42a0ad4a24"
BASE_URL = f"https://huggingface.co/{REPO}/resolve/{REVISION}"

# file name -> sha256. int8 encoder/joiner (fast on CPU, small accuracy cost); the
# decoder is tiny, so the float version is kept for accuracy.
FILES: dict[str, str] = {
    "encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx": (
        "563fde436d16cf7607cf408cd6b30909819d03162652ef389c2450ced3f45ac1"
    ),
    "decoder-epoch-99-avg-1-chunk-16-left-128.onnx": (
        "7bf787f90b194b307e5a4ad6a34fadb4e748304c35f78a8d66358a05b13ee6ef"
    ),
    "joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx": (
        "d944208d660d67c8d72cd2acaeac971fa5ceb8c80e76c1968148846fedd6e297"
    ),
    # Plain (non-LFS) file, so the Hub lists no hash: computed from the file at REVISION.
    "tokens.txt": "49e3c2646595fd907228b3c6787069658f67b17377c60aeb8619c4551b2316fb",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_model(model_dir: Path) -> Path:
    """Fetch any missing or corrupt file; return the directory. Raises on a bad download."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        target = model_dir / name
        if target.exists() and _sha256(target) == expected:
            continue
        url = f"{BASE_URL}/{name}"
        logger.info("Downloading %s", url)
        with tempfile.NamedTemporaryFile(dir=model_dir, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - fixed https URL
                for block in iter(lambda: resp.read(1 << 20), b""):
                    tmp.write(block)
        actual = _sha256(tmp_path)
        if actual != expected:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"Checksum mismatch for {name}: expected {expected}, got {actual}")
        tmp_path.replace(target)
    return model_dir


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_model(Path(os.environ.get("STT_MODEL_DIR", "/models")))
    sys.exit(0)
