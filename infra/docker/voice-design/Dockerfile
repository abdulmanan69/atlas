# OpenAI-compatible speech server for Qwen3-TTS: voices designed from a description, voices
# cloned from a recording.
# NVIDIA GPU: the host needs the driver (CUDA 12.4 or newer) and the NVIDIA Container Toolkit;
# the CUDA libraries come with the pinned torch wheels, so no CUDA base image is needed. Runs on
# a CPU too, many times slower than real time.
# Build: docker build -t atlas/voice-design:qwen3-tts-0.1.1 infra/docker/voice-design
FROM python:3.12.16-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/app \
    HF_HOME=/home/app/.cache/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TQDM_DISABLE=1 \
    DO_NOT_TRACK=1 \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

RUN useradd --create-home --uid 1000 app

# Every package pinned, installed without resolving dependencies (requirements.txt says why).
# torch 2.6.0 from PyPI is the CUDA 12.4 build (about 3 GB with its nvidia-* libraries).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-deps -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /srv
COPY app.py /srv/app.py

# Model weights (about 7 GB for both models, downloaded on first start) and the voices
# (voices/, the built-in one included) are kept in this directory; mount a volume here so they
# survive restarts.
RUN mkdir -p /home/app/.cache/huggingface && chown -R app:app /home/app
USER app
VOLUME ["/home/app/.cache"]

EXPOSE 8000
# The first start downloads the weights and designs the built-in voice; every start loads both
# models, warms up and makes the reply openers (a few minutes on a T4, far longer on a CPU).
HEALTHCHECK --interval=15s --timeout=5s --start-period=900s --retries=5 \
  CMD python -c "import json,urllib.request;r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health'));exit(0 if r['status']=='ok' else 1)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
