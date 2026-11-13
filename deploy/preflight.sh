#!/usr/bin/env bash
# 全新机器上跑 `docker compose up -d` 之前的一次性准备（幂等，可反复执行）
#
# 覆盖部署审查里"新机器一 up 就中止"的几处硬前提：
#   * external 命名卷不存在 → `up` 直接报 external volume "knowledgebase-pgdata" not found；
#   * 宿主绑定目录不存在/属主不对 → 上传写不进去、beat 建不了 schedule；
#   * .env 未准备 → 口令与 ${} 插值全部退回默认值（postgres 与应用口令不一致）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "[1/4] 检查 .env"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "[!] 已从 .env.example 生成 .env —— 请先改这几项再重新运行本脚本："
  echo "      KB_PG_PASSWORD / DJANGO_SECRET_KEY / KB_ALLOWED_HOSTS"
  echo "    （容器里 DB_ENGINE 与 DJANGO_DEBUG 由 compose 钉死，改 .env 的这两项不生效）"
  exit 1
fi
echo "      .env 已存在"
if [ ! -f secrets/postgres-runtime.txt ]; then
  mkdir -p -m 0700 secrets
  (umask 077; set -o noclobber; openssl rand -base64 48 > secrets/postgres-runtime.txt)
  chmod 0444 secrets/postgres-runtime.txt
fi

echo "[2/4] 检查命名卷"
for volume in knowledgebase-pgdata knowledgebase-redisdata; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "      $volume 已存在"
  else
    docker volume create "$volume" >/dev/null
    echo "      $volume 已创建（空卷）"
  fi
done
qdrant_volume="${KB_QDRANT_VOLUME:-knowledgebase-qdrantdata-delivery-20261015}"
if docker volume inspect "$qdrant_volume" >/dev/null 2>&1; then
  echo "      $qdrant_volume 已存在"
else
  docker volume create "$qdrant_volume" >/dev/null
  echo "      $qdrant_volume 已创建（空卷：向量要由 kbrebuild 从原件重建）"
fi

echo "[3/4] 检查宿主绑定目录与属主（容器内运行用户 uid/gid 10001）"
uid_gid="${KB_UID_GID:-10001}"
for dir in data/media/raw data/media/templates data/media/fonts deploy/worker/beat-state; do
  mkdir -p "$dir"
  if [ "$(id -u)" = "0" ]; then
    chown -R "$uid_gid:$uid_gid" "$dir"
  elif command -v sudo >/dev/null 2>&1; then
    sudo chown -R "$uid_gid:$uid_gid" "$dir"
  else
    echo "[!] 非 root 且没有 sudo：请手动执行 chown -R $uid_gid:$uid_gid $dir"
    exit 1
  fi
  echo "      $dir -> $uid_gid:$uid_gid"
done

echo "[4/4] 检查 compose 版本（根 compose 的 include: 需要 >= 2.20）"
docker compose version

cat <<'NEXT'
[下一步]
  1) bash deploy/build-app.sh        # 新机器必须先建镜像（已有 knowledgebase:app 的平台可跳过）
  2) docker compose up -d            # 从**仓库根**执行：这样根 .env 才会参与 ${} 插值
  3) docker compose exec web python manage.py kbcreateuser    # 管理员账号不会被自动创建
  4) docker compose cp https:/data/caddy/pki/authorities/local/root.crt ./secrets/kb-local-ca.crt
  5) curl --cacert ./secrets/kb-local-ca.crt https://localhost:8800/api/v1/health/ready/
NEXT
