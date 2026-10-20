# Live speech-to-text (Deepgram v1 live-listen protocol) on sherpa-onnx, CPU only.
# Build: docker compose --profile local-ai build local-stt
FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STT_MODEL_DIR=/models

RUN useradd --create-home --uid 1000 app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --only-binary=:all: -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /srv
COPY fetch_model.py app.py /srv/

# The model chosen with STT_MODEL (about 0.7 GB for the default) and the voice activity
# detector are downloaded on first start into this directory, pinned to a commit and
# checked by SHA-256; mount a volume here so they are kept between restarts.
RUN mkdir -p /models && chown app:app /models
USER app
VOLUME ["/models"]

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=600s --retries=5 \
  CMD python -c "import json,urllib.request;r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health'));exit(0 if r['status']=='ok' else 1)"
CMD ["python", "app.py"]
