#!/usr/bin/env bash
# Run the API's tests on the Oracle server instead of this PC.
#
#   bash infra/scripts/remote_api_tests.sh tests/test_campaigns.py       one file
#   bash infra/scripts/remote_api_tests.sh                               the whole suite
#   bash infra/scripts/remote_api_tests.sh -k "webhook" tests/test_calls.py
#
# Copies services/api (without .venv and caches) to ~/atlas-dev on the server, then runs
# pytest there in a python:3.12 container on the stack's Docker network, against a database
# made for the run (atlas_test_<id>, dropped afterwards) and a Redis database of its own, so
# the deployed stack and other runs are untouched. Dependencies are installed once into a
# named volume from uv.lock. Runs are serialised with a lock. Needs the server's SSH key:
# ATLAS_TEST_HOST, ATLAS_TEST_KEY and ATLAS_TEST_KNOWN_HOSTS override the defaults below.
set -euo pipefail

HOST="${ATLAS_TEST_HOST:-opc@129.80.159.225}"
KEY="${ATLAS_TEST_KEY:-/d/dev-cache/ssh/oci_server.key}"
KNOWN_HOSTS="${ATLAS_TEST_KNOWN_HOSTS:-/d/dev-cache/ssh/known_hosts}"
SSH=(ssh -i "$KEY" -o UserKnownHostsFile="$KNOWN_HOSTS" -o BatchMode=yes -o ServerAliveInterval=30 "$HOST")

cd "$(dirname "$0")/../.."
tar -cf - \
  --exclude='services/api/.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
  --exclude='.mypy_cache' --exclude='.ruff_cache' --exclude='*.pyc' \
  services/api | "${SSH[@]}" 'mkdir -p ~/atlas-dev && tar -C ~/atlas-dev -xf -' 2>&1 | grep -v "post-quantum\|store now\|upgraded\|openssh.com/pq" || true

# Quote the pytest arguments for the remote shell.
ARGS=""
for a in "$@"; do ARGS="$ARGS $(printf '%q' "$a")"; done

"${SSH[@]}" "bash -s" <<EOF 2>&1 | grep -v "post-quantum\|store now\|upgraded\|openssh.com/pq"
set -euo pipefail
PW=\$(sed -n 's/^ATLAS_POSTGRES_PASSWORD=//p' ~/atlas/.env | tail -n 1 | tr -d '"\r')
PW=\${PW:-atlas}
DB="atlas_test_\$(date +%s)_\$RANDOM"
RDB=\$((10 + RANDOM % 6))
cleanup() { docker exec atlas-postgres-1 psql -U atlas -d atlas -qc "DROP DATABASE IF EXISTS \$DB" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker exec atlas-postgres-1 psql -U atlas -d atlas -qc "CREATE DATABASE \$DB" >/dev/null
# One run at a time: the dependencies volume must not be synced by two runs at once.
exec 9>/tmp/atlas-dev-tests.lock
flock 9
# The deployed checkout's vendored runtime stands in for the repository root (one test reads it).
docker run --rm --network atlas_default \
  -v ~/atlas-dev:/repo -v ~/atlas/vendor:/repo/vendor:ro -v atlas-dev-venv-repo:/repo/services/api/.venv -v atlas-dev-uv:/root/.cache/uv \
  -w /repo/services/api -e UV_LINK_MODE=copy -e UV_PYTHON_DOWNLOADS=never -e PYTHONPATH=/repo/services/api \
  -e ATLAS_TEST_DATABASE_URL="postgresql+asyncpg://atlas:\${PW}@postgres:5432/\$DB" \
  -e ATLAS_TEST_REDIS_URL="redis://redis:6379/\$RDB" \
  python:3.12.15-slim-bookworm sh -c 'pip install -q --root-user-action=ignore --disable-pip-version-check uv >/dev/null && uv sync -q --frozen --no-install-project && uv run --no-sync pytest -q -p no:cacheprovider '"$ARGS"
EOF
