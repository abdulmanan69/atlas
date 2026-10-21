#!/usr/bin/env bash
# Deploy or update ATLAS on one Linux server with Docker (tested: Oracle Cloud, Oracle
# Linux 9, x86_64). Idempotent: run it again after every `git push`. See README.md here
# and docs/deployment-layouts.md.
#
#   bash infra/deploy/oci/deploy.sh [options]
#
#   --role R           all (default): the whole of ATLAS on this server. models: only the
#                      self-hosted models behind the AI gateway, for ATLAS on other hosts.
#                      Saved in .env (ATLAS_ROLE); a server keeps the role it was set up with.
#   --ai-host NAME     AI gateway hostname: one HTTPS entry point to this server's models
#                      for other ATLAS installations. Role models: default
#                      ai.<ip-with-dashes>.sslip.io. Role all: no gateway unless given.
#                      http://<private IP> serves plain HTTP, on a private network only.
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
# password and the AI gateway's token and live speech-to-text secret are printed once, on
# the run that generates them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"
ENV_FILE="$ROOT/.env"
COMPOSE_FILES="docker-compose.yml:infra/deploy/oci/docker-compose.prod.yml"
MODELS_COMPOSE_FILES="docker-compose.yml:infra/deploy/oci/docker-compose.models.yml"
GATEWAY_COMPOSE_FILE="infra/deploy/oci/docker-compose.ai-gateway.yml"
NODE_IMAGE="node:22.23.2-bookworm-slim"
LLM_MODEL="qwen2.5:1.5b"
# Whisper model of the Pocket TTS preset (used when live transcription is not available).
STT_MODEL="Systran/faster-whisper-base"
WEB_VOLUME="atlas_web_dist"
PNPM_VOLUME="atlas_pnpm_store"
MIN_FREE_GB=4

ROLE="" AI_HOST="" PUBLIC_IP="" WEB_HOST="" VOICE_HOST="" PRESET=auto PULL=1 REBUILD=0 SKIP_WARM=0
while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="${2:-}"; shift ;;
    --ai-host) AI_HOST="${2:-}"; shift ;;
    --public-ip) PUBLIC_IP="${2:-}"; shift ;;
    --web-host) WEB_HOST="${2:-}"; shift ;;
    --voice-host) VOICE_HOST="${2:-}"; shift ;;
    --preset) PRESET="${2:-}"; shift ;;
    --no-pull) PULL=0 ;;
    --rebuild) REBUILD=1 ;;
    --skip-warm) SKIP_WARM=1 ;;
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
case "$ROLE" in ""|all|models) ;; *) echo "--role must be all or models" >&2; exit 2 ;; esac
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
# 256 bits as lower-case hex: the gateway matches URL paths without regard to case, so a
# path secret must not depend on it.
hex_secret() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
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
env_set() {  # env_set KEY VALUE: change KEY in place, or append it
  if env_has "$1"; then
    local tmp; tmp="$(mktemp "$ENV_FILE.XXXXXX")"
    awk -v k="$1" -v v="$2" '$0 ~ "^[[:space:]]*" k "[[:space:]]*=" { print k "=" v; next } { print }' "$ENV_FILE" >"$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE"
  fi
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
# The role: the option, else the one saved; a server set up before roles existed has
# ATLAS_WEB_HOST and is "all". Moving a server to another role is not done in place: its
# data and secrets belong to the old one.
SAVED_ROLE="$(env_get ATLAS_ROLE)"
if [ -z "$SAVED_ROLE" ] && env_has ATLAS_WEB_HOST; then SAVED_ROLE=all; fi
[ -n "$ROLE" ] || ROLE="${SAVED_ROLE:-all}"
[ -z "$SAVED_ROLE" ] || [ "$SAVED_ROLE" = "$ROLE" ] \
  || die "this server was set up with --role $SAVED_ROLE; set up a new server for --role $ROLE (docs/deployment-layouts.md)"
if [ "$ROLE" = models ]; then
  [ -z "$DEEPGRAM_KEY" ] || warn "ATLAS_DEMO_DEEPGRAM_KEY is not used on a models host (ATLAS asks for it when the server is added there)"
  DEEPGRAM_KEY=""
  [ "$PRESET" = auto ] || warn "--preset is not used on a models host (presets belong to an ATLAS organisation)"
fi

[ -n "$PUBLIC_IP" ] || PUBLIC_IP="$(env_get ATLAS_SERVER_PUBLIC_IP)"
if [ -z "$PUBLIC_IP" ]; then
  PUBLIC_IP="$(curl -s -4 -m 10 https://api.ipify.org || true)"
  printf '%s' "$PUBLIC_IP" | grep -q -E '^[0-9]+(\.[0-9]+){3}$' || die "could not find the public IP: pass --public-ip"
fi
DASHED="$(printf '%s' "$PUBLIC_IP" | tr . -)"
if [ "$ROLE" = all ]; then
  PRIVATE_IP="$(env_get ATLAS_SERVER_PRIVATE_IP)"
  [ -n "$PRIVATE_IP" ] || PRIVATE_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' | head -n 1)"
  [ -n "$PRIVATE_IP" ] || die "could not find the server's own IPv4 address"
  [ -n "$WEB_HOST" ] || WEB_HOST="$(env_get ATLAS_WEB_HOST)"
  [ -n "$WEB_HOST" ] || WEB_HOST="atlas.$DASHED.sslip.io"
  [ -n "$VOICE_HOST" ] || VOICE_HOST="$(env_get ATLAS_VOICE_HOST)"
  [ -n "$VOICE_HOST" ] || VOICE_HOST="voice.$DASHED.sslip.io"
  TURN_HOST="$(env_get ATLAS_TURN_HOST)"
  [ -n "$TURN_HOST" ] || TURN_HOST="turn.$DASHED.sslip.io"
fi
# The AI gateway: the option (also to change the name later), else the saved name; a
# models host always has one. It goes into the Caddyfile as a site address, so only a
# plain hostname or http://<address> is accepted.
AI_HOST_GIVEN="$AI_HOST"
[ -n "$AI_HOST" ] || AI_HOST="$(env_get ATLAS_AI_HOST)"
[ -n "$AI_HOST" ] || [ "$ROLE" != models ] || AI_HOST="ai.$DASHED.sslip.io"
if [ -n "$AI_HOST" ]; then
  printf '%s' "$AI_HOST" | grep -q -E '^(http://)?[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$' \
    || die "the AI gateway hostname must be a name like ai.example.com, or http://<private IP>: $AI_HOST"
  if [ "$ROLE" = all ]; then
    for other in "$(env_get ATLAS_WEB_HOST)" "$(env_get ATLAS_VOICE_HOST)" "$WEB_HOST" "$VOICE_HOST"; do
      [ "${AI_HOST#http://}" != "$other" ] || die "the AI gateway needs its own hostname, not $other"
    done
  fi
fi

umask 077
if [ ! -f "$ENV_FILE" ]; then
  printf '# ATLAS production settings, written by infra/deploy/oci/deploy.sh. Secrets: never share or commit.\n' >"$ENV_FILE"
  info "created .env"
fi
chmod 600 "$ENV_FILE"
env_default ATLAS_ROLE "$ROLE"
if [ "$ROLE" = models ]; then
  # docker compose reads these two, so `docker compose ...` in this folder needs no -f/--profile.
  # Only the models: PostgreSQL and Redis of the main file stay off (docker-compose.models.yml).
  env_default COMPOSE_FILE "$MODELS_COMPOSE_FILES"
  env_default COMPOSE_PROFILES "local-ai"
  env_default ATLAS_SERVER_PUBLIC_IP "$PUBLIC_IP"
  env_default POCKET_TTS_HF_TOKEN ""
else
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
fi
if [ -n "$AI_HOST" ]; then
  if [ -n "$AI_HOST_GIVEN" ] && env_has ATLAS_AI_HOST && [ "$(env_get ATLAS_AI_HOST)" != "$AI_HOST" ]; then
    env_set ATLAS_AI_HOST "$AI_HOST"
    info "AI gateway hostname changed to $AI_HOST"
  else
    env_default ATLAS_AI_HOST "$AI_HOST"
  fi
fi
# Secrets: fresh random values from the OS CSPRNG, only when missing.
if [ "$ROLE" = models ]; then
  # The main docker-compose.yml requires this one even where the voice service does not
  # run; it is never used on a models host.
  SECRET_SPECS="ATLAS_VOICE_RUNTIME_JWT_SECRET:48"
else
  SECRET_SPECS="ATLAS_POSTGRES_PASSWORD:32 ATLAS_JWT_SECRET:48 ATLAS_SECRETS_ENCRYPTION_KEY:fernet
    ATLAS_VOICE_DB_PASSWORD:32 ATLAS_VOICE_STORAGE_PASSWORD:32 ATLAS_VOICE_RUNTIME_JWT_SECRET:48
    ATLAS_VOICE_RUNTIME_METRICS_SECRET:48 ATLAS_TURN_SECRET:48 ATLAS_DEMO_PASSWORD:15"
fi
# The AI gateway's token (256 bits; ATLAS sends it as "Authorization: Bearer") and the
# secret in its live speech-to-text path.
[ -z "$AI_HOST" ] || SECRET_SPECS="$SECRET_SPECS ATLAS_AI_TOKEN:32 ATLAS_AI_LIVE_STT_SECRET:hex"
SECRETS_ADDED=()
for spec in $SECRET_SPECS; do
  k="${spec%%:*}" n="${spec##*:}"
  env_has "$k" && [ -n "$(env_get "$k")" ] && continue
  case "$n" in
    fernet) v="$(fernet_key)" ;;
    hex) v="$(hex_secret)" ;;
    *) v="$(rand_secret "$n")" ;;
  esac
  env_set "$k" "$v"  # present but empty: filled in place
  SECRETS_ADDED+=("$k")
done
# The gateway's Compose file follows ATLAS_AI_HOST: added when it is set, removed when the
# line is deleted from .env (that turns the gateway off).
CF="$(env_get COMPOSE_FILE)"
if [ -n "$AI_HOST" ]; then
  case ":$CF:" in
    *":$GATEWAY_COMPOSE_FILE:"*) ;;
    *) env_set COMPOSE_FILE "$CF:$GATEWAY_COMPOSE_FILE"; info "COMPOSE_FILE: AI gateway added" ;;
  esac
else
  case ":$CF:" in
    *":$GATEWAY_COMPOSE_FILE:"*)
      env_set COMPOSE_FILE "$(printf '%s' ":$CF:" | sed "s#:$GATEWAY_COMPOSE_FILE:#:#; s#^:##; s#:\$##")"
      info "COMPOSE_FILE: AI gateway removed (ATLAS_AI_HOST is not set)" ;;
  esac
fi
chmod 600 "$ENV_FILE"
umask 022
GENERATED_PASSWORD="" GENERATED_AI_TOKEN="" GENERATED_AI_STT_SECRET=""
case " ${SECRETS_ADDED[*]-} " in *" ATLAS_DEMO_PASSWORD "*) GENERATED_PASSWORD="$(env_get ATLAS_DEMO_PASSWORD)" ;; esac
case " ${SECRETS_ADDED[*]-} " in *" ATLAS_AI_TOKEN "*) GENERATED_AI_TOKEN="$(env_get ATLAS_AI_TOKEN)" ;; esac
case " ${SECRETS_ADDED[*]-} " in *" ATLAS_AI_LIVE_STT_SECRET "*) GENERATED_AI_STT_SECRET="$(env_get ATLAS_AI_LIVE_STT_SECRET)" ;; esac
[ "${#ADDED[@]}" -gt 0 ] && info "settings added: ${ADDED[*]}"
[ "${#SECRETS_ADDED[@]}" -gt 0 ] && info "secrets generated (not shown): ${SECRETS_ADDED[*]}"
if [ -n "$AI_HOST" ]; then
  # Checked every run: the values reach the Caddyfile (a token check and a path), so they
  # must be long and plain. A hand-edited one that is not is refused, never used.
  for k in ATLAS_AI_TOKEN ATLAS_AI_LIVE_STT_SECRET ATLAS_AI_TOKEN_NEXT; do
    [ "$k" != ATLAS_AI_TOKEN_NEXT ] || [ -n "$(env_get "$k")" ] || continue  # optional
    [[ "$(env_get "$k")" =~ ^[A-Za-z0-9_-]{32,}$ ]] \
      || die "$k in .env must be at least 32 letters, digits, - or _ (delete the line to generate a new one)"
  done
  AI_HOST="$(env_get ATLAS_AI_HOST)"
  case "$AI_HOST" in
    http://*) AI_URL="$AI_HOST"; warn "the AI gateway serves plain HTTP ($AI_URL): use it only on a private network" ;;
    *) AI_URL="https://$AI_HOST" ;;
  esac
fi
if [ "$ROLE" = all ]; then
  WEB_HOST="$(env_get ATLAS_WEB_HOST)" VOICE_HOST="$(env_get ATLAS_VOICE_HOST)"
  info "web app https://$WEB_HOST, voice signalling https://$VOICE_HOST, TURN $(env_get ATLAS_TURN_HOST):3478"
  [ "$(env_get ATLAS_ENVIRONMENT)" = production ] || warn "ATLAS_ENVIRONMENT is not production in .env"
fi
[ -z "$AI_HOST" ] || info "AI gateway $AI_URL"

# ------------------------------------------------------------------------- 3. firewall
step "Firewall"
# What this server accepts from the internet. A models host serves only the AI gateway
# (HTTPS, and HTTP for the certificate); everything else stays closed. The cloud's own
# firewall (Oracle: security list or network security group) is set in its console.
if [ "$ROLE" = models ]; then FW_OPEN="http https"
else FW_OPEN="http https 3478/tcp 3478/udp 49152-49252/udp"; fi
fw() { sudo -n firewall-cmd "$@"; }
if ! command -v firewall-cmd >/dev/null 2>&1; then
  info "no firewalld on this server: allow only $FW_OPEN in its firewall"
elif ! fw --state >/dev/null 2>&1; then
  warn "firewalld is not running, or sudo needs a password: open $FW_OPEN yourself (README.md, 'Network')"
else
  FW_ADDED=()
  for item in $FW_OPEN; do
    case "$item" in */*) kind=port ;; *) kind=service ;; esac
    fw "--query-$kind=$item" >/dev/null 2>&1 && continue
    fw --permanent "--add-$kind=$item" >/dev/null || die "firewalld did not open $item"
    FW_ADDED+=("$item")
  done
  if [ "${#FW_ADDED[@]}" -gt 0 ]; then
    fw --reload >/dev/null
    info "firewalld: opened ${FW_ADDED[*]}"
  fi
  info "firewalld allows: $FW_OPEN"
fi
info "the cloud's firewall must allow the same, and nothing else (Oracle: security list; README.md, 'Network')"

# ------------------------------------------------------------------------- 4. web app
if [ "$ROLE" = all ]; then
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
fi

# ------------------------------------------------------------------------- 5. images
step "Images"
need_disk 2 "pulling updated images"
dc pull --ignore-buildable --quiet || die "pulling images failed"
if [ "$ROLE" = all ]; then
  need_disk 2 "building the API image"
  dc build --quiet api worker || die "building the API image failed"
fi
for pair in local-tts=atlas/local-tts:pocket-tts-3.3.0 local-stt=atlas/local-stt:2-sherpa-onnx-1.13.8; do
  svc="${pair%%=*}" img="${pair#*=}"
  if [ "$REBUILD" = 1 ] || ! have_image "$img"; then
    need_disk 4 "building $img"
    info "building $img (once; a few minutes)"
    dc build --quiet "$svc" || die "building $img failed"
  fi
done
if [ -n "$AI_HOST" ]; then
  # One Python file on the base image of the two above: built every run (a no-op when it
  # did not change), so an update reaches the running service.
  dc build --quiet ai-health || die "building the AI gateway's health service failed"
fi
info "free disk: $(free_gb) GB"

# ------------------------------------------------------------------------- 6. start
step "Starting the services"
dc up -d --remove-orphans --wait --wait-timeout 1200 || { dc ps; die "a service did not become healthy (docker compose logs --tail 80 <service>)"; }
if [ "$ROLE" = all ]; then
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
fi
# Apply the current Caddyfile without a restart: open calls and connections stay up, and
# an unchanged file is a no-op. An invalid file is refused and the running one kept. A
# models host runs Caddy with models.Caddyfile (docker-compose.models.yml).
CADDYFILE=/etc/caddy/Caddyfile
[ "$ROLE" = all ] || CADDYFILE=/etc/caddy/models.Caddyfile
dc exec -T caddy caddy reload --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1   || warn "the web server refused the new Caddyfile and keeps the previous one (docker compose logs caddy)"
dc ps --format '    {{.Service}}: {{.Status}}'

# ------------------------------------------------------------------------- 7. database
if [ "$ROLE" = all ]; then
  step "Database migrations and demo data"
  dc exec -T api alembic upgrade head
  # Synthetic demo tenant (demo@example.com, password ATLAS_DEMO_PASSWORD from .env).
  dc exec -T -e ATLAS_ALLOW_DEMO_SEED=true api python -m app.seed
fi

# ------------------------------------------------------------------------- 8. models
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
# A container on the Docker network with Python, for requests to the model servers: the API
# on a whole ATLAS server, the gateway's health service on a models host.
HELPER=api
[ "$ROLE" = all ] || HELPER=ai-health
whisper_load() {  # whisper_load <model>: download it into the speech server (no-op if present)
  local code
  code="$(dc exec -T "$HELPER" python -c "
import urllib.error, urllib.request
try:
    print(urllib.request.urlopen(urllib.request.Request('http://local-speech:8000/v1/models/$1', method='POST'), timeout=900).status)
except urllib.error.HTTPError as e:
    print(e.code)" 2>/dev/null | tail -n 1 || true)"
  case "$code" in 200|201|409) info "$1 ready in the speech server" ;; *) warn "could not load $1 into the speech server (HTTP $code)" ;; esac
}
# A models host offers the default Whisper model to every ATLAS that uses it; a whole ATLAS
# server downloads it below, only when its calls use Whisper.
[ "$ROLE" = all ] || whisper_load "$STT_MODEL"

# ------------------------------------------------------------------------- 9. demo
if [ "$ROLE" = all ]; then
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
fi

# ------------------------------------------------------------------------- 10. warm-up
step "Loading the models into memory"
if [ "$ROLE" = models ]; then
  # Pocket TTS and live speech-to-text load their models when they start, and Whisper
  # loads one on its first request; the language model is loaded here and kept.
  if [ "$SKIP_WARM" = 1 ]; then info "skipped (--skip-warm)"
  elif dc exec -T ai-health python -c "
import json, urllib.request
body = json.dumps({'model': '$LLM_MODEL', 'prompt': '', 'stream': False}).encode()
urllib.request.urlopen(urllib.request.Request('http://local-llm:11434/api/generate', data=body, headers={'Content-Type': 'application/json'}), timeout=600).read()"; then
    info "$LLM_MODEL is in memory"
  else
    warn "loading $LLM_MODEL failed; the first reply will be slower"
  fi
else
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
fi

# ------------------------------------------------------------------------- 11. check
step "Public check"
if [ "$ROLE" = all ]; then
  ok=0
  for _ in $(seq 1 30); do
    if [ "$(curl -s -o /dev/null -m 10 -w '%{http_code}' "https://$WEB_HOST/api/v1/health/ready" || true)" = 200 ]; then ok=1; break; fi
    sleep 5
  done
  if [ "$ok" = 1 ]; then info "https://$WEB_HOST/api/v1/health/ready answers 200 (certificate valid)"
  else warn "https://$WEB_HOST is not answering yet: check that TCP 80/443 are open (README.md, 'Network') and docker compose logs caddy"
  fi
fi
if [ -n "$AI_HOST" ]; then
  # 401 without the token shows the name, the certificate and the gateway work; then the
  # health answer with it. The token goes to curl on stdin, never on its command line.
  code=""
  for _ in $(seq 1 30); do
    code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$AI_URL/health" || true)"
    [ "$code" = 401 ] && break
    sleep 5
  done
  if [ "$code" = 401 ]; then
    HEALTH="$(printf 'Authorization: Bearer %s\n' "$(env_get ATLAS_AI_TOKEN)" | curl -s -m 20 -H @- "$AI_URL/health" || true)"
    # {"status":"ok",...,"services":{"llm":{"up":true,"state":"up",...},...}} -> "llm up, ..."
    STATUS="$(printf '%s' "$HEALTH" | sed -n 's/^{"status":"\([a-z]*\)".*/\1/p' | head -n 1)"
    SERVICES="$(printf '%s' "$HEALTH" | { grep -o '"[a-z_]*":{"up":[a-z]*,"state":"[a-z]*"' || true; } \
      | sed -E 's/"([a-z_]+)":\{"up":[a-z]+,"state":"([a-z]+)"/\1 \2/' | paste -sd, - | sed 's/,/, /g')"
    case "$AI_URL" in
      https://*) info "$AI_URL refuses requests without the token (certificate valid)" ;;
      *) info "$AI_URL refuses requests without the token" ;;
    esac
    info "with the token, /health: ${STATUS:-no answer}${SERVICES:+ ($SERVICES)}"
  else
    warn "$AI_URL is not answering yet (HTTP ${code:-none}): check that TCP 80/443 are open (README.md, 'Network') and docker compose logs caddy"
  fi
fi

ai_summary() {
  echo "  AI gateway:  $AI_URL   (for ATLAS on other hosts; docs/deployment-layouts.md)"
  if [ -n "$GENERATED_AI_TOKEN" ]; then
    echo "  Token:       $GENERATED_AI_TOKEN   (generated now, saved in .env as ATLAS_AI_TOKEN; shown only this once)"
  else
    echo "  Token:       ATLAS_AI_TOKEN in $ENV_FILE"
  fi
  if [ -n "$GENERATED_AI_STT_SECRET" ]; then
    echo "  Live STT:    $AI_URL/live-stt/$GENERATED_AI_STT_SECRET   (Deepgram base URL; the secret is ATLAS_AI_LIVE_STT_SECRET in .env; shown only this once)"
  else
    echo "  Live STT:    $AI_URL/live-stt/<ATLAS_AI_LIVE_STT_SECRET in .env>   (Deepgram base URL)"
  fi
}

echo
if [ "$ROLE" = models ]; then
  printf '\033[32mATLAS models host is deployed.\033[0m\n'
  ai_summary
  echo "  Models:      $LLM_MODEL, $STT_MODEL, Pocket TTS, live speech-to-text"
  echo "  Check:       curl -H \"Authorization: Bearer \$TOKEN\" $AI_URL/health"
  echo "  Update:      git pull is part of this script: bash infra/deploy/oci/deploy.sh"
  exit 0
fi
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
[ -z "$AI_HOST" ] || ai_summary
