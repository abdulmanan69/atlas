# OpenAI-compatible speech server for Resemble AI Chatterbox-Turbo, with sound tags.
# NVIDIA GPU: the host needs the driver (CUDA 12.4 or newer) and the NVIDIA Container Toolkit;
# the CUDA libraries come with the pinned torch wheels, so no CUDA base image is needed. Runs on
# a CPU too, several times slower than real time.
# Build: docker build -t atlas/expressive-tts:chatterbox-turbo-0.1.7 infra/docker/expressive-tts
FROM python:3.13.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/app \
    HF_HOME=/home/app/.cache/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TQDM_DISABLE=1 \
    DO_NOT_TRACK=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

RUN useradd --create-home --uid 1000 app

# Every package pinned, installed without resolving dependencies (requirements.txt says why).
# torch 2.6.0 from PyPI is the CUDA 12.4 build (about 3 GB with its nvidia-* libraries).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-deps -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /srv
COPY app.py /srv/app.py

# Model weights (about 3 GB, downloaded on first start) and cloned voices (voices/) are kept
# in this directory; mount a volume here so they survive restarts.
RUN mkdir -p /home/app/.cache/huggingface && chown -R app:app /home/app
USER app
VOLUME ["/home/app/.cache"]

EXPOSE 8000
# The first start downloads the weights; every start loads and warms the model and makes the
# reply openers (under a minute on a T4, several minutes on a CPU).
HEALTHCHECK --interval=15s --timeout=5s --start-period=600s --retries=5 \
  CMD python -c "import json,urllib.request;r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health'));exit(0 if r['status']=='ok' else 1)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
