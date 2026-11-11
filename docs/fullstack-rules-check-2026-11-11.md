# 智库 · 全栈开发规则对照检查

日期 2026-11-11（Asia/Shanghai）。检查对象：`D:\AProjectScript\KnowledgeBase`，Git HEAD `e77cbd9`（2026-10-27）+ 工作区未提交候选（审查开始时 `git status` 138 项，其中 77 个修改文件 +2470/−1160，147 个未跟踪新文件；本轮新增的报告与证据目录使其变为 139 项）。规则基线：`D:\WorkSpace\_study\全栈开发规则.md`（2026-11-09 共享定稿）。运行环境：本机 11 个容器在运行（web/worker/scheduler/postgres/redis/qdrant/office/voice-*），`knowledgebase:app` = `sha256:f4412ae9…4044`。

方法：按规则章节拆 4 条独立审查线并行取证（选型许可、前端、后端与安全、测试与交付），每条线只读、独立写报告；Lead 复核关键事实后汇总。全部分线报告与日志在 `artifacts/review/fullstack-rules-20261111/`。全程未修改源码、配置与业务数据。

## 一、总体结论

**判级：不满足放行条件（阻断）。** 工程实现与既有验收证据整体扎实——历史高危项均已修复并有代码级核对、镜像/备份/触发器与文档逐字一致、两轮全量回归无新增失败——但**过程与制品链不闭环**：候选没有提交边界、没有 CI、发布制品没有可校验的摘要绑定、全量回归长期 38 项常红且无正式延期记录、管理入口无多因素认证、字体商用授权未核实。按规则第 0 节"必需检查失败/强制安全控制未验证/必要许可未确认即进入阻断状态"，当前候选不能直接发布。

| 规则章节 | 判级 | 一句话依据 |
| --- | --- | --- |
| §0 适用范围与放行原则 | **不符合** | 影响面与验证条件有记录，但候选版本未固定；必需检查常红、控制项缺失均无正式延期登记 |
| §1 技术选型与开源许可 | 部分符合 | 72 条依赖全固定、Office/Qdrant/ASR 锁定件扎实；缺 hash 锁定、基础镜像摘要、字体/Redis 许可记录，Django 落后一个安全补丁 |
| §2 编码规范与可维护性 | 部分符合 | 注释与模块划分好、无裸 except、测试验证真实契约；但无 lint/format/type 配置、候选混成一团、含死代码 |
| §3 前端设计与实现 | 部分符合 | Impeccable 全链真实使用、四档宽度零溢出零 JS 错；WCAG 2.2 AA 未达成（3 项 P1+2 项未验证），视觉证据未入库 |
| §4 后端接口、数据与可靠性 | 部分符合 | 信任边界抽查 24 个入口齐全、DB 约束/事务/触发器扎实；缺机器可读契约、错误细节外泄、重试无抖动、滚动共存未验证 |
| §5 安全审计与差异复审 | 部分符合 | 历史高危已修复并核对、CSRF/上传/沙箱/日志基本到位；无 CI 扫描、管理入口无 MFA、基座未钉摘要 |
| §6 交付验收 | 部分符合 | 两轮全量实测、冒烟、镜像/快照/触发器证据齐全；38 项常红无豁免、恢复耗时/RPO-RTO 缺、交付文档有事实错误 |

分线报告：`01-selection-licenses.md`（§1）、`03-frontend.md`（§3）、`04-backend-security.md`（§4/§5）、`05-delivery.md`（§2/§6）。

## 二、放行阻断项（P0）

| # | 阻断项 | 规则依据 | 关键证据 | 放行条件 |
| --- | --- | --- | --- | --- |
| P0-1 | 候选无版本边界，发布制品不可追溯 | §2 "改动容易审查和回退"、§5 "上线前核对复审证据、待发布版本与实际制品一致" | 138 项未提交变更（`git status`），77 files +2470/−1160；11-09/11-10 镜像由未提交工作区构建；无 commit/tag ↔ digest 映射；回退只能按镜像 tag | 把候选整理为可审查提交（或打候选 tag），记录 `docker image inspect` digest 与提交的对应关系 |
| P0-2 | 全量回归长期 38 项失败，无正式豁免 | §0 "必需检查失败→阻断"、"延期须记录影响/替代控制/责任人/期限/批准人" | SQLite 1673/65/38、PG 1696/42/38，两库失败集完全一致，与 11-09 基线逐条一致（新增 0）；33/38 是测试仍按旧"先确认再执行"协议、3 项操作规格已改自动执行、2 项文档协议变化；两轮均 exit 1 | 修复或隔离这批旧协议用例（更新/xfail/迁 legacy 套件），并按 §0 填写延期记录（责任人、期限、批准人、替代控制） |
| P0-3 | 无 CI，无源码/依赖/密钥扫描与静态检查门禁 | §5 "持续集成采用源码、依赖和密钥扫描"、§6 "用持续集成固化" | 无 `.github/`、`.gitlab-ci.yml`、`Jenkinsfile`、pre-commit、ruff/mypy 配置；依赖扫描只有发布时人工 Docker Scout/OSV；ruff 默认规则即报 15 项（10 F401、5 E402） | 建最小 CI（pytest + `manage.py check --deploy` + pip-audit/OSV + bandit/ruff S + gitleaks），或先补一次可复现扫描证据并归档 |
| P0-4 | 管理入口 `/admin/` 无多因素认证，也无延期记录 | §5 "管理入口采用多因素认证" | `config/urls.py:11`；`requirements.txt` 无 `django-otp`/`django-two-factor-auth`/`django-axes`；`is_staff` 账号等同系统管理员（`apps/knowledge/permissions.py:25-33`）；无登录失败限速 | 实现 TOTP/WebAuthn（至少失败限速+独立强口令），或按 §0 记录延期（缓解=仅回环 `127.0.0.1:8800` + 强口令 + 无自助注册；需责任人/期限/批准人） |
| P0-5 | 制品链缺摘要绑定 | §1 "固定可验证的版本或摘要"、§5 "关键发行件记录来源与摘要" | `deploy/app/Dockerfile:7` `FROM python:3.13-slim` 仅 tag；`deploy/build-app.sh` 只 `docker build -t`，不记录 digest；compose 中 postgres 仅 tag、redis 为浮动 `7.4-alpine` | 基础镜像钉 digest；构建脚本输出并归档产物 digest；发布记录附 digest |
| P0-6 | 字体商用授权未核实 | §1 "许可证未明或商用权利未核实时先澄清" | `media/fonts/SOURCES.md:3-8` 只记来源与哈希：方正小标宋来自高校下载页，仿宋/黑体/楷体/宋体为 Windows 字体副本，无授权范围 | 对外交付/商用前取得授权或替换为 Noto CJK（镜像内已有）；内部自用可按 §0 延期登记 |
| P0-7 | 锁定件与设计证据未入库 | §1 "保证构建可复现"、§3 "关键新页面和大改版在交付中附代表性截图或可访问的预览" | `deploy/office/`（`jars.sha256`、`pom.xml`）、`deploy/qdrant/Dockerfile`、`deploy/fnos/`、`PRODUCT.md`、`DESIGN.md`、`.impeccable/` 全部 untracked；`.impeccable/review/.gitignore` 内容为 `*`，34 张评审截图被排除 | 上述文件入库；截图/检测结果按交付证据入库或归档到版本库可引用的位置 |

## 三、逐节对照

### §0 适用范围与放行原则 — 不符合

- 已做到：受影响用户角色与主任务有记录（`PRODUCT.md`、`docs/function-operation-matrix.md`），关键数据与兼容承诺在 11-09/11-10 两份放行文档中有范围描述；阻断项与缺证据被明确写出（11-09 记录"候选未获放行"）。
- 未做到：候选的版本边界与"待发布版本"不一致（P0-1）；延期事项只有影响描述，未见责任人/截止日期/批准人（38 项失败、`/admin/` MFA、HTTPS 强制性三项均属此类）；本轮新增的阻断项（无 CI、字体授权）此前未进入任何放行记录。
- 结论：放行原则未满足，阻断状态成立。

### §1 技术选型与开源许可 — 部分符合

- 正向：`requirements.txt` 72 条全部 `==` 固定、无第三方 index、`pip check` 通过；72 个包 OSV 查询 0 命中；Office 72/72 JAR 与 `deploy/office/jars.sha256` 全等且 Maven 发行包 SHA-512 校验；ASR 运行镜像 31/31 与 `requirements.lock` 精确一致；三镜像 ID 与文档逐字一致；无 `curl|sh`、无 `ADD http`。11-09 记录的 libxml2/Raptor 高危触达面已随 LibreOffice 移出应用镜像而消除（`soffice` 在两镜像中均不存在）。
- 缺口：Django 锁 6.1.2，上游 6.1.3（2026-11-03）修复 6 个安全项；其中上传相关 `CVE-2026-108147` 经官方发布说明核对为 **low**（中危两项为 GIS 类，本项目未启用 `django.contrib.gis`，不适用），但 `settings.py` 未设 `DATA_UPLOAD_MAX_MEMORY_SIZE`/`FILE_UPLOAD_MAX_MEMORY_SIZE`，业务层 200 MiB 上限不构成对该 CVE 的前置缓解；两份放行文档均未登记。其他：Python 依赖仅版本可复现、无 hash/lock；`deploy/worker/Dockerfile:29` 无 `--no-deps`/`pip check`（11-09"两条路径均 pip check"只对 app 路径成立）；`deploy/qdrant/Dockerfile` 用 `apt-get upgrade -y` 无版本约束且以 root 运行；`voice-asr` 以 root 运行；cloudflared 构建期不校验哈希；Redis 7.4 为 RSALv2/SSPLv1（非 OSI）、psycopg LGPL-3.0、Redis/Whisper 权重与 Django 支持周期均无记录；本机 `.venv` 与锁定清单漂移（Pillow 12.3.0 vs 13.0.0、psutil 未装）。

### §2 编码规范与可维护性 — 部分符合

- 正向：注释解释业务约束与历史坑（如 `requirements.txt` 平台标记、`kb-common.js` 焦点归还），模块职责清楚；无裸 `except:`、无 `except: pass`；测试使用真实 PostgreSQL/真实触发器，`tests/conftest.py:196-219` 在请求 PG 却连到 SQLite 时直接报错，测试基座不静默降级；`skip` 均带 reason、无 `xfail` 掩盖。
- 缺口：无格式/静态/类型检查配置（无 `pyproject.toml`/`ruff.toml`/`mypy.ini`/pre-commit），ruff 默认规则 15 项；138 项变更把 Web 重构、后端检索、语音、固件、Android、评测混在同一候选，无提交/回退单位，违反"大范围重构与行为变更分开提交"；`kb-proto.css`（29 KB，无任何页面引用）为死代码；`apps/ config/ governance/` 约 108 处 `except Exception`（91 处带 `noqa: BLE001`）仅抽查、未逐项审查；文档/注释与实现漂移 3 处（见下）。

### §3 前端设计与实现 — 部分符合（有条件通过，无 P0）

- 正向：Impeccable 流程被真实使用——`PRODUCT.md` / `DESIGN.md`（212 行设计系统）/ `.impeccable/design.json` / surface brief / 34 张评审截图 / detector 输出 192 条；真实 Chromium 复验 11 页 × 1440/720/390/320 四档：**0 横向溢出、0 console/pageerror**；文字对比度最低 5.52 全部通过；跳转链接、抽屉、文档栏、tablist（方向键+Home/End）、Esc 焦点归还、`prefers-reduced-motion`、200% 缩放与文字间距均实测通过；首屏层级合理（提问框、上传/统计、搜索框均在首屏）；无渐变/阴影/卡片套卡片，状态用文字+颜色双通道。
- 缺口（3 项 P1）：① 自定义弹窗无对话框语义且焦点不设容器——`kb-common.js:908-949` 生成的 `.mask>.modal` 无 `role=dialog`/`aria-modal`/`aria-labelledby`，实测正向 Tab 第 7 次逃出遮罩、`Shift+Tab` 第 1 次回背景链接，影响 15 处 `KB.modal` 调用；② 表格行复选框无可访问名（`kb-files.js:624`，AX 树空名）；③ 搜索页命中数与空态不播报（`search.html:41/47/48` 无 `aria-live`，违反 4.1.3）。另：1.4.11 输入框边框对比 1.43:1（需设计决策，非代码级）；标题层级 2.4.6（侧栏 `<h2>` 先于各页 `<h1>`）；`.nature-select` 高 23px（2.5.8 差 1px）；字阶漂移（13px/11.5px 不在 DESIGN.md 刻度内，detector 81 条同结论）；`kb-home.css` 52 处字面色值绕过令牌；detector 192 条未分诊（113 条落在未引用的 `kb-proto.css`）；视觉证据与设计文档未入库；屏幕阅读器抽测与像素观感复核未做（已标注）。

### §4 后端接口、数据与可靠性 — 部分符合

- 正向：抽查 24 个受保护入口，权限+资源归属+库/设备范围齐全；历史三处越权（审计越权、跨设备删任务、RAG 撤权后引用）已核对为已修复并有回归；PostgreSQL 实测 8 个唯一约束、6 个不可篡改触发器、54 条迁移，关键写路径使用事务/`select_for_update`/CAS，触发器重建在 `transaction.atomic` 内；外部调用具备超时、重试上限、熔断、跨进程 Redis 闸门与总时间预算；DRF 错误体无堆栈/SQL。
- 缺口：无 OpenAPI/机器可读契约与版本检查（设备协议只声明 `protocol_version:1` 不校验；Office 线协议无版本字段；`vectorindex.py:132-141` 关闭 Qdrant 兼容检查）；错误响应回传内部细节（`apps/knowledge/views.py:1519` 渲染异常原文、`:448-454` 上传错误原文、`apps/planner/views.py:50-59` 上游 `response.text[:300]`）；重试退避无随机抖动（`apps/knowledge/resilience.py:746-761`），语音 ASR 无重试/熔断；滚动发布新旧版本共存未验证；停用信息库（`is_active=False`）语义未文档化（列表/详情仍可读）。

### §5 安全审计与差异复审 — 部分符合

- 正向：权限默认拒绝，CSRF 全站启用（登录/登出均 POST+Token），Cookie `HttpOnly`/`SameSite=Lax`，会话 401 语义统一；`.env`/`secrets/` 未入库、无硬编码密钥（`sk-`/`ghp_`/`AKIA` 0 命中）、日志无凭据（grep 0 命中）、审计 append-only；上传 200 MB 上限+扩展名白名单+内容寻址只读存储+临时文件 `os.replace`；Office 转换强沙箱（无网络、只读根、`cap_drop:ALL`、pids/mem 限额、DTD/外部实体拒绝）；app/office/voice-gateway/voice-tunnel 非 root；历史高危（Qdrant 1.15 任意文件写、审计越权、跨设备删任务、RAG 撤权）在运行制品与代码中均已关闭。
- 缺口：无 CI 安全扫描（P0-3）；`/admin/` 无 MFA（P0-4），密码校验器仅 2 个、无登录限速、DRF 同时启用 Basic 认证；HTTPS 全套默认关闭（`KB_HTTPS_ONLY` 未设，`check --deploy` 报 W004/W008/W012/W016），当前仅回环 `127.0.0.1:8800` 风险可接受，但缺"扩大暴露面前必须开启"的强制门禁；解析沙箱不完整——PDF/DOCX/XLSX/图片仍在 web/worker 进程内解析（Office 已隔离），属纵深防御缺口；错误细节外泄（见 §4）。

### §6 交付验收 — 部分符合

- 正向：本轮实测两轮全量（SQLite 1673 passed/65 skipped/38 failed，265.92s；PostgreSQL 1696/42/38，326.50s，0 error），失败集与 11-09 基线逐条一致（新增 0、消失 0）；`manage.py check` 与 `makemigrations --check --dry-run` 均 exit 0；线上只读冒烟全绿（live/ready 200、登录页 200、受保护页 302→登录、未认证 API 401、无 CSRF token POST 403、404 无堆栈）；app/office/qdrant 镜像 ID 与文档逐字一致且为运行镜像，`postgres-cutover.dump` SHA-256 与文档一致，两套快照齐全，54 条迁移/6 触发器/`guards` exit 0 均可复现复核；备份恢复有完整性证据。
- 缺口：38 项常红无正式豁免（P0-2）；无 CI（P0-3）；缺恢复**耗时**记录、RPO/RTO 未定义、1020 个向量冷备 SHA 校验无结果文件、发布时点的 HTTP 冒烟记录缺失（现有 `smoke*.txt` 实为 Office 转换输出）；交付文档有事实错误——`README.md:40` 仍称默认镜像安装"LibreOffice 与中文字体"，而 11-10 起应用镜像已移除 LibreOffice（`deploy/app/Dockerfile` 无 soffice，Office 由独立容器承担）；11-09 文档"两条构建路径均 pip check"、`.venv` Pillow 版本等表述与现状不符；Android 安装包与 Note4 固件属另一发布面，不在本轮范围。

## 四、可直接引用的正向证据

- 两轮全量回归实际数字与失败清单一致性：`artifacts/review/fullstack-rules-20261111/logs/pytest-{sqlite,postgres}.txt`、`logs/failure-diff.txt`。
- 线上冒烟：`logs/smoke-20261111.txt`（状态码、响应头、CSRF 403、404 无堆栈）。
- 发布制品与恢复：`logs/delivery-evidence.txt`（三镜像 ID、54 迁移、6 触发器、`guards` 输出、快照清单与 SHA-256）。
- 后端安全与信任边界：`04-backend-security.md` §5（历史修复核对）、§7（24 入口抽查）。
- 前端实测：`03-frontend.md` §3 与 `shots/` 59 张截图、6 份断言 JSON（四档宽度零溢出零 JS 错、对比度、键盘、缩放）。
- 供应链：`01-selection-licenses.md` §5（JAR 72/72、ASR 31/31、OSV 全量 0 命中）。

## 五、建议闭环顺序

**P0（放行前必须完成，按成本从低到高）**
1. 建最小 CI（pytest 子集 + `check --deploy` + pip-audit/OSV + gitleaks/ruff），或先出一份可复现扫描证据并归档。
2. Django 6.1.2→6.1.3（同分支补丁，风险低）；同时显式设置 `DATA_UPLOAD_MAX_MEMORY_SIZE`/`FILE_UPLOAD_MAX_MEMORY_SIZE`。
3. 处理 38 项失败：更新旧确认协议用例（区分"自动执行"设计变更）或标记 legacy 并登记豁免字段。
4. 整理候选提交边界（Web 重构 / RAG / 语音 / 固件 / Android 分开），构建脚本记录 digest。
5. `/admin/` 补 MFA 或按 §0 完成延期记录；字体授权核实或替换 Noto CJK；锁定件入库。
6. 基础镜像钉 digest；`.dockerignore` 补 `snapshots/`；`deploy/worker/Dockerfile` 补 `--no-deps` + `pip check`。

**P1（低成本、本次应修）**
- 前端 3 项 WCAG P1（弹窗 `role=dialog`+`aria-modal`+焦点循环、行复选框 `aria-label`、搜索 `aria-live`）；删除 `kb-proto.css`；补 1.4.11 边框对比设计决策。
- 错误响应收敛为稳定错误码+可执行提示（内部细节进日志）；重试退避加随机抖动。
- 修正 `README.md:40` LibreOffice 描述与 `apps/knowledge/office_convert.py:479-481` 过期注释；`kb-home.css` 硬编码色逐步迁回令牌。

**P2（登记跟踪）**
- OpenAPI/设备协议版本校验、解析进程沙箱化、HTTPS 强制门禁、滚动发布共存验证、RPO/RTO 与恢复耗时记录、1020 个向量冷备 SHA 记录、屏幕阅读器抽测、停用库语义文档化、会话空闲超时、密码校验器与 Basic 认证收敛、Redis SSPL 法务评估、psycopg LGPL 记录。

## 六、未验证范围与限制

- AS711 真实模型链路（默认 marker 跳过，未探测）；Linux 高并发/崩溃恢复；PostgreSQL 跨版本升级；滚动发布新旧版本共存。
- 屏幕阅读器（NVDA/TalkBack）实测、像素级视觉观感复核（审查会话无图像输入），已用 Chromium AX 树与 59 张截图替代。
- 真实用户 Core Web Vitals（无真实流量，属上线后跟踪）；真实口令登录成功路径（用临时会话注入替代，已删除）。
- Docker Scout 整镜像复扫未在本轮重跑；11-09 记录的 Qdrant/libxml2 阻断按 11-10 发布证据核对为已消除，但未重新扫描确认剩余告警。

## 七、附件

- 分线报告：`artifacts/review/fullstack-rules-20261111/01-selection-licenses.md`、`03-frontend.md`、`04-backend-security.md`、`05-delivery.md`
- 证据：同目录 `logs/`（测试、冒烟、Django 检查、镜像与快照、失败清单对比）、`shots/`（59 张浏览器截图）、`browser-evidence*.json`、`visual-structure.json`、`test-quality-scan.txt`
