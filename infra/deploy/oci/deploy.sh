#!/usr/bin/env bash
# Deploy or update ATLAS on one Linux server with Docker (tested: Oracle Cloud, Oracle
# Linux 9, x86_64). Idempotent: run it again after every `git push`. See README.md here.
#
#   bash infra/deploy/oci/deploy.sh [options]
#
#   --public-ip IP     the server's public IPv4 (default: .env, else asked from the internet)
#   --web-host NAME    web app + API hostname (default: atlas.<ip-with-dashes>.sslip.io)
#   --voice-host NAME  voice signalling hostname (default: voice.<ip-with-dashes>.sslip.io)
#   --preset P         pocket | streaming: apply this speed preset to the demo organisation
#                      now, replacing its current model choice. Without it, a preset is
#                      applied only on the first setup (streaming when available, else
#                      pocket); later runs keep whatever the organisation chose.
#   --no-pull          do not `git pull` first
#   --rebuild          rebuild the two model images (Pocket TTS, live transcription)
#   --skip-warm        do not load the models into memory at the end
#
# Live transcription: put a Deepgram key in the environment variable ATLAS_DEMO_DEEPGRAM_KEY
# (for example `read -rs ATLAS_DEMO_DEEPGRAM_KEY; export ATLAS_DEMO_DEEPGRAM_KEY`). It is
# used once, to save the connection to the local streaming server, and never written or
# printed. Without it the first setup uses the Pocket TTS + Whisper preset.
#
# Secrets are generated on the first run into .env (mode 600) and never printed; the demo
# password is printed once, on the run that generates it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"
ENV_FILE="$ROOT/.env"
COMPOSE_FILES="docker-compose.yml:infra/deploy/oci/docker-compose.prod.yml"
NODE_IMAGE="node:22.23.2-bookworm-slim"
LLM_MODEL="qwen2.5:1.5b"
# Whisper model of the Pocket TTS preset (used when live transcription is not available).
STT_MODEL="Systran/faster-whisper-base"
WEB_VOLUME="atlas_web_dist"
PNPM_VOLUME="atlas_pnpm_store"
MIN_FREE_GB=4

PUBLIC_IP="" WEB_HOST="" VOICE_HOST="" PRESET=auto PULL=1 REBUILD=0 SKIP_WARM=0
while [ $# -gt 0 ]; do
  case "$1" in
    --public-ip) PUBLIC_IP="${2:-}"; shift ;;
    --web-host) WEB_HOST="${2:-}"; shift ;;
    --voice-host) VOICE_HOST="${2:-}"; shift ;;
    --preset) PRESET="${2:-}"; shift ;;
    --no-pull) PULL=0 ;;
    --rebuild) REBUILD=1 ;;
    --skip-warm) SKIP_WARM=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
case "$PRESET" in auto|pocket|streaming) ;; *) echo "--preset must be auto, pocket or streaming" >&2; exit 2 ;; esac
DEEPGRAM_KEY="$(printf '%s' "${ATLAS_DEMO_DEEPGRAM_KEY:-}" | tr -d '[:space:]')"
unset ATLAS_DEMO_DEEPGRAM_KEY

step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
info() { printf '    %s\n' "$1"; }
warn() { printf '    \033[33mWARNING: %s\033[0m\n' "$1"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

# ------------------------------------------------------------------------- docker
if docker info >/dev/null 2>&1; then DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then DOCKER=(sudo -E docker)
else die "Docker is not running or this user may not use it (add it to the docker group and log in again)."
fi
dc() { "${DOCKER[@]}" compose "$@"; }
dc version >/dev/null 2>&1 || die "Docker Compose v2 is missing."

docker_root() { "${DOCKER[@]}" info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker; }
free_gb() { df -Pk "$(docker_root)" 2>/dev/null | awk 'NR==2 {print int($4/1048576)}'; }
need_disk() {  # need_disk <GB> <what>: stop unless MIN_FREE_GB stays free afterwards
  local free; free="$(free_gb)"
  [ $((free - $1)) -ge "$MIN_FREE_GB" ] || die "$2 needs about $1 GB but only $free GB are free (and $MIN_FREE_GB GB must stay free). Grow the disk (README.md, 'Disk') and re-run."
}
have_image() { "${DOCKER[@]}" image inspect "$1" >/dev/null 2>&1; }

# ------------------------------------------------------------------------- .env helpers
rand_secret() { head -c "${1:-48}" /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_'; }
fernet_key() { head -c 32 /dev/urandom | base64 | tr -d '\n' | tr '+/' '-_'; }
env_get() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n -E "s/^[[:space:]]*$1[[:space:]]*=(.*)$/\1/p" "$ENV_FILE" | tail -n 1 | sed -E "s/^[\"']//; s/[\"']$//; s/[[:space:]]+$//"
}
env_has() { [ -f "$ENV_FILE" ] && grep -q -E "^[[:space:]]*$1[[:space:]]*=" "$ENV_FILE"; }
ADDED=()
env_default() {  # env_default KEY VALUE: append only when KEY is missing; never changes a value
  env_has "$1" && return 0
  printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE"
  ADDED+=("$1")
}

# ------------------------------------------------------------------------- 1. code
step "Code"
if [ "$PULL" = 1 ] && [ -d .git ]; then
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    warn "tracked files have local changes: skipped git pull (commit or discard them, then re-run)"
  elif ! git pull --ff-only --quiet; then
    die "git pull failed. If it names untracked files that were copied here by hand, remove them (git status) and re-run."
  fi
fi
info "commit $(git rev-parse --short HEAD 2>/dev/null || echo unknown)$( [ -n "$(git status --porcelain 2>/dev/null)" ] && echo ' (with local changes)')"

# ------------------------------------------------------------------------- 2. settings
step "Settings (.env)"
[ -n "$PUBLIC_IP" ] || PUBLIC_IP="$(env_get ATLAS_SERVER_PUBLIC_IP)"
if [ -z "$PUBLIC_IP" ]; then
  PUBLIC_IP="$(curl -s -4 -m 10 https://api.ipify.org || true)"
  printf '%s' "$PUBLIC_IP" | grep -q -E '^[0-9]+(\.[0-9]+){3}$' || die "could not find the public IP: pass --public-ip"
fi
PRIVATE_IP="$(env_get ATLAS_SERVER_PRIVATE_IP)"
[ -n "$PRIVATE_IP" ] || PRIVATE_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' | head -n 1)"
[ -n "$PRIVATE_IP" ] || die "could not find the server's own IPv4 address"
DASHED="$(printf '%s' "$PUBLIC_IP" | tr . -)"
[ -n "$WEB_HOST" ] || WEB_HOST="$(env_get ATLAS_WEB_HOST)"
[ -n "$WEB_HOST" ] || WEB_HOST="atlas.$DASHED.sslip.io"
[ -n "$VOICE_HOST" ] || VOICE_HOST="$(env_get ATLAS_VOICE_HOST)"
[ -n "$VOICE_HOST" ] || VOICE_HOST="voice.$DASHED.sslip.io"
TURN_HOST="$(env_get ATLAS_TURN_HOST)"
[ -n "$TURN_HOST" ] || TURN_HOST="turn.$DASHED.sslip.io"

umask 077
if [ ! -f "$ENV_FILE" ]; then
  printf '# ATLAS production settings, written by infra/deploy/oci/deploy.sh. Secrets: never share or commit.\n' >"$ENV_FILE"
  info "created .env"
fi
chmod 600 "$ENV_FILE"
# docker compose reads these two, so `docker compose ...` in this folder needs no -f/--profile.
env_default COMPOSE_FILE "$COMPOSE_FILES"
env_default COMPOSE_PROFILES "app,voice,local-ai"
# Where it runs
env_default ATLAS_SERVER_PUBLIC_IP "$PUBLIC_IP"
env_default ATLAS_SERVER_PRIVATE_IP "$PRIVATE_IP"
env_default ATLAS_WEB_HOST "$WEB_HOST"
env_default ATLAS_VOICE_HOST "$VOICE_HOST"
env_default ATLAS_TURN_HOST "$TURN_HOST"
env_default ATLAS_VOICE_RUNTIME_IP "172.30.0.10"
# ATLAS API (services/api/app/core/config.py)
env_default ATLAS_ENVIRONMENT production
env_default ATLAS_PRODUCT_NAME ATLAS
env_default ATLAS_LOG_LEVEL INFO
env_default ATLAS_COOKIE_SECURE true
env_default ATLAS_CORS_ORIGINS "https://$WEB_HOST"
env_default ATLAS_WEB_APP_ORIGIN "https://$WEB_HOST"
env_default ATLAS_PUBLIC_API_URL ""
env_default ATLAS_ALLOW_SIGNUP false
env_default ATLAS_DOCS_ENABLED false
env_default ATLAS_LIVE_CALLS_ENABLED false
env_default ATLAS_VOICE_RUNTIME_BASE_URL "http://voice-runtime:8000"
env_default ATLAS_VOICE_RUNTIME_PUBLIC_URL "https://$VOICE_HOST"
env_default ATLAS_VOICE_RUNTIME_CALLBACK_URL "http://api:8080"
env_default ATLAS_VOICE_ALLOW_HOSTED_TRIAL_MODELS false
env_default ATLAS_ALLOW_PRIVATE_ACTION_URLS false
env_default ATLAS_ALLOW_PRIVATE_WEBHOOK_URLS false
env_default ATLAS_ALLOW_PRIVATE_MODEL_URLS false
env_default ATLAS_LOCAL_LLM_WARMUP_ENABLED true
env_default ATLAS_LOCAL_LLM_WARMUP_URL "http://local-llm:11434/v1"
env_default ATLAS_LOCAL_TTS_URL "http://local-tts:8000/v1"
env_default POCKET_TTS_HF_TOKEN ""
# Secrets: fresh random values from the OS CSPRNG, only when missing.
SECRETS_ADDED=()
for spec in ATLAS_POSTGRES_PASSWORD:32 ATLAS_JWT_SECRET:48 ATLAS_SECRETS_ENCRYPTION_KEY:fernet \
            ATLAS_VOICE_DB_PASSWORD:32 ATLAS_VOICE_STORAGE_PASSWORD:32 ATLAS_VOICE_RUNTIME_JWT_SECRET:48 \
            ATLAS_VOICE_RUNTIME_METRICS_SECRET:48 ATLAS_TURN_SECRET:48 ATLAS_DEMO_PASSWORD:15; do
  k="${spec%%:*}" n="${spec##*:}"
  env_has "$k" && [ -n "$(env_get "$k")" ] && continue
  if [ "$n" = fernet ]; then v="$(fernet_key)"; else v="$(rand_secret "$n")"; fi
  if env_has "$k"; then  # present but empty: fill in place
    tmp="$(mktemp "$ENV_FILE.XXXXXX")"
    awk -v k="$k" -v v="$v" '$0 ~ "^[[:space:]]*" k "[[:space:]]*=" { print k "=" v; next } { print }' "$ENV_FILE" >"$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$k" "$v" >>"$ENV_FILE"
  fi
  SECRETS_ADDED+=("$k")
done
chmod 600 "$ENV_FILE"
umask 022
GENERATED_PASSWORD=""
case " ${SECRETS_ADDED[*]-} " in *" ATLAS_DEMO_PASSWORD "*) GENERATED_PASSWORD="$(env_get ATLAS_DEMO_PASSWORD)" ;; esac
[ "${#ADDED[@]}" -gt 0 ] && info "settings added: ${ADDED[*]}"
[ "${#SECRETS_ADDED[@]}" -gt 0 ] && info "secrets generated (not shown): ${SECRETS_ADDED[*]}"
WEB_HOST="$(env_get ATLAS_WEB_HOST)" VOICE_HOST="$(env_get ATLAS_VOICE_HOST)"
info "web app https://$WEB_HOST, voice signalling https://$VOICE_HOST, TURN $(env_get ATLAS_TURN_HOST):3478"
[ "$(env_get ATLAS_ENVIRONMENT)" = production ] || warn "ATLAS_ENVIRONMENT is not production in .env"

# ------------------------------------------------------------------------- 3. web app
step "Web app (production build)"
have_image "$NODE_IMAGE" || need_disk 1 "the Node.js build image"
need_disk 2 "the web app build"
"${DOCKER[@]}" volume create "$WEB_VOLUME" >/dev/null
"${DOCKER[@]}" volume create "$PNPM_VOLUME" >/dev/null
"${DOCKER[@]}" run --rm \
  -e COREPACK_ENABLE_DOWNLOAD_PROMPT=0 -e CI=true \
  -v "$ROOT/apps/web:/src:ro" -v "$PNPM_VOLUME:/pnpm-store" -v "$WEB_VOLUME:/out" \
  "$NODE_IMAGE" sh -euc '
    corepack enable
    mkdir /build
    cd /src && tar --exclude=./node_modules --exclude=./dist -cf - . | tar -xf - -C /build
    cd /build
    pnpm install --frozen-lockfile --store-dir /pnpm-store --reporter=append-only >/tmp/install.log 2>&1 || { tail -n 40 /tmp/install.log; exit 1; }
    pnpm build >/tmp/build.log 2>&1 || { tail -n 60 /tmp/build.log; exit 1; }
    # New hashed bundles first, index.html last, then drop files of older builds.
    (cd dist && find . -type f ! -name index.html -exec sh -c "mkdir -p \"/out/\$(dirname \"\$1\")\" && cp \"\$1\" \"/out/\$1\"" _ {} \;)
    cp dist/index.html /out/index.html
    (cd /out && find . -type f | while read -r f; do [ -e "/build/dist/$f" ] || rm -f "$f"; done)
    find /out -mindepth 1 -type d -empty -delete
    echo "    $(find /out -type f | wc -l) files, $(du -sh /out | cut -f1)"
  ' || die "the web app build failed (see above)"

# ------------------------------------------------------------------------- 4. images
step "Images"
need_disk 2 "pulling updated images"
dc pull --ignore-buildable --quiet || die "pulling images failed"
need_disk 2 "building the API image"
dc build --quiet api worker || die "building the API image failed"
for pair in local-tts=atlas/local-tts:pocket-tts-3.3.0 local-stt=atlas/local-stt:2-sherpa-onnx-1.13.8; do
  svc="${pair%%=*}" img="${pair#*=}"
  if [ "$REBUILD" = 1 ] || ! have_image "$img"; then
    need_disk 4 "building $img"
    info "building $img (once; a few minutes)"
    dc build --quiet "$svc" || die "building $img failed"
  fi
done
info "free disk: $(free_gb) GB"

# ------------------------------------------------------------------------- 5. start
step "Starting the services"
dc up -d --remove-orphans --wait --wait-timeout 1200 || { dc ps; die "a service did not become healthy (docker compose logs --tail 80 <service>)"; }
# Compose does not notice a changed inline config, so coturn is recreated when the rendered
# turnserver.conf changes (only its hash is kept, in .atlas-run/, never the content).
mkdir -p "$ROOT/.atlas-run"
TURN_HASH_FILE="$ROOT/.atlas-run/turnserver.conf.sha256"
TURN_HASH="$(dc config 2>/dev/null | awk '/^configs:/ {f = 1; print; next} f && /^[^ ]/ {f = 0} f' | sha256sum | cut -d' ' -f1)"
if [ "$TURN_HASH" != "$(cat "$TURN_HASH_FILE" 2>/dev/null || true)" ]; then
  if [ -f "$TURN_HASH_FILE" ]; then
    info "the TURN server's settings changed: restarting it"
    dc up -d --force-recreate --no-deps coturn >/dev/null
  fi
  printf '%s\n' "$TURN_HASH" >"$TURN_HASH_FILE"
fi
dc ps --format '    {{.Service}}: {{.Status}}'

# ------------------------------------------------------------------------- 6. database
step "Database migrations and demo data"
dc exec -T api alembic upgrade head
# Synthetic demo tenant (demo@example.com, password ATLAS_DEMO_PASSWORD from .env).
dc exec -T -e ATLAS_ALLOW_DEMO_SEED=true api python -m app.seed

# ------------------------------------------------------------------------- 7. models
step "Self-hosted models"
if dc exec -T local-llm ollama list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -qx "$LLM_MODEL"; then
  info "$LLM_MODEL is present"
else
  need_disk 2 "downloading $LLM_MODEL"
  info "downloading $LLM_MODEL (about 1 GB)"
  dc exec -T local-llm ollama pull "$LLM_MODEL" >/dev/null || die "could not download $LLM_MODEL"
fi
api_python() {  # api_python <script> [args...]: run a repository script inside the API container
  local script="$1"; shift
  dc exec -T -e ATLAS_DEMO_PASSWORD -e ATLAS_DEMO_DEEPGRAM_KEY api python - "$@" <"$script"
}
whisper_load() {  # whisper_load <model>: download it into the speech server (no-op if present)
  local code
  code="$(dc exec -T api python -c "
import httpx
r = httpx.post('http://local-speech:8000/v1/models/$1', timeout=900)
print(r.status_code)" 2>/dev/null | tail -n 1 || true)"
  case "$code" in 200|201|409) info "$1 ready in the speech server" ;; *) warn "could not load $1 into the speech server (HTTP $code)" ;; esac
}

# ------------------------------------------------------------------------- 8. demo
step "AI providers, speed preset and demo agent"
ATLAS_DEMO_PASSWORD="$(env_get ATLAS_DEMO_PASSWORD)"
export ATLAS_DEMO_PASSWORD
if [ -n "$DEEPGRAM_KEY" ] && [ "$PRESET" != pocket ]; then export ATLAS_DEMO_DEEPGRAM_KEY="$DEEPGRAM_KEY"; fi
OUT="$(mktemp)"
api_python infra/scripts/demo_configure.py connections --api http://127.0.0.1:8080 | tee "$OUT" \
  || { unset ATLAS_DEMO_DEEPGRAM_KEY; die "configuring the AI provider connections failed (see above)"; }
unset ATLAS_DEMO_DEEPGRAM_KEY
DEEPGRAM_KEY=""
TARGET=private_pocket_tts
if grep -qx 'STREAMING=yes' "$OUT" && [ "$PRESET" != pocket ]; then TARGET=private_streaming; fi
[ "$PRESET" = streaming ] && TARGET=private_streaming
# The preset is applied on the first setup only, or when --preset was given: a re-deploy
# never switches the organisation's chosen models (speech-to-text included) on its own.
FORCE=""
[ "$PRESET" != auto ] && FORCE=--force
api_python infra/scripts/demo_configure.py finish --api http://127.0.0.1:8080 \
  --preset "$TARGET" --stt-model "$STT_MODEL" --web "https://$WEB_HOST" $FORCE | tee "$OUT" \
  || warn "the demo agent could not be published (see above); fix it in the web app under Agents"
ACTIVE="$(sed -n 's/^PRESET=//p' "$OUT" | tail -n 1)"
STT_IN_USE="$(sed -n 's/^STT=//p' "$OUT" | tail -n 1)"
rm -f "$OUT"
unset ATLAS_DEMO_PASSWORD

# ------------------------------------------------------------------------- 9. warm-up
step "Loading the models into memory"
# Whisper is loaded only when calls use it (STT=whisper:<model>); the streaming server
# (STT=streaming) loaded its model when it started.
WHISPER=""
case "$STT_IN_USE" in whisper:?*) WHISPER="${STT_IN_USE#whisper:}" ;; esac
[ -n "$WHISPER" ] && whisper_load "$WHISPER"
if [ "$SKIP_WARM" = 1 ]; then info "skipped (--skip-warm)"
else
  SKIP=kokoro
  [ -z "$WHISPER" ] && SKIP=kokoro,stt
  api_python infra/scripts/bench_local_models.py --warm-only --stt-model "${WHISPER:-$STT_MODEL}" --skip "$SKIP" \
    --llm-url http://local-llm:11434/v1 --speech-url http://local-speech:8000/v1 --pocket-url http://local-tts:8000/v1 \
    | sed 's/^/    /' || warn "warming the models failed; the first reply of the first call will be slower"
fi

# ------------------------------------------------------------------------- 10. check
step "Public check"
ok=0
for _ in $(seq 1 30); do
  if [ "$(curl -s -o /dev/null -m 10 -w '%{http_code}' "https://$WEB_HOST/api/v1/health/ready" || true)" = 200 ]; then ok=1; break; fi
  sleep 5
done
if [ "$ok" = 1 ]; then info "https://$WEB_HOST/api/v1/health/ready answers 200 (certificate valid)"
else warn "https://$WEB_HOST is not answering yet: check that TCP 80/443 are open (README.md, 'Network') and docker compose logs caddy"
fi

echo
printf '\033[32mATLAS is deployed.\033[0m\n'
echo "  Open:        https://$WEB_HOST"
if [ -n "$GENERATED_PASSWORD" ]; then
  echo "  Sign in:     demo@example.com / $GENERATED_PASSWORD   (generated now, saved in .env as ATLAS_DEMO_PASSWORD; shown only this once)"
else
  echo "  Sign in:     demo@example.com, password: ATLAS_DEMO_PASSWORD in $ENV_FILE"
fi
echo "  Preset:      ${ACTIVE:-unknown} (speech-to-text: ${STT_IN_USE:-unknown}; change it with --preset or in the web app)"
echo "  Test call:   Agents > Mortgage lead qualifier > Test call (allow the microphone)"
echo "  Check:       python3 infra/scripts/e2e_test_call.py --runs 2 --api http://api:8080 --signalling ws://localhost:8000"
echo "  Update:      git pull is part of this script: bash infra/deploy/oci/deploy.sh"
