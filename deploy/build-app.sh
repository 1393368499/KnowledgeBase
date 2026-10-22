#!/usr/bin/env bash
# 交付镜像的 Linux/macOS 构建入口（等价于 deploy/build-app.ps1 的**标准路径**）
#
# 用法：
#   bash deploy/build-app.sh
#   BASE_TAG=kb-worker:custom APP_TAG=kb-app:delivery bash deploy/build-app.sh
#
# 为什么必须有这个脚本（部署审查 BLOCKER B1）：
#   `deploy/app/Dockerfile` 的 FROM 是**基座镜像**（默认 kb-worker:phase2-review-20261015，
#   那是本机历史交付基座，全仓库没有任何脚本能从零产出它）。基座只能由
#   `deploy/worker/Dockerfile`（python:3.13-slim）建出来。任何**新机器**都必须先建基座，
#   否则 `docker compose up -d --build` 会去 Docker Hub 解析 `kb-worker` 而失败。
#
# 跑完本脚本后：
#   docker compose up -d                  # 镜像已就位，无需 --build
#   export KB_BASE_IMAGE=$BASE_TAG        # 若还要让 compose 自己 build，用它对齐基座 tag
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BASE_TAG="${BASE_TAG:-kb-worker:local-build}"
APP_TAG="${APP_TAG:-kb-app:delivery}"

echo "[1/2] 构建基座 $BASE_TAG（deploy/worker/Dockerfile，python:3.13-slim）"
docker build -f deploy/worker/Dockerfile -t "$BASE_TAG" .

echo "[2/2] 构建交付镜像 $APP_TAG（--build-arg BASE_IMAGE=$BASE_TAG）"
docker build -f deploy/app/Dockerfile --build-arg "BASE_IMAGE=$BASE_TAG" -t "$APP_TAG" .

echo "[完成] 产物 = $APP_TAG（基座 $BASE_TAG）"
echo "[下一步] docker compose up -d"
echo "         若还要用 compose 的 build（而不是上面的已建镜像）：export KB_BASE_IMAGE=$BASE_TAG"
