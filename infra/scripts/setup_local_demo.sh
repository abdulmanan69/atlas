#!/usr/bin/env bash
# One command to set up and start the fully self-hosted ATLAS demo (browser test call).
# Bash twin of setup_local_demo.ps1 (Linux, macOS, Git Bash on Windows). Safe to re-run.
#
#   bash infra/scripts/setup_local_demo.sh [options]      (or: make demo ARGS="--restart")
#
#   --stop               stop what this script started and the demo's Docker containers
#   --restart            also stop programs this script did not start that hold the ATLAS
#                        ports (8080 API, 5173 web), then start fresh ones
#   --dry-run            show what would be done, change nothing
#   --preset P           auto (default) | pocket | streaming
#   --deepgram-key KEY   a real Deepgram key, only to save the live-transcription connection
#                        (needs internet once). Prefer env ATLAS_DEMO_DEEPGRAM_KEY. Never printed.
#   --skip-warm          do not load the models into memory at the end
#   --fast-stt           Whisper tiny.en instead of base: ~0.7 s faster per reply, English only
#   --rebuild            rebuild the two locally built model images (needs internet)
#
# Default preset private_pocket_tts (Whisper + qwen2.5:1.5b + Pocket TTS) works with no
# internet. See docs/university-demo.md.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
API_DIR="$ROOT/services/api"
WEB_DIR="$ROOT/apps/web"
RUN_DIR="$ROOT/.atlas-run"
DEPS_DIR="$RUN_DIR/deps"
LOG_DIR="${TMPDIR:-/tmp}/atlas-demo"
ENV_FILE="$ROOT/.env"
ENV_EXAMPLE="$ROOT/.env.example"
PROFILES=(--profile voice --profile local-ai)
API_URL="http://127.0.0.1:8080"
WEB_URL="http://localhost:5173"
LLM_MODEL="qwen2.5:1.5b"
STT_MODEL="Systran/faster-whisper-base"
CORE_SERVICES=(postgres redis voice-db voice-redis voice-storage voice-runtime local-llm local-speech local-tts)
REQUIRED_SERVICES=" postgres redis voice-db voice-redis voice-storage voice-runtime "

STOP=0 RESTART=0 DRY_RUN=0 SKIP_WARM=0 REBUILD=0 PRESET=auto
KEY="${ATLAS_DEMO_DEEPGRAM_KEY:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --stop) STOP=1 ;;
    --restart) RESTART=1 ;;
    --dry-run|--what-if) DRY_RUN=1 ;;
    --skip-warm) SKIP_WARM=1 ;;
    --fast-stt) STT_MODEL=Systran/faster-whisper-tiny.en ;;
    --rebuild) REBUILD=1 ;;
    --preset) PRESET="${2:-}"; shift ;;
    --deepgram-key) KEY="${2:-}"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
case "$PRESET" in auto|pocket|streaming) ;; *) echo "--preset must be auto, pocket or streaming" >&2; exit 2 ;; esac
# Kept in shell variables only: the API, worker and web app started below must not inherit them.
unset ATLAS_DEMO_DEEPGRAM_KEY ATLAS_DEMO_PASSWORD

case "$(uname -s)" in
  Linux*) PLATFORM=linux ;;
  Darwin*) PLATFORM=mac ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM=windows ;;
  *) PLATFORM=other ;;
esac
if [ "$PLATFORM" = windows ]; then
  VENV_PY="$API_DIR/.venv/Scripts/python.exe"
  LOG_DIR="${TEMP:-${TMPDIR:-/tmp}}/atlas-demo"
else
  VENV_PY="$API_DIR/.venv/bin/python"
fi

WARNINGS=()
GENERATED_PASSWORD=""

# ------------------------------------------------------------------------------ output
step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
info() { printf '    %s\n' "$1"; }
warn() { printf '    \033[33mWARNING: %s\033[0m\n' "$1"; WARNINGS+=("$1"); }
fail() { printf '\n\033[31mERROR: %s\033[0m\n' "$1"; unset ATLAS_DEMO_PASSWORD ATLAS_DEMO_DEEPGRAM_KEY; exit 1; }
plan() { printf '    \033[90m[dry run] would %s\033[0m\n' "$1"; }

# ------------------------------------------------------------------------------ helpers
have() { command -v "$1" >/dev/null 2>&1; }
major() { printf '%s' "$1" | sed -E 's/^v//; s/^([0-9]+).*/\1/'; }
http_code() { curl -s -o /dev/null -m "${2:-5}" -w '%{http_code}' "$1" 2>/dev/null || true; }
compose() { docker compose "${PROFILES[@]}" "$@"; }

rand_secret() {  # url-safe random text from the OS CSPRNG
  head -c "${1:-48}" /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_'
}
fernet_key() { head -c 32 /dev/urandom | base64 | tr -d '\n' | tr '+/' '-_'; }

env_get() {  # value of KEY in an env file, quotes stripped
  [ -f "$2" ] || return 0
  sed -n -E "s/^[[:space:]]*$1[[:space:]]*=(.*)$/\1/p" "$2" | tail -n 1 | sed -E "s/^[\"']//; s/[\"']$//; s/[[:space:]]+$//"
}

env_set() {  # replace KEY=... in place or append it; keeps every other line
  local key="$1" value="$2" file="$3" tmp
  tmp="$(mktemp "$file.XXXXXX")" || fail "cannot write next to $file"
  if grep -q -E "^[[:space:]]*${key}[[:space:]]*=" "$file"; then
    awk -v k="$key" -v v="$value" '
      $0 ~ "^[[:space:]]*" k "[[:space:]]*=" { print k "=" v; next } { print }' "$file" >"$tmp"
  else
    cat "$file" >"$tmp"
    if ! grep -q "^# Added by infra/scripts/setup_local_demo" "$file"; then
      printf '\n# Added by infra/scripts/setup_local_demo\n' >>"$tmp"
    fi
    printf '%s=%s\n' "$key" "$value" >>"$tmp"
  fi
  chmod 600 "$tmp" 2>/dev/null || true
  mv "$tmp" "$file"
}

port_pid() {  # pid listening on a TCP port (Windows pid under Git Bash); empty if none
  local port="$1"
  case "$PLATFORM" in
    windows)
      netstat -ano 2>/dev/null | tr -d '\r' | awk -v p=":$port" '$1=="TCP" && $4=="LISTENING" && substr($2, length($2)-length(p)+1)==p {print $5; exit}' ;;
    *)
      if have lsof; then lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n 1
      elif have ss; then ss -ltnpH "sport = :$port" 2>/dev/null | sed -n -E 's/.*pid=([0-9]+).*/\1/p' | head -n 1
      fi ;;
  esac
}

proc_name() {
  case "$PLATFORM" in
    windows) tasklist //FI "PID eq $1" //FO CSV //NH 2>/dev/null | tr -d '\r' | head -n 1 | cut -d, -f1 | tr -d '"' ;;
    *) ps -o comm= -p "$1" 2>/dev/null | head -n 1 ;;
  esac
}

kill_tree() {  # pid in this platform's numbering (Windows pid under Git Bash)
  local pid="$1" child
  if [ "$PLATFORM" = windows ]; then
    taskkill //PID "$pid" //T //F >/dev/null 2>&1
    return 0
  fi
  if have pgrep; then
    for child in $(pgrep -P "$pid" 2>/dev/null); do kill_tree "$child"; done
  fi
  kill -TERM "$pid" 2>/dev/null
  for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || return 0; sleep 1; done
  kill -KILL "$pid" 2>/dev/null
  return 0
}

start_token() {  # identifies a process instance, so a reused pid is never mistaken for ours
  if [ "$PLATFORM" = windows ]; then
    tasklist //FI "PID eq $1" //FO CSV //NH 2>/dev/null | tr -d '\r' | grep -q "\"$1\"" && echo alive
  else
    ps -o lstart= -p "$1" 2>/dev/null | tr -s ' ' '_'
  fi
}

own_pid() {  # pid recorded by this script if that process still runs; empty otherwise
  local file="$RUN_DIR/$1.sh.pid" pid token
  [ -f "$file" ] || return 0
  read -r pid token <"$file" || return 0
  [ -n "${pid:-}" ] || return 0
  [ "$(start_token "$pid")" = "${token:-}" ] && [ -n "${token:-}" ] && echo "$pid"
}

stop_own() {
  local pid
  pid="$(own_pid "$1")"
  if [ -n "$pid" ]; then
    if [ "$DRY_RUN" = 1 ]; then plan "stop $1 (pid $pid, started by this script)"; return 1; fi
    kill_tree "$pid"
    info "stopped $1 (pid $pid, started by this script earlier)"
    rm -f "$RUN_DIR/$1.sh.pid"
    return 0
  fi
  [ "$DRY_RUN" = 1 ] || rm -f "$RUN_DIR/$1.sh.pid"
  return 1
}

wait_port_free() {
  local i
  for i in $(seq 1 "${2:-15}"); do
    [ -z "$(port_pid "$1")" ] && return 0
    sleep 1
    : "$i"
  done
  return 1
}

start_bg() {  # name dir command...
  local name="$1" dir="$2" pid win
  shift 2
  ( cd "$dir" || exit 1; exec nohup "$@" ) >"$LOG_DIR/$name.log" 2>&1 </dev/null &
  pid=$!
  if [ "$PLATFORM" = windows ] && [ -r "/proc/$pid/winpid" ]; then
    sleep 1
    win="$(cat "/proc/$pid/winpid" 2>/dev/null)"
    [ -n "$win" ] && pid="$win"
  fi
  printf '%s %s\n' "$pid" "$(start_token "$pid")" >"$RUN_DIR/$name.sh.pid"
  info "started $name (pid $pid); log: $LOG_DIR/$name.log"
  STARTED_PID="$pid"
}

alive() {
  if [ "$PLATFORM" = windows ]; then [ -n "$(start_token "$1")" ]; else kill -0 "$1" 2>/dev/null; fi
}

log_tail() {
  [ -f "$LOG_DIR/$1.log" ] && { printf '    --- last lines of %s\n' "$LOG_DIR/$1.log"; tail -n 15 "$LOG_DIR/$1.log" | sed 's/^/    /'; }
  return 0
}

container_state() {  # status|health|oomkilled|exitcode
  local id
  id="$(compose ps -a -q "$1" 2>/dev/null | head -n 1)"
  [ -n "$id" ] || return 0
  docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.State.OOMKilled}}|{{.State.ExitCode}}' "$id" 2>/dev/null
}

wait_service() {  # service seconds [probe-url]
  local svc="$1" limit="$2" probe="${3:-}" start now state status health oom code ready last
  start="$(date +%s)"; last="$start"
  while :; do
    now="$(date +%s)"
    [ $((now - start)) -ge "$limit" ] && break
    state="$(container_state "$svc")"
    if [ -n "$state" ]; then
      IFS='|' read -r status health oom code <<<"$state"
      if [ "$oom" = true ] || [ "$code" = 137 ]; then
        warn "$svc was stopped for lack of memory (out-of-memory kill). Close other programs or give Docker more memory, then re-run."
        return 1
      fi
      if [ "$status" = exited ] || [ "$status" = dead ]; then
        warn "$svc exited (code $code). See: docker compose ${PROFILES[*]} logs --tail 50 $svc"
        return 1
      fi
      ready=0
      if [ "$health" != none ]; then [ "$health" = healthy ] && ready=1
      elif [ "$status" = running ]; then
        if [ -z "$probe" ] || [ "$(http_code "$probe" 3)" = 200 ]; then ready=1; fi
      fi
      if [ "$ready" = 1 ]; then info "$svc is ready"; return 0; fi
    fi
    if [ $((now - last)) -ge 30 ]; then info "still waiting for $svc ..."; last="$now"; fi
    sleep 3
  done
  warn "$svc did not become ready within ${limit}s. See: docker compose ${PROFILES[*]} logs --tail 50 $svc"
  return 1
}

api_python() {  # run a Python script with the API environment; output also saved to $PY_OUT
  PY_OUT="$(mktemp)"
  (cd "$API_DIR" && uv run --no-sync python "$@") | tee "$PY_OUT"
  PY_EXIT=${PIPESTATUS[0]}
}

find_worker() {  # pid of an ATLAS worker already running (any starter); empty if none
  if [ "$PLATFORM" = windows ]; then
    powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.Name -match '^(python|arq)' -and \$_.CommandLine -like '*app.workers.main.WorkerSettings*' } | Select-Object -First 1 -ExpandProperty ProcessId" 2>/dev/null | tr -d '\r'
  elif have pgrep; then
    pgrep -f "app.workers.main.WorkerSettings" 2>/dev/null | head -n 1
  fi
}

# ------------------------------------------------------------------------------ stop
if [ "$STOP" = 1 ]; then
  step "Stopping the ATLAS demo"
  for n in web worker api; do stop_own "$n" || true; done
  if [ "$DRY_RUN" != 1 ]; then
    wait_port_free 8080 10 || true; wait_port_free 5173 10 || true
    for port in 8080 5173; do
      p="$(port_pid "$port")"
      [ -n "$p" ] && info "port $port is still used by $(proc_name "$p") (pid $p), which this script did not start: left running"
    done
  fi
  cd "$ROOT" || exit 1
  if [ "$DRY_RUN" = 1 ]; then plan "docker compose ${PROFILES[*]} stop"
  elif compose stop; then info "Docker containers stopped (data and models are kept)"
  else warn "docker compose stop failed (is Docker running?)"
  fi
  exit 0
fi

# ------------------------------------------------------------------------------ main
KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
[ "$DRY_RUN" = 1 ] && printf '\033[90mDry run: nothing will be changed.\033[0m\n'
cd "$ROOT" || exit 1

# 1. preflight
step "Checking this machine"
have docker || fail "Docker is not installed. Install Docker (https://docs.docker.com/get-docker/) and re-run."
docker info --format '{{.ServerVersion}}' >/dev/null 2>&1 || fail "Docker is not running. Start Docker, wait until it is ready, then re-run."
COMPOSE_VERSION="$(docker compose version --short 2>/dev/null)" || fail "Docker Compose v2 is missing ('docker compose version' failed)."
info "Docker Compose $COMPOSE_VERSION"
have curl || fail "curl is missing; install it and re-run."

have node || fail "Node.js is not installed. Install Node.js 22 LTS (https://nodejs.org/) and re-run."
NODE_VERSION="$(node --version 2>/dev/null)"
NODE_MAJOR="$(major "$NODE_VERSION")"
[ "${NODE_MAJOR:-0}" -ge 22 ] 2>/dev/null || fail "Node.js $NODE_VERSION is too old. Install Node.js 22 LTS."
if [ "$NODE_MAJOR" -ne 22 ]; then warn "Node.js $NODE_VERSION: ATLAS is tested with Node.js 22 LTS (newer versions usually work)."; else info "Node.js $NODE_VERSION"; fi

have pnpm || fail "pnpm is not installed. Run: npm install -g pnpm@10"
PNPM_VERSION="$(pnpm --version 2>/dev/null)"
PNPM_MAJOR="$(major "$PNPM_VERSION")"
[ "${PNPM_MAJOR:-0}" -ge 10 ] 2>/dev/null || fail "pnpm $PNPM_VERSION is too old. Run: npm install -g pnpm@10"
if [ "$PNPM_MAJOR" -ne 10 ]; then warn "pnpm $PNPM_VERSION: the web app pins pnpm 10. A newer pnpm downloads pnpm 10 on first use, which fails without internet: install pnpm 10 on the demo PC (npm install -g pnpm@10)."; else info "pnpm $PNPM_VERSION"; fi

have uv || fail "uv is not installed. Run: curl -LsSf https://astral.sh/uv/install.sh | sh"
info "$(uv --version 2>/dev/null)"

RAM_TOTAL_KB=0 RAM_FREE_KB=0
case "$PLATFORM" in
  linux)
    RAM_TOTAL_KB="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
    RAM_FREE_KB="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)" ;;
  mac)
    RAM_TOTAL_KB=$(( $(sysctl -n hw.memsize) / 1024 ))
    RAM_FREE_KB="$(vm_stat | awk -v ps="$(sysctl -n hw.pagesize)" '/Pages (free|inactive|speculative)/ {gsub("\\.","",$NF); s+=$NF} END {print int(s*ps/1024)}')" ;;
  windows)
    read -r RAM_TOTAL_KB RAM_FREE_KB < <(powershell.exe -NoProfile -Command "\$o = Get-CimInstance Win32_OperatingSystem; '{0} {1}' -f \$o.TotalVisibleMemorySize, \$o.FreePhysicalMemory" 2>/dev/null | tr -d '\r') ;;
esac
CORES="$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0)"
RAM_TOTAL_GB="$(awk -v k="${RAM_TOTAL_KB:-0}" 'BEGIN {printf "%.1f", k/1048576}')"
RAM_FREE_GB="$(awk -v k="${RAM_FREE_KB:-0}" 'BEGIN {printf "%.1f", k/1048576}')"
info "RAM $RAM_TOTAL_GB GB total, $RAM_FREE_GB GB free; $CORES logical CPU cores"
# A "16 GB" machine reports a little less; warn below about 15 GB.
[ "${RAM_TOTAL_KB:-0}" -lt 15000000 ] && warn "This machine has $RAM_TOTAL_GB GB of RAM; 16 GB or more is recommended. Replies will be slower; close other programs before the demo."
[ "${RAM_FREE_KB:-0}" -lt $((6 * 1024 * 1024)) ] && warn "Only $RAM_FREE_GB GB of RAM is free; the models need about 6 GB. Close browser tabs and other programs."
[ "${CORES:-0}" -lt 8 ] && warn "$CORES CPU cores; 8 or more are recommended for snappy replies on CPU."
DOCKER_MEM="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
if [ "${DOCKER_MEM:-0}" -gt 0 ] 2>/dev/null; then
  DOCKER_GB="$(awk -v b="$DOCKER_MEM" 'BEGIN {printf "%.1f", b/1073741824}')"
  info "Docker can use $DOCKER_GB GB of memory"
  [ "$DOCKER_MEM" -lt $((7 * 1024 * 1024 * 1024)) ] && warn "Docker can use only $DOCKER_GB GB of memory; the demo needs about 7 GB. Raise Docker's memory limit (Docker Desktop: Settings > Resources)."
fi
FREE_KB="$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')"
FREE_GB="$(awk -v k="${FREE_KB:-0}" 'BEGIN {printf "%.1f", k/1048576}')"
info "Free disk where the repository is: $FREE_GB GB"
[ "${FREE_KB:-0}" -lt $((25 * 1024 * 1024)) ] && warn "Only $FREE_GB GB free. The first setup needs about 25 GB (Docker images and models; Docker's own disk may be elsewhere)."

for port in 5433 6380 8000 9000 11434 8001 8002 8003; do
  p="$(port_pid "$port")"
  [ -n "$p" ] || continue
  name="$(proc_name "$p")"
  case "$name" in
    *com.docker*|*wslrelay*|*vpnkit*|*docker-proxy*|*dockerd*|*rootlesskit*) ;;
    *) warn "Port $port is used by ${name:-a program} (pid $p). A demo container needs it; close that program." ;;
  esac
done

# uv.toml points the cache at D: on the development machine: fall back to uv's default.
if [ -z "${UV_CACHE_DIR:-}" ] && [ -f "$API_DIR/uv.toml" ]; then
  UV_TOML_CACHE="$(sed -n -E 's/^[[:space:]]*cache-dir[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$API_DIR/uv.toml")"
  case "$UV_TOML_CACHE" in
    [A-Za-z]:*)
      if [ "$PLATFORM" != windows ] || [ ! -d "/${UV_TOML_CACHE:0:1}" ]; then
        if [ "$PLATFORM" = windows ]; then export UV_CACHE_DIR="${LOCALAPPDATA:-$HOME}/uv/cache"; else export UV_CACHE_DIR="$HOME/.cache/uv"; fi
        info "uv cache: $UV_CACHE_DIR (the drive in services/api/uv.toml does not exist here)"
      fi ;;
  esac
fi
USE_DEPS=0
if [ -d "$DEPS_DIR/uv-cache" ]; then
  USE_DEPS=1
  export UV_CACHE_DIR="$DEPS_DIR/uv-cache"
  [ -d "$DEPS_DIR/uv-python" ] && export UV_PYTHON_INSTALL_DIR="$DEPS_DIR/uv-python"
  info "using the code dependencies from the offline bundle in $DEPS_DIR"
fi

# 2. .env
step "Settings file (.env)"
ENV_IS_NEW=0
if [ ! -f "$ENV_FILE" ]; then
  ENV_IS_NEW=1
  [ -f "$ENV_EXAMPLE" ] || fail ".env.example is missing; is this the ATLAS repository?"
  if [ "$DRY_RUN" = 1 ]; then plan "create .env from .env.example"
  else cp "$ENV_EXAMPLE" "$ENV_FILE" && chmod 600 "$ENV_FILE" 2>/dev/null; info "created .env from .env.example"; fi
else
  info ".env exists: keeping every value that is already set"
fi
SRC_ENV="$ENV_FILE"; [ "$ENV_IS_NEW" = 1 ] && SRC_ENV="$ENV_EXAMPLE"
GENERATED=()
for spec in ATLAS_JWT_SECRET:secret ATLAS_SECRETS_ENCRYPTION_KEY:fernet ATLAS_VOICE_RUNTIME_JWT_SECRET:secret \
            ATLAS_VOICE_RUNTIME_METRICS_SECRET:secret ATLAS_VOICE_STORAGE_PASSWORD:secret; do
  k="${spec%%:*}" kind="${spec##*:}"
  cur="$(env_get "$k" "$SRC_ENV")"
  case "$cur" in ""|replace-with-a-long-random-secret|change-me) ;; *) continue ;; esac
  # An existing storage volume was initialised with the compose default password.
  if [ "$k" = ATLAS_VOICE_STORAGE_PASSWORD ] && docker volume inspect atlas_voice_storage_data >/dev/null 2>&1; then continue; fi
  GENERATED+=("$k")
  if [ "$DRY_RUN" != 1 ]; then
    if [ "$kind" = fernet ]; then env_set "$k" "$(fernet_key)" "$ENV_FILE"; else env_set "$k" "$(rand_secret 48)" "$ENV_FILE"; fi
  fi
done
DEMO_PASSWORD="$(env_get ATLAS_DEMO_PASSWORD "$SRC_ENV")"
if [ -z "$DEMO_PASSWORD" ]; then
  DEMO_PASSWORD="$(rand_secret 15)"
  GENERATED_PASSWORD="$DEMO_PASSWORD"
  GENERATED+=(ATLAS_DEMO_PASSWORD)
  [ "$DRY_RUN" = 1 ] || env_set ATLAS_DEMO_PASSWORD "$DEMO_PASSWORD" "$ENV_FILE"
fi
if [ "${#GENERATED[@]}" -gt 0 ]; then
  if [ "$DRY_RUN" = 1 ]; then plan "generate (random, not shown): ${GENERATED[*]}"; else info "generated (random, not shown): ${GENERATED[*]}"; fi
else
  info "all required secrets are set"
fi

# 3. code dependencies
step "Code dependencies (API and web app)"
if [ "$DRY_RUN" = 1 ]; then
  plan "run 'uv sync --frozen' in services/api (offline from the cache if there is no internet)"
  plan "run 'pnpm install --frozen-lockfile' in apps/web (offline from the store if there is no internet)"
else
  ok=0
  if [ "$USE_DEPS" = 1 ]; then (cd "$API_DIR" && uv sync --frozen --offline) && ok=1
  else
    if (cd "$API_DIR" && uv sync --frozen); then ok=1
    else info "retrying offline from the uv cache"; (cd "$API_DIR" && uv sync --frozen --offline) && ok=1; fi
  fi
  if [ "$ok" != 1 ]; then
    if [ -x "$VENV_PY" ]; then warn "uv sync failed; using the existing services/api/.venv as it is."
    else fail "Installing the API's Python packages failed. Connect to the internet once, or import an offline bundle made with --with-deps."; fi
  fi
  ok=0
  if [ -d "$DEPS_DIR/pnpm-store" ]; then
    (cd "$WEB_DIR" && pnpm install --frozen-lockfile --offline --store-dir "$DEPS_DIR/pnpm-store") && ok=1
  else
    # Keep the store node_modules was linked from: with another store pnpm wants to delete
    # and rebuild node_modules (and refuses without a terminal).
    STORE_ARGS=()
    if [ -f "$WEB_DIR/node_modules/.modules.yaml" ]; then
      store_dir="$(sed -n -E "s/^storeDir:[[:space:]]*['\"]?([^'\"]+)['\"]?[[:space:]]*$/\1/p" "$WEB_DIR/node_modules/.modules.yaml" | head -n 1 | sed -E 's#[\/]v[0-9]+$##')"
      if [ -n "$store_dir" ]; then
        [ "$PLATFORM" = windows ] && store_dir="$(cygpath -u "$store_dir" 2>/dev/null || printf '%s' "$store_dir")"
        [ -d "$store_dir" ] && STORE_ARGS=(--store-dir "$store_dir")
      fi
    fi
    if (cd "$WEB_DIR" && pnpm install --frozen-lockfile "${STORE_ARGS[@]+"${STORE_ARGS[@]}"}"); then ok=1
    else info "retrying offline from the pnpm store"; (cd "$WEB_DIR" && pnpm install --frozen-lockfile --offline "${STORE_ARGS[@]+"${STORE_ARGS[@]}"}") && ok=1; fi
  fi
  if [ "$ok" != 1 ]; then
    if [ -d "$WEB_DIR/node_modules/vite" ]; then warn "pnpm install failed; using the existing apps/web/node_modules as it is."
    else fail "Installing the web app's packages failed. Connect to the internet once, or import an offline bundle made with --with-deps."; fi
  fi
fi

# 4. Docker services
step "Docker services"
WANT_STREAMING=0
if [ "$PRESET" = streaming ] || { [ -n "$KEY" ] && [ "$PRESET" != pocket ]; }; then WANT_STREAMING=1; fi
SERVICES=("${CORE_SERVICES[@]}")
[ "$WANT_STREAMING" = 1 ] && SERVICES+=(local-stt)
for pair in local-tts=atlas/local-tts:pocket-tts-3.3.0 local-stt=atlas/local-stt:sherpa-onnx-1.13.8; do
  svc="${pair%%=*}" img="${pair#*=}"
  case " ${SERVICES[*]} " in *" $svc "*) ;; *) continue ;; esac
  docker image inspect "$img" >/dev/null 2>&1 || info "image $img is not here yet: it is built now (needs internet; import the offline bundle to skip this)"
done
UP_ARGS=(up -d)
[ "$REBUILD" = 1 ] && UP_ARGS+=(--build)
UP_ARGS+=("${SERVICES[@]}")
if [ "$DRY_RUN" = 1 ]; then
  plan "docker compose ${PROFILES[*]} ${UP_ARGS[*]}"
else
  compose "${UP_ARGS[@]}" || fail "docker compose up failed (see above). If a port is in use, close that program; if an image is missing and there is no internet, import the offline bundle."
  FAILED=()
  for svc in "${SERVICES[@]}"; do
    case "$svc" in
      voice-runtime) limit=300; probe="" ;;
      local-tts) limit=900; probe="" ;;
      local-stt) limit=600; probe="" ;;
      local-speech) limit=180; probe="http://127.0.0.1:8001/health" ;;
      local-llm) limit=90; probe="http://127.0.0.1:11434/api/version" ;;
      *) limit=90; probe="" ;;
    esac
    wait_service "$svc" "$limit" "$probe" || FAILED+=("$svc")
  done
  FATAL=()
  for svc in "${FAILED[@]+"${FAILED[@]}"}"; do
    case "$REQUIRED_SERVICES" in *" $svc "*) FATAL+=("$svc") ;; esac
  done
  [ "${#FATAL[@]}" -gt 0 ] && fail "required services are not running: ${FATAL[*]}"
  [ "${#FAILED[@]}" -gt 0 ] && warn "model services not ready: ${FAILED[*]}. The test call needs them."
fi

# 5. database
step "Database migrations and demo data"
if [ "$DRY_RUN" = 1 ]; then
  plan "run 'alembic upgrade head' and 'python -m app.seed' (demo@example.com, password from .env)"
else
  (cd "$API_DIR" && uv run --no-sync alembic upgrade head) || fail "database migration failed"
  (cd "$API_DIR" && ATLAS_DEMO_PASSWORD="$DEMO_PASSWORD" uv run --no-sync python -m app.seed) || fail "seeding the demo data failed"
fi

# 6. models
step "Self-hosted models"
if [ "$DRY_RUN" = 1 ]; then
  plan "download $LLM_MODEL into Ollama and $STT_MODEL into the speech server if they are missing (needs internet once)"
else
  TAGS="$(curl -s -m 10 http://127.0.0.1:11434/api/tags 2>/dev/null || true)"
  if [ -z "$TAGS" ]; then warn "the language model server is not answering; skipped the model download"
  elif printf '%s' "$TAGS" | grep -q "\"$LLM_MODEL\""; then info "$LLM_MODEL is present"
  else
    info "downloading $LLM_MODEL (about 1 GB, needs internet)"
    compose exec -T local-llm ollama pull "$LLM_MODEL" || warn "could not download $LLM_MODEL. Connect to the internet once, or import the offline bundle."
  fi
  if [ "$(http_code http://127.0.0.1:8001/health)" = 200 ]; then
    if curl -s -m 20 http://127.0.0.1:8001/v1/models 2>/dev/null | grep -q "\"$STT_MODEL\""; then info "$STT_MODEL is present"
    else
      info "downloading $STT_MODEL (about 150 MB, needs internet)"
      code="$(curl -s -o /dev/null -m 900 -w '%{http_code}' -X POST "http://127.0.0.1:8001/v1/models/$STT_MODEL" || true)"
      case "$code" in 200|201|409) info "$STT_MODEL ready" ;; *) warn "could not download $STT_MODEL (HTTP $code). Connect to the internet once, or import the offline bundle." ;; esac
    fi
  else
    warn "the speech server is not answering; skipped the Whisper download"
  fi
  info "Pocket TTS and live transcription keep their weights in their volumes (downloaded on first start)"
fi

# 7. API, worker, web
step "API, worker and web app"
[ "$DRY_RUN" = 1 ] || mkdir -p "$RUN_DIR" "$LOG_DIR"
STOPPED=0
for n in web worker api; do stop_own "$n" && STOPPED=1; done
[ "$STOPPED" = 1 ] && { wait_port_free 8080 15 || true; wait_port_free 5173 15 || true; }
REUSE_API=0 REUSE_WEB=0
for spec in api:8080:$API_URL/api/v1/health/live web:5173:$WEB_URL/; do
  name="${spec%%:*}" rest="${spec#*:}"
  port="${rest%%:*}" health="${rest#*:}"
  p="$(port_pid "$port")"
  [ -n "$p" ] || continue
  pname="$(proc_name "$p")"
  if [ "$RESTART" = 1 ]; then
    if [ "$DRY_RUN" = 1 ]; then plan "stop ${pname:-pid $p} (pid $p) holding port $port (--restart)"; continue; fi
    kill_tree "$p"
    wait_port_free "$port" 20 || fail "port $port is still in use after stopping pid $p"
    info "stopped ${pname:-a program} (pid $p) that held port $port (--restart)"
  elif [ "$(http_code "$health")" = 200 ]; then
    [ "$name" = api ] && REUSE_API=1
    [ "$name" = web ] && REUSE_WEB=1
    info "reusing the $name already running on port $port (${pname:-?}, pid $p; not started by this script, pass --restart to replace it)"
  else
    fail "port $port is used by ${pname:-a program} (pid $p) and it is not the ATLAS $name. Close it, or re-run with --restart to stop it."
  fi
done
WORKER_PID="$(find_worker)"
API_HOST="${ATLAS_DEMO_API_HOST:-127.0.0.1}"
if [ "$DRY_RUN" = 1 ]; then
  [ "$REUSE_API" = 1 ] || plan "start the API: $VENV_PY -m uvicorn app.main:app --host $API_HOST --port 8080 (log in $LOG_DIR)"
  if [ -n "$WORKER_PID" ]; then info "a worker is already running (pid $WORKER_PID): it would be reused"; else plan "start the worker: $VENV_PY -m arq app.workers.main.WorkerSettings"; fi
  [ "$REUSE_WEB" = 1 ] || plan "start the web app: vite on port 5173"
else
  [ -x "$VENV_PY" ] || fail "$VENV_PY is missing; the Python install step did not finish"
  API_PID=""
  if [ "$REUSE_API" != 1 ]; then
    start_bg api "$API_DIR" "$VENV_PY" -m uvicorn app.main:app --host "$API_HOST" --port 8080
    API_PID="$STARTED_PID"
  fi
  ok=0
  for _ in $(seq 1 60); do
    [ "$(http_code "$API_URL/api/v1/health/ready")" = 200 ] && { ok=1; break; }
    [ -n "$API_PID" ] && ! alive "$API_PID" && break
    sleep 2
  done
  [ "$ok" = 1 ] || { log_tail api; fail "the API did not become ready on $API_URL"; }
  info "API ready: $API_URL/docs"
  if [ -n "$WORKER_PID" ]; then
    info "reusing the worker already running (pid $WORKER_PID); not started by this script"
  else
    start_bg worker "$API_DIR" "$VENV_PY" -m arq app.workers.main.WorkerSettings
    sleep 3
    alive "$STARTED_PID" || { log_tail worker; warn "the worker stopped right after starting"; }
  fi
  if [ "$REUSE_WEB" != 1 ]; then
    if [ -f "$WEB_DIR/node_modules/vite/bin/vite.js" ]; then start_bg web "$WEB_DIR" node "$WEB_DIR/node_modules/vite/bin/vite.js"
    else start_bg web "$WEB_DIR" pnpm dev; fi
    WEB_PID="$STARTED_PID"
    ok=0
    for _ in $(seq 1 45); do
      [ "$(http_code "$WEB_URL/")" = 200 ] && { ok=1; break; }
      alive "$WEB_PID" || break
      sleep 2
    done
    [ "$ok" = 1 ] || { log_tail web; fail "the web app did not start on $WEB_URL"; }
  fi
  info "web app ready: $WEB_URL"
fi

# 8. connections, preset, agent
step "AI providers, speed preset and demo agent"
ACTIVE_PRESET=private_pocket_tts
HELPER=../../infra/scripts/demo_configure.py
if [ "$DRY_RUN" = 1 ]; then
  plan "sign in as demo@example.com and re-use or create the connections 'Local LLM (Ollama)', 'Local speech (Whisper)', 'Pocket TTS (local-tts)'"
  [ -n "$KEY" ] && plan "save 'Live speech (local-stt)' with the given Deepgram key if there is internet (key not shown)"
  plan "apply private_streaming if that connection exists or was saved, else private_pocket_tts; check models; publish 'Mortgage lead qualifier'"
else
  export ATLAS_DEMO_PASSWORD="$DEMO_PASSWORD"
  if [ -n "$KEY" ]; then export ATLAS_DEMO_DEEPGRAM_KEY="$KEY"; fi
  api_python "$HELPER" connections
  unset ATLAS_DEMO_DEEPGRAM_KEY
  KEY=""
  [ "$PY_EXIT" = 0 ] || fail "configuring the AI provider connections failed (see above)"
  TARGET=private_pocket_tts
  if grep -qx 'STREAMING=yes' "$PY_OUT" && [ "$PRESET" != pocket ]; then
    TARGET=private_streaming
    case " ${SERVICES[*]} " in *" local-stt "*) ;; *) compose up -d local-stt || TARGET=private_pocket_tts ;; esac
    if [ "$TARGET" = private_streaming ] && ! wait_service local-stt 600; then
      warn "the live transcription server is not ready: using Whisper instead"
      TARGET=private_pocket_tts
    fi
  fi
  rm -f "$PY_OUT"
  api_python "$HELPER" finish --stt-model "$STT_MODEL" --preset "$TARGET" --web "$WEB_URL"
  line="$(grep '^PRESET=' "$PY_OUT" | tail -n 1)"
  [ -n "$line" ] && ACTIVE_PRESET="${line#PRESET=}"
  [ "$PY_EXIT" = 0 ] || warn "the demo agent could not be published (see above); fix it in the web app under Agents"
  rm -f "$PY_OUT"
  unset ATLAS_DEMO_PASSWORD
fi

# 9. warm-up
step "Loading the models into memory"
SKIP=kokoro
[ "$ACTIVE_PRESET" = private_streaming ] && SKIP=kokoro,stt
if [ "$SKIP_WARM" = 1 ]; then info "skipped (--skip-warm). Before the demo run: make local-ai-warm"
elif [ "$DRY_RUN" = 1 ]; then plan "run bench_local_models.py --warm-only --stt-model $STT_MODEL --skip $SKIP"
else
  api_python ../../infra/scripts/bench_local_models.py --warm-only --stt-model "$STT_MODEL" --skip "$SKIP"
  rm -f "$PY_OUT"
  [ "$PY_EXIT" = 0 ] || warn "warming the models failed; the first reply of the first call will be slow"
fi

# 10. summary
echo
if [ "$DRY_RUN" = 1 ]; then printf '\033[32mDry run finished. Nothing was changed.\033[0m\n'; exit 0; fi
case "$ACTIVE_PRESET" in
  private_streaming) PRESET_TEXT="Private streaming (self-hosted): live transcription, qwen2.5:1.5b, Pocket TTS voice. Works with no internet." ;;
  *) PRESET_TEXT="Private self-hosted (Pocket TTS): Whisper speech-to-text, qwen2.5:1.5b, Pocket TTS voice. Works with no internet." ;;
esac
printf '\033[32mATLAS demo is ready.\033[0m\n'
echo "  Active preset: $PRESET_TEXT"
echo "  Open:          $WEB_URL"
if [ -n "$GENERATED_PASSWORD" ]; then
  echo "  Sign in:       demo@example.com / $GENERATED_PASSWORD   (generated now and saved in .env as ATLAS_DEMO_PASSWORD; shown only this once)"
else
  echo "  Sign in:       demo@example.com, password: the ATLAS_DEMO_PASSWORD value in .env"
fi
echo "  Test call:     Agents > Mortgage lead qualifier > Test call > Start test call (allow the microphone)"
echo "  Afterwards:    Calls > the newest test call > Response times"
echo "  Logs:          $LOG_DIR"
echo "  Stop all:      make demo-stop   (or: bash infra/scripts/setup_local_demo.sh --stop)"
if [ "${#WARNINGS[@]}" -gt 0 ]; then
  printf '\n\033[33mWarnings (%s):\033[0m\n' "${#WARNINGS[@]}"
  for w in "${WARNINGS[@]}"; do printf '\033[33m  - %s\033[0m\n' "$w"; done
fi
exit 0
