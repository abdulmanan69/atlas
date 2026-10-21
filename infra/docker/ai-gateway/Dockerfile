# Health service of the AI gateway (app.py): Python standard library only, no packages.
# Build: docker compose build ai-health (with infra/deploy/oci/docker-compose.ai-gateway.yml)
# Same base image as the Pocket TTS and live speech-to-text images, so its layers are shared.
FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv
COPY app.py /srv/app.py
USER nobody

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/livez', timeout=4)"
CMD ["python", "app.py"]
