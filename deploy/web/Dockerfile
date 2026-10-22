# ⚠️ 已废弃（DEPRECATED）—— 交付镜像请用 `deploy/app/Dockerfile`
#
# 本文件是早期"web 叠在 kb-worker:local 之上"的增量构建路径，已被
# `deploy/app/Dockerfile`（显式 `ARG BASE_IMAGE` + 两步构建 + 本地 image ID 校验，
# 见 deploy/build-app.ps1 / deploy/build-app.sh）取代。保留只为历史可追溯。
# 它的问题：`FROM kb-worker:local` 与 `deploy/docker-compose.worker.yml` 的
# `image: kb-worker:local` 抢同一个 tag，且没有基座来源，新机器无法复现。
#
# Web 服务镜像（在**当前源码构建出的** worker 镜像之上增量构建）
#
# 关键前提（部署审查指出的阻断）：
#   web 必须建立在**由当前源码构建**的 `kb-worker:local` 之上。
#   之前的问题是我把历史镜像 `kb-worker:phase2-review-20261015` 直接重打成
#   `kb-worker:local`，那份镜像里的 config/apps/governance 是**旧源码** ——
#   于是本机对 settings.py 的新增（whitenoise 中间件）以及后续任何代码改动
#   **都不会进入容器**。正确流程是：每次部署先重建 worker 镜像（COPY 当前源码），
#   web 再以它为基底。
#
# 为什么这里**不再 pip install whitenoise**：
#   whitenoise 已固定进 `requirements.txt`，由 worker 镜像的
#   `pip install -r requirements.txt` 统一安装。在 web 层再单独 pip install
#   会造成"镜像里的依赖与 requirements 不一致"的漂移 —— 这正是要避免的。
FROM kb-worker:local

USER root

# 静态文件收集目录（STATIC_ROOT=<项目根>/staticfiles）：
# 构建期先建好并交给运行用户，避免容器内 collectstatic 因权限失败。
RUN mkdir -p /app/staticfiles && chown -R kb:kb /app/staticfiles

USER kb

WORKDIR /app
