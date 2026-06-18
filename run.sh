#!/usr/bin/env bash
#
# FSKX Runner launcher (macOS / Linux).
#
#   ./run.sh [MODELS_FOLDER] [PORT]
#
# By default it serves (and downloads into) the "fskx_models" folder inside this directory.
# Override per-launch with the arguments above, or persistently in .env (MODELS_DIR / PORT).
# Requires Docker Desktop (or Docker Engine) to be installed and running.

set -euo pipefail
cd "$(dirname "$0")"

IMAGE="fskx-runner"

# Load local config + secrets from .env (next to this script). This is the single config
# file for the tool: API key (ANTHROPIC_API_KEY, or API_KEY alias), default Claude model
# (FSKX_CLAUDE_MODEL), the models/downloads folder (MODELS_DIR) and the web port (PORT).
# Format: KEY=value per line ('#' comments allowed; spaces around '=' and surrounding
# quotes tolerated). Keep it private — it is git/Docker-ignored.
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    key=$(printf '%s' "${line%%=*}" | awk '{$1=$1; print}')
    val=$(printf '%s' "${line#*=}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    case "$val" in
      \"*\") val=${val#\"}; val=${val%\"} ;;
      \'*\') val=${val#\'}; val=${val%\'} ;;
    esac
    [ -n "$key" ] && export "$key=$val"
  done < .env
fi
# Accept API_KEY as a friendly alias for ANTHROPIC_API_KEY.
: "${ANTHROPIC_API_KEY:=${API_KEY:-}}"
export ANTHROPIC_API_KEY
export FSKX_CLAUDE_MODEL="${FSKX_CLAUDE_MODEL:-}"

# Resolve the models folder and port. Precedence: CLI argument > .env > default.
# Default: a dedicated "fskx_models" folder inside fskx-runner (created if missing).
MODELS_DIR="${1:-${MODELS_DIR:-$(pwd)/fskx_models}}"
PORT="${2:-${PORT:-8000}}"
mkdir -p "$MODELS_DIR"
MODELS_DIR="$(cd "$MODELS_DIR" && pwd)"   # absolute path for the docker -v mount

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is not installed or not on PATH."
  echo "Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and try again."
  exit 1
fi

echo "Models folder : $MODELS_DIR"
echo "Building image (first run only — this can take a minute)…"
docker build -t "$IMAGE" .

URL="http://localhost:$PORT"
echo
echo "Starting FSKX Runner at $URL"
echo "(The FIRST run of each model also builds its environment, which may take a few minutes."
echo " Later runs of the same model are fast. Press Ctrl+C to stop.)"
echo

# Persist per-model environments and work files across runs via named volumes.
# The models folder is mounted read-write so repository downloads can be saved there.
# The Docker socket is mounted so the AI-assisted feature can build/run per-model images.
docker run --rm \
  -p "$PORT:8000" \
  -e ANTHROPIC_API_KEY \
  -e FSKX_CLAUDE_MODEL \
  -v "$MODELS_DIR:/models" \
  -v fskx_envs:/opt/conda/envs \
  -v fskx_work:/work \
  -v /var/run/docker.sock:/var/run/docker.sock \
  "$IMAGE" &
CONTAINER_PID=$!

# Open the browser only once the server actually responds (poll /healthz), so the user
# never lands on a transient "didn't send any data" page while the container is starting.
( for _ in $(seq 1 120); do
    curl -fsS "$URL/healthz" >/dev/null 2>&1 && break
    sleep 0.5
  done
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  fi ) >/dev/null 2>&1 || true

wait $CONTAINER_PID
