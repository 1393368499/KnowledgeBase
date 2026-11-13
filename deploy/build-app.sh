#!/usr/bin/env bash
# Self-contained source build; no prebuilt kb-worker image is required.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
APP_TAG="${APP_TAG:-knowledgebase:app}"
docker build -f deploy/app/Dockerfile -t "$APP_TAG" .
echo "Built $APP_TAG. Next: docker compose up -d --wait"

# 记录发布制品摘要（规则第 5 节：关键发行件记录来源与摘要）。
# 记到 snapshots/builds/（本地证据目录，不入版本库），同时打印到控制台。
mkdir -p snapshots/builds
RECORD="snapshots/builds/$(date +%Y%m%d-%H%M%S)-build.txt"
IMAGE_ID="$(docker image inspect "$APP_TAG" --format '{{.Id}}')"
REPO_DIGEST="$(docker image inspect "$APP_TAG" --format '{{json .RepoDigests}}')"
GIT_HEAD="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
GIT_DIRTY="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
{
  echo "tag=$APP_TAG"
  echo "imageId=$IMAGE_ID"
  echo "repoDigests=$REPO_DIGEST"
  echo "gitHead=$GIT_HEAD"
  echo "gitDirtyFiles=$GIT_DIRTY"
  echo "baseImage=source-base python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928"
  echo "createdAt=$(date -Iseconds)"
} > "$RECORD"
echo "构建记录：$RECORD"
echo "镜像摘要：$IMAGE_ID"
