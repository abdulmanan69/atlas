#!/usr/bin/env sh
# Download the default small, CPU-friendly local models into their Docker volumes.
# Run after `make local-ai-up`. Safe to re-run: already-present models are skipped.
#
# Override with environment variables, e.g.
#   LLM_MODEL=llama3.2:3b STT_MODEL=Systran/faster-whisper-base sh infra/scripts/pull-local-models.sh
set -eu

LLM_MODEL="${LLM_MODEL:-qwen2.5:1.5b}"
STT_MODEL="${STT_MODEL:-Systran/faster-whisper-small}"
TTS_MODEL="${TTS_MODEL:-speaches-ai/Kokoro-82M-v1.0-ONNX}"
SPEECH_URL="${SPEECH_URL:-http://127.0.0.1:8001}"

echo "==> Language model: $LLM_MODEL"
docker compose exec -T local-llm ollama pull "$LLM_MODEL"

for model in "$STT_MODEL" "$TTS_MODEL"; do
  echo "==> Speech model: $model"
  # Speaches downloads a model when it is registered through its API.
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$SPEECH_URL/v1/models/$model")
  case "$code" in
    200|201|409) echo "    ready ($code)" ;;
    *) echo "    failed with HTTP $code" >&2; exit 1 ;;
  esac
done

echo
echo "Local models are ready. In ATLAS → Voice AI → AI providers, add a"
echo "'Self-hosted models' connection for each server:"
echo "  Language model   base URL http://local-llm:11434/v1     model $LLM_MODEL"
echo "  Speech and voice base URL http://local-speech:8000/v1   STT $STT_MODEL, voice model kokoro"
