#!/usr/bin/env bash
# =============================================================================
# build.sh — Build opencode-serve Docker image
#
# Usage:
#   ./build.sh                         # docker build (default tag: opencode-serve:latest)
#   ./build.sh --tag v1.0.0            # custom tag
#   ./build.sh --push                  # build + push to registry
#   ./build.sh --tag v1.0.0 --push
#   ./build.sh --dry-run               # show commands without executing
#
# Prerequisites:
#   - docker installed
#   - docker login to registry (for --push)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE_NAME="opencode-serve"
IMAGE_TAG="latest"
REGISTRY="registry.cn-hangzhou.aliyuncs.com/jiefengnewsv2"
PLATFORM=""
PUSH=false
DRY_RUN=false

for arg in "$@"; do
  case "$arg" in
    --tag)        IMAGE_TAG="$2"; shift 2 ;;
    --tag=*)      IMAGE_TAG="${arg#*=}" ;;
    --push)       PUSH=true ;;
    --dry-run)    DRY_RUN=true ;;
    --help|-h)
      echo "Usage: $0 [--tag TAG] [--push] [--dry-run]"
      echo ""
      echo "Options:"
      echo "  --tag TAG     Image tag (default: latest)"
      echo "  --push        Push to $REGISTRY after build"
      echo "  --dry-run     Show commands without executing"
      exit 0
      ;;
    *) ;;
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
# Build
# ---------------------------------------------------------------------------
echo "=== Building Docker image: ${IMAGE_NAME}:${IMAGE_TAG} ==="
if [ -n "$PLATFORM" ]; then
    run docker build \
      --platform "$PLATFORM" \
      -t "${IMAGE_NAME}:${IMAGE_TAG}" \
      -f "$SCRIPT_DIR/Dockerfile" \
      "$SCRIPT_DIR"
else
    run docker build \
      -t "${IMAGE_NAME}:${IMAGE_TAG}" \
      -f "$SCRIPT_DIR/Dockerfile" \
      "$SCRIPT_DIR"
fi

echo ""
echo "=== Build complete ==="
echo "Image: ${IMAGE_NAME}:${IMAGE_TAG}"

# ---------------------------------------------------------------------------
# Push (optional)
# ---------------------------------------------------------------------------
if $PUSH; then
  FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"
  echo ""
  echo "=== Pushing to registry ==="
  run docker tag "${IMAGE_NAME}:${IMAGE_TAG}" "$FULL_IMAGE"
  run docker push "$FULL_IMAGE"
  echo "=== Push complete: $FULL_IMAGE ==="
fi

# ---------------------------------------------------------------------------
# Run instructions
# ---------------------------------------------------------------------------
echo ""
echo "Run with:"
echo "  docker run --platform $PLATFORM -d \\"
echo "    --name opencode-web \\"
echo "    -p 4096:4096 \\"
echo "    -e DASHSCOPE_API_KEY=sk-xxx \\"
echo "    -e OPENCODE_SERVER_PASSWORD=your-password \\"
echo "    -e CLICKHOUSE_HOST=your-clickhouse-host \\"
echo "    -e CLICKHOUSE_PORT=8123 \\"
echo "    -e CLICKHOUSE_USER=default \\"
echo "    -e CLICKHOUSE_PASSWORD=your-password \\"
echo "    -e CLICKHOUSE_DATABASE=ashare \\"
echo "    -v /path/to/analysis:/workspace/analysis \\"
echo "    ${IMAGE_NAME}:${IMAGE_TAG}"
echo ""
echo "Or use docker-compose:"
echo "  cp .env.example .env  # edit with your credentials"
echo "  docker compose up -d"