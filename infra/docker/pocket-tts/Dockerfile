# OpenAI-compatible speech server for Kyutai Pocket TTS, CPU only.
# Build: docker compose --profile local-ai build local-tts
FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/app \
    HF_HOME=/home/app/.cache/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1

RUN useradd --create-home --uid 1000 app

# CPU build of torch from the PyTorch index: avoids several GB of CUDA wheels.
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.14.0+cpu"
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /srv
COPY app.py /srv/app.py

# Model weights and voice embeddings are downloaded on first start into this
# directory; mount a volume here so they are kept between restarts.
RUN mkdir -p /home/app/.cache/huggingface && chown -R app:app /home/app
USER app
VOLUME ["/home/app/.cache"]

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=5 \
  CMD python -c "import json,urllib.request;r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health'));exit(0 if r['status']=='ok' else 1)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
