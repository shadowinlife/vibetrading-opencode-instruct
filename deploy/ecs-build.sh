#!/usr/bin/env bash
set -euo pipefail

REGISTRY="registry.cn-hangzhou.aliyuncs.com/jiefengnewsv2"
VERSION="${1:-v2.0.0-mymain}"
VT_BRANCH="mymain"

echo "=== ECS AMD64 Build: opencode-serve:${VERSION} ==="

if [ ! -d "vibetrading-opencode-instruct" ]; then
    git clone https://github.com/shadowinlife/vibetrading-opencode-instruct.git
fi
if [ ! -d "Vibe-Trading" ]; then
    git clone -b "$VT_BRANCH" https://github.com/shadowinlife/Vibe-Trading.git
fi

cd Vibe-Trading && git fetch origin "$VT_BRANCH" && git checkout "$VT_BRANCH" && git pull origin "$VT_BRANCH"
cd ../vibetrading-opencode-instruct && git pull origin main

echo "=== Step 1: Build base image ==="
cd vibetrading-opencode-instruct
./build.sh --base --tag latest --push

echo "=== Step 2: Build app image ==="
VT_SOURCE="../Vibe-Trading" ./build.sh --app --tag "$VERSION" --push

echo "=== Done: ${REGISTRY}/opencode-serve:${VERSION} ==="