# Python 3.15 适配与项目根目录重构记录（2026-11-13）

依据人类 owner 2026-11-13 会话指令：「我更新了python，项目更新适配新的python，镜像也要适配。并且整理项目根目录，按照标准的Django框架。框架外的文件和数据也要放在项目根目录，但是要分类放好，不能平铺。」owner 选择 **方案 C（数据集中 + 项目包改名）**，voice-asr 采用「先尝试升 3.15，不行就保留 3.13 并登记」策略。

## 1. Python 运行时

| 项 | 变更 |
| --- | --- |
| 宿主新运行时 | Python **3.15.2**（`E:\Tools\Python\Python315`，pip 26.2.2，2026-11-13 安装）；`py -3.15` 可用 |
| 项目虚拟环境 | 旧 `.venv`（3.13.5）移至 `%TEMP%\kb-venv-py313-backup`；新建 `.venv` = 3.15.2 |
| 依赖验证 | `requirements.txt` 74 个固定版本在 3.15 上全部可安装：`pip install --no-deps -r requirements.txt` 成功，`pip check` 无冲突；Django 6.1.3 的 PyPI classifiers 明确包含 `Python :: 3.15` |
| 开发工具 | 新 `.venv` 安装 ruff 0.14.4、bandit 1.9.4、playwright 1.64.0（与旧 venv 同版本，用于对齐本地浏览器用例） |
| 代码兼容 | 无需改代码：`manage.py check`、`makemigrations --check` 通过；SQLite 全量 **1746 用例 / 0 失败 / 0 错误 / 51 跳过**（225s）；PostgreSQL 全量 **1746 / 0 / 0 / 28 跳过**（300s） |

版本引用更新：`pyproject.toml` 的 ruff `target-version` 改为 `py314`（ruff 0.14.4 尚无 `py315` 目标，注释说明升 ruff 后再改）；`requirements.txt`、`README.md` 的建环境命令改 `py -3.15`；CI 四个 job 的 `python-version` 改 `3.15`。

## 2. 镜像适配

- 统一基础镜像：`python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928`（Docker Hub 拉取所得；Debian 13 trixie）。
- 更新 5 个 Dockerfile：`deploy/app/Dockerfile`（source-base 阶段及上方注释）、`deploy/worker/Dockerfile`、`deploy/voice-asr/Dockerfile`、`deploy/voice-gateway/Dockerfile`、`deploy/voice-tunnel/Dockerfile`；`deploy/web/Dockerfile` 已废弃，保持只作历史追溯。
- `deploy/build-app.ps1` / `deploy/build-app.sh` 的 `baseImage` 记录文字同步为 3.15 digest；构建脚本的构建记录机制不变（写入 `snapshots/builds/`）。

### 2.1 应用镜像（完成）

- 构建命令：`powershell -File deploy/build-app.ps1 -Standard`（完全从源码构建）。
- 产物：`knowledgebase:app` = **`sha256:72d49a7b1164dd4773dd2c1cb05b395445d4ec13912d477d14e43ffa745cc7c1`**（2026-11-13 11:23 构建）；`docker run --rm --entrypoint python knowledgebase:app -V` → **Python 3.15.2**。
- 构建记录：`snapshots/builds/20261113-112302-build.json`（gitHead `e9f92f6`，dirtyFiles 161，baseImage 3.15 digest）。
- 部署后 `knowledgebase-web` 内 `python -V` = 3.15.2；就绪接口 200；`check --deploy` 0 警告。

### 2.2 Office / 语音镜像（完成，ASR 按预批准策略保留 3.13）

- **`knowledgebase:office`**：已重建，基础镜像 3.15；镜像 ID `sha256:22d5cdb4ba8e729cb950b764a42bc6073f176372a4306d1111352db9b2ce89a9`（2026-11-13 11:30）；容器内 `python -V` = 3.15.2，健康检查 healthy。
- **`knowledgebase:voice-gateway`**：已重建，ID `sha256:499eb1a96a7ac8753a94b3af3d9e090b7303df5bbbcb3229b48cf81144979a0b`，容器内 Python 3.15.2。
- **`knowledgebase:voice-tunnel`**：已重建，ID `sha256:71047f4f38ee28cd8c5faf64e763f3737fbf54d5b84df363d19be6c68f23fe11`，容器内 Python 3.15.2。
- **`knowledgebase:voice-asr` 保留 Python 3.13.16**：3.15 构建实测失败——`onnxruntime==1.31.1` 当前无 cp315 轮子（`ERROR: No matching distribution found for onnxruntime==1.31.1`）。按 owner 预先选择的回退策略，`deploy/voice-asr/Dockerfile` 基础镜像保持 `python:3.13-slim@sha256:70729b46…`；失败日志见 `artifacts/python315/voice-asr-315-build-failure.log`。**2026-11-13 12:24 复核**：onnxruntime 最新版本仍为 1.31.1 且无 cp315 manylinux 轮子，PyYAML 也无预编译 3.15 轮子，ASR 升 3.15 当前不可行；待上游发布 cp315 轮子后重试并复跑语音链路验收。
- 所有存活服务已在 2026-11-13 11:31 前完成重建/重启：12 个 knowledgebase 容器 running，office/asr 健康检查通过，语音网关管理路由 404、无 Bearer 设备请求 401 的边界不变。

## 3. 根目录重构（方案 C）

### 3.1 变更前后

| 变更前 | 变更后 |
| --- | --- |
| `config/`（项目包） | `knowledgebase/`（标准 startproject 风格项目包） |
| `db/`、`media/`、`vector_data/`、`staticfiles/` | `data/db/`、`data/media/`、`data/vector_data/`、`data/staticfiles/` |
| 根目录 `DESIGN.md`、`PRODUCT.md` | `docs/design/DESIGN.md`、`docs/design/PRODUCT.md` |
| 根目录 `knowledge-base-prototype.html` | `docs/prototype/knowledge-base-prototype.html` |
| 根目录 `.tmp-*.py`、`.tmp-verify-p2.log` | `scripts/archive/tmp/`（`.gitignore` 归档目录，不入库） |
| 根目录 `.pytest-final-*.txt`、`.pytest-summary.txt` | `docs/logs/legacy/`（保留版本控制） |
| 根目录 `.build-app.log` | `artifacts/logs/legacy/`（证据目录，不入库） |
| `__pycache__/`、`.pytest_cache/`、`.ruff_cache/`、`.qdrant-initialized` | 删除（生成物/旧标记） |

整理后的根目录：`manage.py`、`requirements.txt`、`pyproject.toml`、`pytest.ini`、`README.md`、`knowledgebase/`、`apps/`、`governance/`、`tests/`、`data/`、`docs/`、`deploy/`、`scripts/`、`artifacts/`、`snapshots/`、`secrets/`、`evals/`、`firmware/`、`android/`、`.github/`、`.impeccable/`、`.venv/`、`.env`、`.env.example`、`docker-compose.yml`、`.gitignore`、`.dockerignore`。

### 3.2 路径引用更新

- **项目包**：`config.settings/urls/wsgi/asgi/paths/exceptions/health_urls/test_settings/worker_test_settings/loadtest_settings/install_check_settings/celery` → `knowledgebase.*`；`from config import ...` 同步替换（共 128 个文件中出现引用，含代码、测试、脚本、CI、compose、Dockerfile）。
- **容器与脚手架**：`DJANGO_SETTINGS_MODULE`、`gunicorn knowledgebase.wsgi`、`celery -A knowledgebase`、Dockerfile `COPY knowledgebase/ ./knowledgebase/`、CI 的 ruff/bandit 扫描路径、`pytest.ini` 警告过滤器均更新。
- **数据路径**：`knowledgebase/paths.py` 默认目录改为 `data/{vector_data,db,media}`、`data/media/raw`；`knowledgebase/settings.py` 的 `STATIC_ROOT=data/staticfiles`、`VOICE_AUDIO_ROOT=data/media/voice-audio`；compose 挂载与容器内路径改为 `data/media/...` → `/app/data/media/...`、`RAW_STORE_DIR=/app/data/media/raw`；`deploy/preflight.sh`/`start.ps1` 的目录准备、`.gitignore`/`.dockerignore`、`pyproject.toml` 的 ruff 排除、README/部署/备份文档同步更新。
- **体量数据迁移**：`media/`（766 MB，含 raw/templates/fonts/voice-*）、`db/`、`vector_data/`（764 MB）整体移入 `data/`；Qdrant 仍使用既有命名卷，不受影响。测试数据清理策略按 owner 2026-11-13 判定（测试数据可不备份）。

### 3.3 原例外已闭环（2026-11-13 12:20）

根目录 `vector_data/wsl-runtime/ext4.vhdx`（764 MB）原先被 WSL/Docker 进程占用而无法移动；2026-11-13 用户退出 Docker Desktop 后，先 `wsl --shutdown` 释放 vmmemWSL 句柄，再将 `ext4.vhdx` 移入 `data/vector_data/wsl-runtime/`，根目录 `vector_data/` 已删除。至此根目录不再有平铺的散落数据目录，`.gitignore` 中的根 `vector_data/` 例外也已移除。

> 环境备注：Docker Desktop 启动时曾因 `%LOCALAPPDATA%\Docker\run` 目录缺失/残留而反复弹出 Ingest server 初始化错误；重建 `run` 目录并单实例启动后恢复，与项目代码无关。

> WSL 注册补充（2026-11-13 16:05）：移动虚拟盘后 WSL 注册表 BasePath 仍指向旧目录，`test_storage_migrate_safety` 的 WSL 用例因此失败。已用 `wsl --unregister KnowledgeBase-Runtime` + `wsl --import-in-place KnowledgeBase-Runtime <新路径>/ext4.vhdx` 重新注册，发行版可从新路径启动，该用例恢复通过。

## 4. 验证

| 检查 | 结果 |
| --- | --- |
| 宿主 `.venv` | Python 3.15.2；`manage.py check` 0 问题；`makemigrations --check` 无变化 |
| 静态检查 | `ruff check apps knowledgebase governance tests scripts`：All checks passed |
| SQLite 全量 | 1746 用例，0 失败/0 错误，51 跳过（225s，`artifacts/python315/pytest-sqlite-refactor.xml`） |
| PostgreSQL 全量 | 1746 用例，0 失败/0 错误，28 跳过（300s，`artifacts/python315/pytest-postgres-refactor.xml`） |
| 容器部署 | `docker compose up -d --wait` 全部 Healthy，`migrate` Exit 0（触发器 6 个、runtime role ready）；HTTPS 就绪 200；`check --deploy` 0 警告 |
| 运行时 | web 容器 Python 3.15.2；容器内 `/app/data/{db,media,staticfiles,vector_data}` 就位，`data/media/raw`、`data/media/templates` 挂载成功 |
| 数据库 | PostgreSQL 17.12；`kb_runtime` 非超级；57 条迁移；6 个触发器 |
| 包引用 | 代码/CI/compose 中无残留 `config.*` 模块引用（归档脚本与历史文档除外） |

## 5. 风险与回滚

- **回滚**：恢复旧 `.venv`（`%TEMP%\kb-venv-py313-backup`）；镜像可回退到旧标签（`knowledgebase:app` 上一产物 `9ceccc…` 仍在本机）；目录重构按本记录反向移动即可（数据均为移动而非复制，路径可逆）。
- **Django 3.15 支持**：Django 6.1.3 官方 classifiers 已含 3.15，且全量回归与部署验证通过；无兼容性阻塞。
- **ruff 目标版本**：0.14.4 不支持 `py315`，暂用 `py314`（不影响实际运行解释器）；升级 ruff 后改为 `py315`。
- **历史文档**：`docs/` 中 2026-11-13 之前的历史记录仍引用旧路径/旧行号（作为历史证据保留；当前操作文档已更新）。审计报告中的行号同样对应重构前快照。

## 6. 证据

- 测试与检查：`artifacts/python315/`（`pytest-sqlite-*.xml`、`pytest-postgres-*.xml`、日志）。
- 构建记录：`snapshots/builds/20261113-112302-build.json`。
- 部署核验：容器内 `/app/data` 列表、`manage.py check --deploy`、就绪接口、DB 版本查询（见上表；原始输出可按命令复跑）。
- 关联记录：`docs/remediation-2026-11-13.md`（同日 PostgreSQL 17.12 升级）、`docs/fullstack-compliance-audit-2026-11-13.md`（重构前审计快照）。
