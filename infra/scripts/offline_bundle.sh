#!/usr/bin/env bash
# Export or import everything the self-hosted ATLAS demo needs as plain files, for a machine
# with slow or no internet. Bash twin of offline_bundle.ps1; bundles are interchangeable
# (same layout, manifest.tsv and tar format). See docs/university-demo.md.
#
#   bash infra/scripts/offline_bundle.sh export <dir> [options]
#   bash infra/scripts/offline_bundle.sh import <dir> [options]
#
#   --dry-run      print the plan and estimated sizes, change nothing
#   --with-deps    export: also a Python 3.12 runtime, the uv cache and the pnpm store, so
#                  "uv sync" / "pnpm install" work offline (same OS and CPU type only)
#   --with-data    export: also the ATLAS and voice-service databases, voice storage and
#                  .env, to move a configured machine as it is. THAT COPIES EVERY ACCOUNT,
#                  PASSWORD HASH, API KEY AND SECRET: keep the bundle like a password.
#                  import: restore those (and .env) too; without it they are skipped
#   --force        export: overwrite files already in the bundle
#                  import: replace volumes that already hold data, and a different .env
#   --only a,b     only these images/volumes (names as in the plan, "deps" for the code
#                  dependencies; export also accepts any other existing image or volume)
#
# export writes images/<image>.tar (docker save), volumes/<volume>.tar (tarred in a helper
# container), deps/*.tar, env/dot-env, source/atlas-source-<commit>.zip, manifest.tsv
# (sizes and SHA-256), SHA256SUMS and README.txt.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
API_DIR="$ROOT/services/api"
WEB_DIR="$ROOT/apps/web"
DEPS_TARGET="$ROOT/.atlas-run/deps"
PROFILES=(--profile voice --profile local-ai)
# busybox tar is in this image, and the image is part of the bundle: import needs no internet.
HELPER_IMAGE="redis:7.4-alpine"
MODEL_KEYS="local_llm_models local_speech_models local_tts_models local_stt_models"
DATA_KEYS="pg_data redis_data voice_db_data voice_redis_data voice_storage_data"

ACTION="${1:-}"; DIR="${2:-}"
if [ $# -lt 2 ]; then sed -n '2,25p' "$0"; exit 2; fi
shift 2
DRY_RUN=0 WITH_DEPS=0 WITH_DATA=0 FORCE=0 ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--what-if) DRY_RUN=1 ;;
    --with-deps) WITH_DEPS=1 ;;
    --with-data) WITH_DATA=1 ;;
    --force) FORCE=1 ;;
    --only) ONLY="${2:-}"; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$ACTION" in export|import) ;; *) echo "first argument must be export or import" >&2; exit 2 ;; esac

case "$(uname -s)" in
  Linux*) OS=linux ;; Darwin*) OS=mac ;; MINGW*|MSYS*|CYGWIN*) OS=windows ;; *) OS=other ;;
esac
ARCH="$(uname -m)"; case "$ARCH" in x86_64|amd64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; esac
PLATFORM="$OS-$ARCH"

step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
info() { printf '    %s\n' "$1"; }
warn() { printf '    \033[33mWARNING: %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31mERROR: %s\033[0m\n' "$1"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
# Git Bash rewrites arguments that look like paths; container paths such as /vol must stay.
drun() { MSYS_NO_PATHCONV=1 docker run "$@"; }

human() { awk -v b="${1:-0}" 'BEGIN { if (b >= 1073741824) printf "%.1f GB", b/1073741824; else if (b >= 1048576) printf "%.0f MB", b/1048576; else printf "%.0f KB", b/1024 }'; }
sha256() { if have sha256sum; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
fsize() { wc -c <"$1" | tr -d ' '; }
safe_name() { printf '%s' "$1" | tr '/:@' '___' | tr "\\\\" _; }
selected() {  # any of the given names in --only (or no --only)
  [ -z "$ONLY" ] && return 0
  local n
  for n in "$@"; do case ",$ONLY," in *",$n,"*) return 0 ;; esac; done
  return 1
}
image_exists() { docker image inspect "$1" >/dev/null 2>&1; }
volume_exists() { docker volume inspect "$1" >/dev/null 2>&1; }
volume_kb() { drun --rm -v "$1:/vol:ro" --entrypoint du "$HELPER_IMAGE" -sk /vol 2>/dev/null | awk '{print $1; exit}'; }
volume_empty() { [ -z "$(drun --rm -v "$1:/vol:ro" --entrypoint sh "$HELPER_IMAGE" -c 'ls -A /vol | head -n 1' 2>/dev/null)" ]; }
volume_users() { docker ps -q --filter "volume=$1" 2>/dev/null; }
volume_labels() {
  docker volume inspect --format '{{range $k, $v := .Labels}}{{$k}}={{$v}};{{end}}' "$1" 2>/dev/null | sed 's/;$//'
}
volume_name() {  # compose key -> real volume name (project prefix)
  local name
  name="$(printf '%s\n' "$VOLUME_MAP" | awk -v k="$1" '$1==k {print $2; exit}')"
  printf '%s' "${name:-atlas_$1}"
}

resolve_volume_map() {
  # "key name" lines from the compose config. Only names are kept; the config itself holds
  # secrets from .env and is never printed.
  VOLUME_MAP="$(cd "$ROOT" && docker compose "${PROFILES[@]}" config --format json 2>/dev/null |
    tr -d '\n' | sed -n 's/.*"volumes": *{\(.*\)}.*/\1/p' |
    grep -o '"[a-z_]*": *{ *"name": *"[^"]*"' | sed -E 's/"([a-z_]*)": *\{ *"name": *"([^"]*)"/\1 \2/')" || true
}

# ------------------------------------------------------------------------------ export
do_export() {
  step "Planning the bundle"
  docker info --format '{{.ServerVersion}}' >/dev/null 2>&1 || fail "Docker is not running."
  local images
  images="$(cd "$ROOT" && docker compose "${PROFILES[@]}" config --images 2>/dev/null | sort -u)"
  [ -n "$images" ] || fail "could not read the image list from docker compose (is .env present?)"
  resolve_volume_map
  image_exists "$HELPER_IMAGE" || fail "$HELPER_IMAGE is needed to read the volumes; run the demo setup once (it pulls it)"

  PLAN=()  # kind|name|bytes
  local img key kind name size total=0 missing=() o
  while read -r img; do
    [ -n "$img" ] || continue
    selected "$img" || continue
    if image_exists "$img"; then size="$(docker image inspect --format '{{.Size}}' "$img")"; else size=0; missing+=("$img"); fi
    PLAN+=("image|$img|$size")
  done <<<"$images"
  local keys="$MODEL_KEYS"
  [ "$WITH_DATA" = 1 ] && keys="$keys $DATA_KEYS"
  for key in $keys; do
    name="$(volume_name "$key")"
    selected "$key" "$name" || continue
    kind=volume; case " $DATA_KEYS " in *" $key "*) kind=data-volume ;; esac
    if volume_exists "$name"; then size=$(( $(volume_kb "$name") * 1024 )); else size=0; missing+=("$name"); fi
    PLAN+=("$kind|$name|$size")
  done
  if [ -n "$ONLY" ]; then
    for o in ${ONLY//,/ }; do
      [ "$o" = deps ] && continue
      printf '%s\n' "${PLAN[@]+"${PLAN[@]}"}" | grep -q "|$o|" && continue
      case " $MODEL_KEYS $DATA_KEYS " in *" $o "*) continue ;; esac
      if image_exists "$o"; then PLAN+=("image|$o|$(docker image inspect --format '{{.Size}}' "$o")")
      elif volume_exists "$o"; then PLAN+=("volume|$o|$(( $(volume_kb "$o") * 1024 ))")
      else warn "'$o' is neither an image nor a volume here: skipped"; fi
    done
  fi

  echo
  printf '    %-12s %-48s %10s\n' KIND NAME ESTIMATE
  local entry
  for entry in "${PLAN[@]+"${PLAN[@]}"}"; do
    IFS='|' read -r kind name size <<<"$entry"
    total=$((total + size))
    if [ "$size" -gt 0 ]; then printf '    %-12s %-48s %10s\n' "$kind" "$name" "$(human "$size")"
    else printf '    %-12s %-48s %10s\n' "$kind" "$name" missing; fi
  done
  DEPS_WANTED=0
  if [ "$WITH_DEPS" = 1 ] && { [ -z "$ONLY" ] || selected deps; }; then DEPS_WANTED=1; fi
  if [ "$DEPS_WANTED" = 1 ]; then
    printf '    %-12s %-48s %10s\n' deps "Python 3.12 + uv cache + pnpm store" "~0.6 GB"; total=$((total + 644245094))
  fi
  if [ -z "$ONLY" ]; then printf '    %-12s %-48s %10s\n' source "git archive of HEAD" "~0.1 GB"; total=$((total + 107374182)); fi
  printf '    %-12s %-48s %10s\n' "" "total (image sizes are as stored by Docker)" "$(human "$total")"
  [ "${#missing[@]}" -gt 0 ] && warn "not on this machine, so not exported: ${missing[*]}. Run the demo setup once (it downloads and builds them), then export again."
  [ "$WITH_DATA" = 1 ] && warn "--with-data copies the databases, voice storage and .env: every account, password hash, API key and secret. Keep the bundle like a password and import it together with that .env."
  if [ "$DRY_RUN" = 1 ]; then
    if [ -d "$DIR" ] || [ -d "$(dirname "$DIR")" ]; then
      local probe="$DIR"; [ -d "$probe" ] || probe="$(dirname "$DIR")"
      info "free space at $probe: $(human $(( $(df -Pk "$probe" | awk 'NR==2 {print $4}') * 1024 )))"
    fi
    printf '\n\033[32mDry run: nothing was written.\033[0m\n'
    return 0
  fi

  mkdir -p "$DIR/images" "$DIR/volumes" "$DIR/deps" "$DIR/env" "$DIR/source" || fail "cannot create $DIR"
  DIR="$(cd "$DIR" && pwd)"
  info "free space at $DIR: $(human $(( $(df -Pk "$DIR" | awk 'NR==2 {print $4}') * 1024 )))"
  ROWS=()
  local platform_flag=0 rel file plat
  docker save --help 2>/dev/null | grep -q -- '--platform' && platform_flag=1

  step "Saving Docker images"
  for entry in "${PLAN[@]+"${PLAN[@]}"}"; do
    IFS='|' read -r kind name size <<<"$entry"
    [ "$kind" = image ] && [ "$size" -gt 0 ] || continue
    rel="images/$(safe_name "$name").tar"; file="$DIR/$rel"
    plat="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$name" 2>/dev/null)"
    if [ -f "$file" ] && [ "$FORCE" != 1 ]; then info "$name: already in the bundle"
    else
      info "$name -> $rel"
      if [ "$platform_flag" = 1 ] && [ -n "$plat" ]; then docker save --platform "$plat" -o "$file.partial" "$name"
      else docker save -o "$file.partial" "$name"; fi || { rm -f "$file.partial"; fail "docker save $name failed"; }
      mv -f "$file.partial" "$file"
    fi
    ROWS+=("image"$'\t'"$name"$'\t'"$rel"$'\t'"$(fsize "$file")"$'\t'"$(sha256 "$file")"$'\t'"$plat")
  done

  local stopped=() c
  for entry in "${PLAN[@]+"${PLAN[@]}"}"; do
    IFS='|' read -r kind name size <<<"$entry"
    [ "$kind" = data-volume ] || continue
    volume_exists "$name" || continue
    for c in $(volume_users "$name"); do
      case " ${stopped[*]+"${stopped[*]}"} " in *" $c "*) ;; *) stopped+=("$c") ;; esac
    done
  done
  step "Saving Docker volumes"
  if [ "${#stopped[@]}" -gt 0 ]; then
    info "stopping ${#stopped[@]} container(s) that use the data volumes, for a consistent copy"
    docker stop "${stopped[@]}" >/dev/null
  fi
  for entry in "${PLAN[@]+"${PLAN[@]}"}"; do
    IFS='|' read -r kind name size <<<"$entry"
    [ "$kind" != image ] || continue
    volume_exists "$name" || continue
    rel="volumes/$name.tar"; file="$DIR/$rel"
    if [ -f "$file" ] && [ "$FORCE" != 1 ]; then info "$name: already in the bundle"
    else
      info "$name -> $rel"
      drun --rm -v "$name:/vol:ro" --entrypoint tar "$HELPER_IMAGE" -cf - -C /vol . >"$file.partial" ||
        { rm -f "$file.partial"; fail "saving volume $name failed"; }
      mv -f "$file.partial" "$file"
    fi
    ROWS+=("$kind"$'\t'"$name"$'\t'"$rel"$'\t'"$(fsize "$file")"$'\t'"$(sha256 "$file")"$'\t'"$(volume_labels "$name")")
  done
  if [ "${#stopped[@]}" -gt 0 ]; then docker start "${stopped[@]}" >/dev/null; info "restarted those containers"; fi

  if [ "$WITH_DATA" = 1 ]; then
    step "Settings file"
    if [ -f "$ROOT/.env" ]; then
      cp "$ROOT/.env" "$DIR/env/dot-env" && chmod 600 "$DIR/env/dot-env" 2>/dev/null
      ROWS+=("env"$'\t'".env"$'\t'"env/dot-env"$'\t'"$(fsize "$DIR/env/dot-env")"$'\t'"$(sha256 "$DIR/env/dot-env")"$'\t')
      info "copied .env (contains secrets)"
    else
      warn "no .env here: the data volumes are useless without the .env that created them"
    fi
  fi

  [ "$DEPS_WANTED" = 1 ] && export_deps

  if [ -z "$ONLY" ] && have git; then
    step "Source code"
    local sha
    sha="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)"
    if [ -n "$sha" ]; then
      rel="source/atlas-source-$sha.zip"
      if git -C "$ROOT" archive --format=zip -o "$DIR/$rel" HEAD; then
        ROWS+=("source"$'\t'"atlas-source-$sha"$'\t'"$rel"$'\t'"$(fsize "$DIR/$rel")"$'\t'"$(sha256 "$DIR/$rel")"$'\t')
        [ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ] && warn "the zip holds commit $sha only; uncommitted changes are not in it"
        info "commit $sha -> $rel"
      else
        warn "git archive failed: copy the repository yourself"
      fi
    fi
  fi
  write_manifest
}

export_deps() {
  step "Code dependencies (same OS and CPU type only)"
  have uv || fail "uv is needed for --with-deps"
  have pnpm || fail "pnpm is needed for --with-deps"
  local work="$DIR/deps/work" f name
  rm -rf "$work"
  mkdir -p "$work/uv-python" "$work/uv-cache" "$work/pnpm-store" "$work/web"
  info "Python 3.12 runtime (downloaded now)"
  uv python install 3.12 --install-dir "$work/uv-python" --no-bin --no-registry || fail "uv python install failed (needs internet)"
  info "API packages into a fresh uv cache (a throwaway environment; services/api/.venv is not touched)"
  (cd "$API_DIR" && UV_CACHE_DIR="$work/uv-cache" UV_PYTHON_INSTALL_DIR="$work/uv-python" \
    UV_PROJECT_ENVIRONMENT="$work/venv" uv sync --frozen --managed-python) || fail "uv sync into the bundle cache failed (needs internet)"
  info "web packages into a fresh pnpm store (from a copy of the lockfile; apps/web is not touched)"
  for f in package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc; do
    [ -f "$WEB_DIR/$f" ] && cp "$WEB_DIR/$f" "$work/web/$f"
  done
  (cd "$work/web" && pnpm fetch --store-dir "$work/pnpm-store") || fail "pnpm fetch into the bundle store failed (needs internet)"
  # uv links the minor version (cpython-3.12-...) to the patch version; uv finds the patch
  # version without the link, and a link would not survive a FAT/exFAT stick.
  find "$work/uv-python" -mindepth 1 -maxdepth 1 -type l -exec rm -f {} + 2>/dev/null
  if [ "$OS" = windows ]; then
    while IFS= read -r f; do
      [ -n "$f" ] && cmd //c rmdir "$(cygpath -w "$f")" >/dev/null 2>&1
    done < <(cd "$work/uv-python" && cmd //c "dir /AL /B" 2>/dev/null | tr -d '\r' | sed "s|^|$work/uv-python/|")
  fi
  for name in uv-python uv-cache pnpm-store; do
    tar -cf "$DIR/deps/$name.tar" -C "$work/$name" . || fail "packing $name failed"
    ROWS+=("deps"$'\t'"$name"$'\t'"deps/$name.tar"$'\t'"$(fsize "$DIR/deps/$name.tar")"$'\t'"$(sha256 "$DIR/deps/$name.tar")"$'\t'"$PLATFORM")
    info "$name -> deps/$name.tar ($(human "$(fsize "$DIR/deps/$name.tar")"))"
  done
  rm -rf "$work"
}

write_manifest() {
  local manifest="$DIR/manifest.tsv" tmp names old="" commit pnpm_v count=0 bytes=0 row
  tmp="$(mktemp)"
  if [ -f "$manifest" ] && [ -n "$ONLY" ]; then
    # A partial export adds to an existing bundle instead of replacing its manifest.
    names="$(printf '%s\n' "${ROWS[@]+"${ROWS[@]}"}" | cut -f2)"
    old="$(grep -v -e '^#' -e '^meta' "$manifest" | awk -F'\t' -v n="$names" 'BEGIN {split(n, a, "\n"); for (i in a) skip[a[i]]=1} !($2 in skip)')"
  fi
  commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)"
  # The version that really fills the store: in apps/web a newer pnpm runs the pinned one.
  pnpm_v="$(cd "$WEB_DIR" && pnpm --version 2>/dev/null)"
  {
    printf '#atlas-offline-bundle\t1\n#kind\tname\tfile\tbytes\tsha256\textra\n'
    printf 'meta\tcreated\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'meta\tplatform\t%s\nmeta\tcommit\t%s\nmeta\tpnpm\t%s\n' "$PLATFORM" "$commit" "$pnpm_v"
    printf 'meta\twith_data\t%s\n' "$([ "$WITH_DATA" = 1 ] && echo yes || echo no)"
    [ -n "$old" ] && printf '%s\n' "$old"
    for row in "${ROWS[@]+"${ROWS[@]}"}"; do printf '%s\n' "$row"; done
  } >"$tmp"
  mv -f "$tmp" "$manifest"
  grep -v -e '^#' -e '^meta' "$manifest" | awk -F'\t' '{gsub(/\\/, "/", $3); print $5 "  " $3}' >"$DIR/SHA256SUMS"
  {
    echo "ATLAS offline bundle (made $(date +%Y-%m-%d) from commit $commit)"
    echo
    echo "1. Unzip source/atlas-source-*.zip (or clone the repository) on the demo machine."
    echo "2. Import:  bash infra/scripts/offline_bundle.sh import <this folder>"
    echo "            (Windows: powershell -NoProfile -ExecutionPolicy Bypass -File infra\\scripts\\offline_bundle.ps1 import <this folder>)"
    echo "3. Set up:  make demo     (Windows: .\\make.ps1 demo)"
    echo
    echo "manifest.tsv lists every file with its size and SHA-256; SHA256SUMS works with 'sha256sum -c'."
    echo "See docs/university-demo.md in the repository."
    [ "$WITH_DATA" = 1 ] && printf '\nTHIS BUNDLE CONTAINS DATABASES AND .env: ACCOUNTS, PASSWORD HASHES, API KEYS AND SECRETS.\n'
  } >"$DIR/README.txt"
  while IFS=$'\t' read -r kind _ _ b _ _; do
    case "$kind" in '#'*|meta|'') continue ;; esac
    count=$((count + 1)); bytes=$((bytes + b))
  done <"$manifest"
  printf '\n\033[32mBundle written to %s (%s files, %s). Manifest: manifest.tsv\033[0m\n' "$DIR" "$count" "$(human "$bytes")"
}

# ------------------------------------------------------------------------------ import
do_import() {
  local manifest="$DIR/manifest.tsv"
  [ -f "$manifest" ] || fail "no manifest.tsv in $DIR"
  DIR="$(cd "$DIR" && pwd)"
  docker info --format '{{.ServerVersion}}' >/dev/null 2>&1 || fail "Docker is not running. Start Docker and re-run."
  local m_commit m_created m_platform m_pnpm m_data
  m_commit="$(awk -F'\t' '$1=="meta" && $2=="commit" {print $3}' "$manifest")"
  m_created="$(awk -F'\t' '$1=="meta" && $2=="created" {print $3}' "$manifest")"
  m_platform="$(awk -F'\t' '$1=="meta" && $2=="platform" {print $3}' "$manifest")"
  m_pnpm="$(awk -F'\t' '$1=="meta" && $2=="pnpm" {print $3}' "$manifest")"
  m_data="$(awk -F'\t' '$1=="meta" && $2=="with_data" {print $3}' "$manifest")"
  info "bundle from commit $m_commit, made $m_created on $m_platform"
  [ "$m_data" = yes ] && [ "$WITH_DATA" != 1 ] && info "this bundle also holds databases and .env; they are restored only with --with-data"

  ROWS=()
  local kind name file bytes sha extra short
  while IFS=$'\t' read -r kind name file bytes sha extra; do
    case "$kind" in '#'*|meta|'') continue ;; esac
    short="${name#*_}"
    selected "$name" "$short" || continue
    file="${file//\\//}"
    ROWS+=("$kind"$'\t'"$name"$'\t'"$file"$'\t'"$bytes"$'\t'"$sha"$'\t'"${extra:-}")
  done <"$manifest"

  step "Checking the files"
  local bad=() row
  for row in "${ROWS[@]+"${ROWS[@]}"}"; do
    IFS=$'\t' read -r kind name file bytes sha extra <<<"$row"
    if [ ! -f "$DIR/$file" ]; then bad+=("$file is missing"); continue; fi
    if [ "$(fsize "$DIR/$file")" != "$bytes" ]; then bad+=("$file has the wrong size"); continue; fi
    [ "$DRY_RUN" = 1 ] && continue
    [ "$(sha256 "$DIR/$file")" = "$sha" ] || bad+=("$file is damaged (SHA-256 differs)")
  done
  if [ "${#bad[@]}" -gt 0 ]; then printf '    %s\n' "${bad[@]}"; fail "bundle check failed"; fi
  if [ "$DRY_RUN" = 1 ]; then info "all ${#ROWS[@]} files are present with the right size (checksums are verified on a real import)"
  else info "all ${#ROWS[@]} files verified (size and SHA-256)"; fi

  # images, the helper image first
  local ordered=() header=0
  for row in "${ROWS[@]+"${ROWS[@]}"}"; do
    IFS=$'\t' read -r kind name _ <<<"$row"
    [ "$kind" = image ] || continue
    if [ "$name" = "$HELPER_IMAGE" ]; then ordered=("$row" "${ordered[@]+"${ordered[@]}"}"); else ordered+=("$row"); fi
  done
  for row in "${ordered[@]+"${ordered[@]}"}"; do
    IFS=$'\t' read -r kind name file bytes sha extra <<<"$row"
    [ "$header" = 1 ] || { step "Docker images"; header=1; }
    if image_exists "$name" && [ "$FORCE" != 1 ]; then info "$name: already here"; continue; fi
    if [ "$DRY_RUN" = 1 ]; then info "[dry run] would load $file ($(human "$bytes"))"; continue; fi
    info "loading $name ($(human "$bytes"))"
    docker load -i "$DIR/$file" || fail "docker load $file failed"
  done

  header=0
  local exists p labels
  for row in "${ROWS[@]+"${ROWS[@]}"}"; do
    IFS=$'\t' read -r kind name file bytes sha extra <<<"$row"
    case "$kind" in volume) ;; data-volume) [ "$WITH_DATA" = 1 ] || continue ;; *) continue ;; esac
    if [ "$header" = 0 ]; then
      step "Docker volumes"; header=1
      image_exists "$HELPER_IMAGE" || [ "$DRY_RUN" = 1 ] || fail "$HELPER_IMAGE is needed to restore volumes; import the images first"
    fi
    exists=0; volume_exists "$name" && exists=1
    if [ "$exists" = 1 ] && ! volume_empty "$name"; then
      if [ "$FORCE" != 1 ]; then info "$name: already holds data, left alone (use --force to replace it)"; continue; fi
      [ -z "$(volume_users "$name")" ] || fail "$name is in use by a running container. Stop the demo first: make demo-stop"
    fi
    if [ "$DRY_RUN" = 1 ]; then info "[dry run] would restore $name from $file ($(human "$bytes"))"; continue; fi
    if [ "$exists" = 0 ]; then
      labels=()
      IFS=';' read -r -a parts <<<"${extra:-}"
      for p in "${parts[@]+"${parts[@]}"}"; do [ -n "$p" ] && labels+=(--label "$p"); done
      docker volume create "${labels[@]+"${labels[@]}"}" "$name" >/dev/null || fail "could not create volume $name"
    else
      drun --rm -v "$name:/vol" --entrypoint sh "$HELPER_IMAGE" -c 'find /vol -mindepth 1 -maxdepth 1 -exec rm -rf {} +'
    fi
    info "restoring $name ($(human "$bytes"))"
    drun --rm -i -v "$name:/vol" --entrypoint tar "$HELPER_IMAGE" -xf - -C /vol <"$DIR/$file" || fail "restoring $name failed"
  done

  if [ "$WITH_DATA" = 1 ]; then
    for row in "${ROWS[@]+"${ROWS[@]}"}"; do
      IFS=$'\t' read -r kind name file bytes sha extra <<<"$row"
      [ "$kind" = env ] || continue
      step "Settings file (.env)"
      if [ ! -f "$ROOT/.env" ]; then
        if [ "$DRY_RUN" = 1 ]; then info "[dry run] would copy the bundle's .env"
        else cp "$DIR/$file" "$ROOT/.env" && chmod 600 "$ROOT/.env" 2>/dev/null; info "copied .env from the bundle"; fi
      elif [ "$(sha256 "$ROOT/.env")" = "$sha" ]; then info ".env is already the bundle's"
      elif [ "$FORCE" = 1 ]; then
        local backup
        backup="$ROOT/.env.backup-$(date +%Y%m%d-%H%M%S)"
        if [ "$DRY_RUN" = 1 ]; then info "[dry run] would back up .env to $backup and copy the bundle's"
        else cp "$ROOT/.env" "$backup" && cp -f "$DIR/$file" "$ROOT/.env"; info "replaced .env (old one: $backup)"; fi
      else
        warn ".env differs from the bundle's. The restored databases only work with the bundle's .env: re-run with --with-data --force to replace it (the current one is backed up)."
      fi
    done
  fi

  header=0
  local dest pnpm_here
  for row in "${ROWS[@]+"${ROWS[@]}"}"; do
    IFS=$'\t' read -r kind name file bytes sha extra <<<"$row"
    [ "$kind" = deps ] || continue
    if [ "$header" = 0 ]; then
      step "Code dependencies"; header=1
      [ "$m_platform" = "$PLATFORM" ] || warn "the bundle's code dependencies were made on $m_platform, this machine is $PLATFORM: they will not work here; setup needs internet for them"
      # Outside apps/web: there a newer pnpm would try to download the pinned version.
      pnpm_here="$(cd "$ROOT" && pnpm --version 2>/dev/null)"
      if [ -n "$pnpm_here" ] && [ -n "$m_pnpm" ] && [ "${pnpm_here%%.*}" != "${m_pnpm%%.*}" ]; then
        warn "the pnpm store was made with pnpm $m_pnpm, this machine has pnpm $pnpm_here: install the same major version for an offline install"
      fi
    fi
    dest="$DEPS_TARGET/$name"
    if [ -d "$dest" ] && [ "$FORCE" != 1 ]; then info "$name: already unpacked in $dest"; continue; fi
    if [ "$DRY_RUN" = 1 ]; then info "[dry run] would unpack $file into $dest"; continue; fi
    rm -rf "$dest"; mkdir -p "$dest"
    tar -xf "$DIR/$file" -C "$dest" || fail "unpacking $file failed"
    info "$name -> $dest"
  done

  echo
  if [ "$DRY_RUN" = 1 ]; then printf '\033[32mDry run: nothing was changed.\033[0m\n'
  else printf '\033[32mImport finished. Next: make demo\033[0m\n'; fi
}

if [ "$ACTION" = export ]; then do_export; else do_import; fi
exit 0
