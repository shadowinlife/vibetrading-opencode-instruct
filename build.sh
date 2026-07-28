#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE_NAME="opencode-serve"
IMAGE_TAG="latest"
REGISTRY="registry.cn-hangzhou.aliyuncs.com/jiefengnewsv2"
PLATFORM="${DOCKER_PLATFORM:-}"
PUSH=false
DRY_RUN=false
MODE="app"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)        IMAGE_TAG="$2"; shift 2 ;;
    --tag=*)      IMAGE_TAG="${1#*=}"; shift ;;
    --push)       PUSH=true; shift ;;
    --dry-run)    DRY_RUN=true; shift ;;
    --base)       MODE="base"; shift ;;
    --app)        MODE="app"; shift ;;
    --help|-h)
      echo "Usage: $0 [--base|--app] [--tag TAG] [--push] [--dry-run]"
      echo ""
      echo "Modes:"
      echo "  --base    Build opencode-serve-base (heavy deps, rarely)"
      echo "  --app     Build opencode-serve app image (default)"
      echo ""
      echo "Options:"
      echo "  --tag TAG     Image tag (default: latest)"
      echo "  --push        Push to $REGISTRY after build"
      echo "  --dry-run     Show commands without executing"
      exit 0
      ;;
    *) shift ;;
  esac
done

run() {
  if $DRY_RUN; then
    echo "[DRY-RUN] $*"
  else
    echo ">>> $*"
    "$@"
  fi
}

# ---------------------------------------------------------------------------
# Base image build
# ---------------------------------------------------------------------------
if [ "$MODE" = "base" ]; then
  BASE_TAG="${IMAGE_TAG:-latest}"
  echo "=== Building base image: opencode-serve-base:${BASE_TAG} ==="
  PLATFORM_ARG=()
  [ -n "$PLATFORM" ] && PLATFORM_ARG=(--platform "$PLATFORM")
  run docker build \
    "${PLATFORM_ARG[@]}" \
    -t "opencode-serve-base:${BASE_TAG}" \
    -f "$SCRIPT_DIR/Dockerfile.base" \
    "$SCRIPT_DIR"

  if $PUSH; then
    FULL_IMAGE="${REGISTRY}/opencode-serve-base:${BASE_TAG}"
    run docker tag "opencode-serve-base:${BASE_TAG}" "$FULL_IMAGE"
    run docker push "$FULL_IMAGE"
    echo "=== Base push complete: $FULL_IMAGE ==="
  fi
  echo "=== Base image done: opencode-serve-base:${BASE_TAG} ==="
  exit 0
fi

# ---------------------------------------------------------------------------
# App image build
# ---------------------------------------------------------------------------
VT_SOURCE="${VT_SOURCE:-../Vibe-Trading}"
VENDOR_DIR="$SCRIPT_DIR/vendor/Vibe-Trading"

if [[ "$VT_SOURCE" == http* ]]; then
    echo "=== Cloning Vibe-Trading from $VT_SOURCE (mymain branch) ==="
    rm -rf "$VENDOR_DIR"
    git clone --depth 1 -b mymain "$VT_SOURCE" "$VENDOR_DIR"
    echo "=== VT cloned: $(find "$VENDOR_DIR" -type f -name '*.py' | wc -l) Python files ==="
elif [ -d "$VT_SOURCE" ]; then
    echo "=== Vendoring Vibe-Trading from $VT_SOURCE (mymain branch) ==="
    mkdir -p "$VENDOR_DIR"
    VT_BRANCH=$(cd "$VT_SOURCE" && git branch --show-current 2>/dev/null || echo "unknown")
    if [ "$VT_BRANCH" != "mymain" ]; then
        echo "WARNING: VT source is on branch '$VT_BRANCH', expected 'mymain'"
    fi
    rsync -a \
        --exclude='.git' --exclude='frontend/' --exclude='node_modules/' \
        --exclude='__pycache__/' --exclude='*.pyc' --exclude='*.egg-info/' \
        --exclude='.venv/' --exclude='.codex/' --exclude='assets/' \
        --exclude='tests/' --exclude='agent/tests/' \
        "$VT_SOURCE/" "$VENDOR_DIR/"
    echo "=== VT vendored: $(find "$VENDOR_DIR" -type f -name '*.py' | wc -l) Python files ==="
else
    echo "ERROR: Vibe-Trading source not found at $VT_SOURCE"
    exit 1
fi

echo "=== Building app image: ${IMAGE_NAME}:${IMAGE_TAG} ==="
run docker build \
  "${PLATFORM_ARG[@]}" \
  -t "${IMAGE_NAME}:${IMAGE_TAG}" \
  -f "$SCRIPT_DIR/Dockerfile" \
  "$SCRIPT_DIR"

if $PUSH; then
  FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"
  run docker tag "${IMAGE_NAME}:${IMAGE_TAG}" "$FULL_IMAGE"
  run docker push "$FULL_IMAGE"
  echo "=== Push complete: $FULL_IMAGE ==="
fi

echo "=== App image done: ${IMAGE_NAME}:${IMAGE_TAG} ==="
echo ""
echo "Run with docker-compose:"
echo "  cp .env.example .env"
echo "  docker compose up -d"