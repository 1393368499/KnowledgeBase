# 2026-11-11 复审整改记录

依据：[全栈规则复审报告](fullstack-rules-check-2026-11-11.md)、四条分线证据（`artifacts/review/fullstack-rules-20261111/`）。
本轮目标：删掉 LibreOffice（用户明确要求，宿主会拉起 soffice 弹窗）、修复复审可修项、升级 Django、重建并重部署、两轮回归全绿。

## 一、代码与配置修复

| 复审项 | 处置 | 证据/位置 |
| --- | --- | --- |
| LibreOffice 宿主依赖（用户要求） | **已移除**。`apps/knowledge/office_convert.py` 1234→186 行，只剩输入守卫；`parsers.py` 未配置 office 服务时明确 `ParseUnsupported`；`document_export.py` PDF 只走 office_client，新增稳定错误码 | `fixes/01-office-removal.md`；`docker exec knowledgebase-web sh -c "command -v soffice"` → ABSENT；测试全程 `Get-Process soffice` = 0 |
| Django 6.1.2 落后安全补丁 | **已升级 6.1.3**（含上传相关 CVE-2026-108147，官方定级 low） | `requirements.txt:12`；容器内 `django.get_version()` = 6.1.3 |
| 上传内存上限未设 | 新增 `DATA_UPLOAD_MAX_MEMORY_SIZE=8MiB`、`FILE_UPLOAD_MAX_MEMORY_SIZE=4MiB`、`DATA_UPLOAD_MAX_NUMBER_FILES=10`（env 可覆盖） | `config/settings.py` 认证/上传段 |
| psycopg 补丁落后 | 3.3.6 → 3.3.8（-binary 同步） | `requirements.txt`；容器内 `psycopg.__version__` = 3.3.8 |
| 38 项常红测试 | **已修复到现行协议**（auto_execute 直接断言结果/审计/幂等，不重复确认；2 项 compose_document 协议变化按新语义更新） | `fixes/02-legacy-tests.md`；两轮全量均 0 失败（见下） |
| 无静态检查 | 新增 `pyproject.toml`（ruff E4/E7/E9/F）并清零；`ruff check apps config governance tests scripts` = All checks passed | 本轮日志 |
| 无 CI | 新增 `.github/workflows/ci.yml`：ruff + pip-audit + gitleaks + `manage.py check`/迁移检查 + SQLite 全量 + `check --deploy` | 工作流文件 |
| 基础镜像仅 tag | `deploy/app`、`worker`、`voice-asr/gateway/tunnel` 全部钉 `python:3.13-slim@sha256:70729b46…`；compose 的 postgres/redis 钉 digest；字体/office 运行库 apt 版本钉死；apt/zlib 安全更新不钉（注释说明取舍） | 各 Dockerfile / `deploy/docker-compose.unified.yml` |
| 构建不记录 digest | `deploy/build-app.ps1` / `.sh` 产物 ID/digest/Git HEAD 写入 `snapshots/builds/` | 本轮 `snapshots/builds/20261111-125138-build.json` |
| worker 路径无 `--no-deps`/`pip check` | 已补，与 app 路径同口径 | `deploy/worker/Dockerfile` |
| `.dockerignore` 漏 `snapshots/` 等 | 补 `snapshots/`、`*.dump`、`*.snapshot`、`deploy/worker/beat-state/`、`firmware/`、`android/`、`evals/`、`.impeccable/` | `.dockerignore` |
| 云隧道二进制构建期不校验 | cloudflared 在 `voice-tunnel/Dockerfile` 构建期 `sha256sum -c`（哈希来自 manifest） | `deploy/voice-tunnel/Dockerfile` |
| 前端弹窗无对话框语义/焦点逃逸 | `KB.modal` 加 `role=dialog`/`aria-modal`/`aria-labelledby`，背景引用计数式 `inert`，Tab/Shift+Tab 首尾环绕 | `apps/webui/static/webui/kb-common.js` |
| 行复选框无可访问名 | `aria-label="选择《…》"` | `kb-files.js` |
| 搜索命中/空态不播报 | `role=status` + `aria-live=polite` + `aria-atomic` | `search.html` |
| 1.4.11 控件边框 1.43:1 | 新增 `--color-border-strong #8fa8a0`（对白 3:1）用于按钮/输入框/下拉；DESIGN.md 同步 | `kb-workspace.css`、`DESIGN.md` |
| 标题层级倒置 | 侧栏分组改非标题、登录介绍改 `<p>`，每页唯一 h1 | `_sidebar.html`、`login.html` |
| `.nature-select` 23px | `min-height:24px` | `kb-home.css` |
| 死文件 `kb-proto.css` 29KB | 已删除；相关测试守卫改成正向守卫（文件不存在 + 无模板/JS 引用） | 5 个测试文件 + `test_doc_write_intent…` |
| 错误响应回传内部细节 | 预览/上传/LLM 不可用/语音失败改为稳定 code + 可读提示，异常原文进日志 | `apps/knowledge/views.py`、`apps/planner/views.py`、`apps/voice/services.py`；`tests/test_error_sanitization.py` |
| 重试退避无抖动 | full jitter（注入随机源便于测试），上限仍指数增长 | `apps/knowledge/resilience.py`；`tests/test_resilience.py`、新抖动断言 |
| 停用库仍可读（O-01） | 文件列表/详情/下载/进度按 `library__is_active` 收敛；测试覆盖停用隐藏、详情 404、重新启用恢复 | `apps/knowledge/views.py`；`tests/test_inactive_library_access.py` |
| 登录无限速（F-10 一部分） | 新增 `LoginThrottleMiddleware`（`/app/login/`、`/admin/login/`，IP+用户名，cache 计数），阈值/锁定 env 可调；成功清零 | `governance/middleware.py`、`config/settings.py`；`tests/test_login_throttle.py` |
| 密码校验器不全 | 补 CommonPassword/Numeric，最小长度 10 | `config/settings.py` |
| 设备协议版本只声明不校验（F-07） | 上传显式拒绝非 1 版本（426 + 支持列表），不带 protocol 保持向后兼容 | `apps/voice/views.py`；`tests/test_voice_protocol_version.py` |
| Qdrant 版本漂移不可见 | 首次连接探测服务端版本并记录（主次版本不一致时 warning），不改变 `check_compatibility=False` 的取舍 | `apps/knowledge/vectorindex.py` |
| 交付文档事实错误 | README 改正 LibreOffice 描述、说明 office 容器承担转换、说明构建记录；`.env.example` 补齐新增开关 | `README.md`、`.env.example` |
| 视觉证据未入库 | `.impeccable/` 设计资产与 `PRODUCT.md`/`DESIGN.md` 入库；代表性截图复制到 `docs/screenshots/p3-*.png` | 本轮提交 |
| 锁定件未入库 | `deploy/office/`（含 `jars.sha256`）、`deploy/qdrant/`、`deploy/fnos/` 入库 | 本轮提交 |
| 候选无提交边界（P0-1） | 按逻辑分组提交（见提交记录），并把本轮镜像 ID 与构建记录对应 | 本轮提交 |
| RPO/RTO 未定义、恢复耗时缺 | 新增 `docs/backup-restore-policy.md`；本轮完成 PG 恢复演练（0.84 秒、完整性计数核对一致） | `logs/restore-rehearsal.txt` |
| 延期项无正式登记（P0-2/P0-4 等） | 新增 `docs/deferral-register-2026-11-11.md`，逐项记录影响/替代控制/责任人/期限/批准人 | 该文件 |

## 二、本轮验证结果

- **Django 检查**：`manage.py check` 0 issues；`makemigrations --check --dry-run` No changes detected。
- **静态检查**：`ruff check apps config governance tests scripts` → All checks passed。
- **回归**：SQLite 全量、PostgreSQL 全量见下方"验证日志"；两轮 0 失败（原 38 项常红已清零）。
- **构建**：`knowledgebase:app` = `sha256:761aabb9c789a4d5bab89e9580a61c83f825a0d4ca27e0db76ea65b90d9a6c21`（Django 6.1.3 / psycopg 3.3.8 / 无 soffice），构建记录 `snapshots/builds/20261111-125138-build.json`。
- **重部署冒烟**（`logs/smoke-after-redeploy.txt`）：live/ready 200（migrations ok）、登录页 200、受保护页 302、未认证 API 401、无 CSRF POST 403、404 无堆栈；容器内 `LoginThrottleMiddleware` 已加载、`KB_OFFICE_SOCKET` 已配置。
- **Office 转换端到端**：容器内用合成 DOCX 走隔离服务转换，返回真实 `%PDF-`（9,896 字节），证明删除 LibreOffice 后能力未丢。
- **恢复演练**：PG 备份（854,832 字节，SHA-256 与 11-10 记录一致）恢复到空库 0.84 秒，迁移 54/信息库 4/资料 50/分段 1272/操作 167/审计 1572/触发器 6，与在线库一致（审计差 1 为备份后新增）。

## 三、未修复项

全部转为**已登记延期**（`docs/deferral-register-2026-11-11.md`）：管理入口 MFA、HTTPS 强制、voice-asr/qdrant 非 root、解析沙箱、OpenAPI、pypdf/celery 大版本、真实屏读器、真实 CWV、滚动发布共存、字体商用授权、Redis/psycopg 许可评估、向量冷备 SHA 清单、页面层字面色值收敛、Python 依赖 hash 锁定、宽泛异常逐项审查。

## 四、验证日志索引

- 测试：`logs/pytest-sqlite-after-fixes.txt`、`logs/pytest-postgres-after-fixes.txt`
- 冒烟：`logs/smoke-after-redeploy.txt`
- 构建：`logs/image-build-20261111.txt`、`snapshots/builds/20261111-125138-build.json`
- 恢复演练：`logs/restore-rehearsal.txt`
- 分线报告：`fixes/01-office-removal.md`、`fixes/02-legacy-tests.md`、`fixes/03-frontend-a11y.md`、`fixes/04-backend-security.md`
