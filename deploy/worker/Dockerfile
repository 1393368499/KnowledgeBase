# Linux worker / beat 镜像
#
# 为什么必须 Linux：
#   Windows 上 Celery 官方不支持生产级并发（prefork 不可用，只能 solo=串行）。
#   此前所谓"并发验收"用的是 ASGI 多 worker，不是 Celery 并发，不能当生产证据。
#
# 构建上下文安全（部署审查要求）：
#   **不用 `COPY . .`**。顶层有 `.tmp-*.py` / `_probe_ocr.py` / `db/` / `media/` /
#   `vector_data/`(65 GiB) / `.env`，`COPY . .` 会把密钥与海量数据带进镜像层。
#   这里**逐个显式列出运行所需目录**，实测上下文仅 ~1.6 MB。
# 基础镜像按 digest 固定（与 deploy/app/Dockerfile 保持一致），避免同名 tag 被上游重推。
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 不装任何 apt 包：
#   1) python:3.13-slim **已自带** ca-certificates（HTTPS 访问 AS711 足够）；
#   2) worker 不需要 curl —— 健康检查用 Python/httpx（requirements 里已有）；
#   3) 实测在 Debian 镜像上 apt-get 安装会因上游 500/503 直接 exit 100，
#      平白让构建失败。少一层依赖 = 少一个不稳定源。

WORKDIR /app

# 依赖单独一层（改代码不必重装依赖）。
# requirements.txt 里 pywin32 已加 `sys_platform == "win32"` 标记，
# 否则 Linux 上没有该发行包会构建失败。
# 与 deploy/app/Dockerfile 保持同一套安装口径：
#   --no-deps 证明"清单本身完整"（缺依赖会立刻 import 失败），
#   pip check 兜底校验依赖关系，不靠 pip 现场解析传递依赖。
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --no-deps -r requirements.txt \
 && python -m pip check

# ---- 只复制运行时真正需要的代码 ----
COPY config/ ./config/
COPY apps/ ./apps/
COPY governance/ ./governance/
COPY manage.py ./

# 非 root 运行
RUN useradd --create-home --uid 10001 kb \
 && mkdir -p /var/lib/kb \
 && chown -R kb:kb /app /var/lib/kb
USER kb

# 队列隔离：worker 只消费**专用队列**，绝不碰业务 Redis 里的历史消息。
# KB_SKIP_DOTENV=1 关键：项目 .env 用 override=True 加载，容器里若不跳过，
# 会把地址覆盖回 127.0.0.1，导致容器内连不上 PG/Redis/Qdrant。
ENV KB_SKIP_DOTENV=1 \
    KB_CELERY_QUEUE=kb_worker_v2 \
    CELERY_BEAT_SCHEDULE_FILE=/var/lib/kb/celerybeat-schedule

# 容器级健康检查：用 Celery 自己的 ping，**不依赖 curl，也不依赖 Qdrant**。
#
# 为什么不去探 Qdrant：迁移期间 Qdrant 会被**有意停掉**（停写窗口），
# 那时 worker 被判 unhealthy 会造成误导。worker 的职责是"能接任务"，
# 用自身 ping 判定更准确。
HEALTHCHECK --interval=60s --timeout=20s --start-period=30s --retries=3 \
  CMD celery -A config inspect ping -d celery@$HOSTNAME --timeout=10 || exit 1

CMD ["sh", "-c", "celery -A config worker -Q ${KB_CELERY_QUEUE} --concurrency=${KB_CELERY_CONCURRENCY:-4} --loglevel=INFO"]
