> 注：本报告为 2026-11-13 上午审计快照；当日随后完成 Python 3.15 适配与根目录重构（`config/`→`knowledgebase/`、`data/` 集中运行数据，见 [python315-and-layout-migration-2026-11-13.md](python315-and-layout-migration-2026-11-13.md)）。报告中的文件路径与行号均指重构前状态。
# 智库系统 · 全栈开发规则合规审计（2026-11-13）

- **审计日期**：2026-11-13（Asia/Shanghai）。
- **规则基线**：《全栈开发规则》**v2.5.0**（生效 2026-11-13，文件头版本与生效日期已核对），`D:/WorkSpace/_study/全栈开发规则.md`，审计时工作树版本 SHA-256 `38C3C0903897C995DB89EBF1F1C80D532219796B00A28D3D58F1410E3E005878`。规则仓库本身有未提交修改（见 F-19），本审计以该工作树内容为准。
- **审计对象**：`D:\AProjectScript\KnowledgeBase`。候选 = Git `main` HEAD `e9f92f6` + **12 个修改文件** + **10 个新增路径**（其中 9 个已由 owner 暂存、`docs/remediation-2026-11-13.md` 未暂存；代码类有效变更约 315 行，另含审计/整改文档；**尚未提交**）。运行制品：`knowledgebase:app` = `sha256:9ceccc4da1e052e6dd270e99d7f33d5e2fd39a5f14f7ed561e504fd5dcc22dba`；`knowledgebase:voice-gateway` = `sha256:df7984963993e3835e0f64f406d6846323fd01f3278468ceabfd0f76ff7c6994`。本机 Docker 现场有 **12 个 knowledgebase 容器**在运行（web/worker/scheduler/postgres/redis/qdrant/office/https/voice-*；migrate 为一次性容器，运行成功后已退出），另有 2 个无关项目容器同样在运行。
- **审计方式**：只读现场核验 + 本机隔离/本地复跑。未修改运行数据、未启停服务、未读取或输出任何凭据；新增证据写入 `artifacts/audit-20261113/`。既有证据与本次复跑分别标注（0.7/0.8）。

## 结论

**判定：部分符合；合并与生产发布门禁不通过（阻断）。** 2026-11-12 安全整改的四个方向（数据库最小权限、后台 MFA、回环 HTTPS、CI 配置）在现场均可核验到实现；截至 2026-11-13 10:16，7 项阻断中 **P0-1 已闭环（PostgreSQL 已升 17.12）**、P0-5 按测试数据 owner 判定降为「生产发布适用」，其余 5 项未闭环：

1. ~~**已部署 PostgreSQL 为 17.11，落后 2026-11-12 发布的 17.12 共 52 个 CVE 修复（含多个 CVSS 8.8 任意代码执行）**（附录 A 第 5 条 / §1）。~~ **已于 2026-11-13 10:05 升级到 17.12 并复核通过**（owner 10:02 确认；测试数据不备份）。证据与回归见 [docs/remediation-2026-11-13.md](remediation-2026-11-13.md) 与 `artifacts/audit-20261113/upgrade-postgres-17.12/`。该项不再阻断。
2. **系统管理员权限仍可单因子触达**：MFA 只覆盖 Django `/admin/`，`is_staff` 授权的管理 API（审计读取、运行指标、全局库操作等）通过 `/app/login/` 会话或 DRF Basic 认证即可使用（§5「管理入口采用多因素认证」）。
3. **6.1 默认必需检查未满足**：`ruff format --check` 失败（283/359 文件），CI 无格式门禁、无类型检查、无镜像构建作业；仓库无远端，CI 从未运行（§6/§6.1）。
4. **候选制品与证据链不闭合**：运行镜像 `9ceccc…` 无对应 `snapshots/builds` 构建记录；`deploy/https/`、`governance/admin_*.py`、`provision_runtime_db.py`、`enroll_admin_totp.py` 等关键新文件未提交，且被已跟踪文件硬依赖（只 review `git diff` 会漏审，部分暂存会启动失败）（0.8/§6）。
5. **备份/RPO（仅生产发布适用）**：`docs/backup-restore-policy.md` 的每日备份仍未有受管任务，最新 PG 备份 11-10、Qdrant 快照 11-09，完整恢复未计时。**但本机当前环境为测试数据部署，owner 已于 2026-11-13 09:51 在会话中明确「都是测试数据，不用备份」**，故本机测试阶段不要求备份/RPO 证据（记录见 §七）；若未来转入真实业务数据或对外发布，该项恢复为阻断项。
6. **延期登记册无 owner 批准**：16 项批准人全部「待签」，D-01/D-02 已整改但状态未更新，D-14~D-16 三行落在表格外（0.11/§7.2）。
7. **许可未闭环**：字体商用授权、Redis RSALv2/SSPLv1、psycopg LGPL-3.0 未形成 owner 决策；D-14 的依赖 hash 固定未做（附录 A 第 9 条）。

本机测试部署（仅回环、无外部用户、当前全部为测试数据）在 owner 接受剩余风险的前提下可继续联调；**P0-1（数据库补丁）已于 2026-11-13 10:05 完成（PG 17.12，双库回归通过）**，其余阻断项见 §一。高风险整改候选必须按 §7.2 由人类 owner 在会话中确认，本报告不代为确认（附录 A 第 8 条）。

---

## 一、阻断项

| ID | 条款 | 发现 | 证据（可复核） | 影响 | 整改建议 |
| --- | --- | --- | --- | --- | --- |
| **P0-1（已闭环）** | 附录 A-5、§1、§5 | **原发现**：正式库 PostgreSQL 17.11，17.12（2026-11-12）修复 52 个 CVE，多个 8.8 任意代码执行（如 CVE-2026-120139，`<17.12` 受影响）。**处置（2026-11-13 10:05）**：镜像升级为 `postgres:17.12-alpine@sha256:34261f59…8b6f`，同一命名卷原地升级；`select version()` = PostgreSQL 17.12；迁移 0 变更、6 个触发器在位、`kb_runtime` 权限不变、ready 200、`check --deploy` 0 警告；库内无 GIN/GiST 索引与 pg_trgm 扩展，公告要求的 REINDEX 步骤不适用 | 现场版本查询；[17.12 发布公告](https://www.postgresql.org/about/news/postgresql-187-1712-1616-1520-1425-and-19-rc-2-released-3442/)；[CVE-2026-120139](https://www.postgresql.org/support/security/CVE-2026-120139/)；升级证据 `artifacts/audit-20261113/upgrade-postgres-17.12/`；[remediation-2026-11-13.md](remediation-2026-11-13.md)；升级后双库回归 1746/0/0（PG 28 跳过、SQLite 51 跳过） | 应用持有 `kb_runtime` 数据库凭据；旧版本存在可升级为数据库进程权限/宿主代码执行的高危 CVE | **已完成**：镜像固定新摘要、重建容器、复跑迁移/触发器/角色/回归；备份按 owner 判定（测试数据）免除。后续随 17.x 安全公告持续更新 |
| **P0-2** | §5「管理入口 MFA」、附录 A-2 | 后台 `/admin/` 已强制 TOTP（`MfaAdminSite` + `OTPAdminSite.has_permission`，登录页含 `otp_token`），但 **`is_staff`/`is_superuser` 即系统管理员**（`apps/knowledge/permissions.py:25-29`），其管理 API（`/api/v1/audit/events/`、`audit/verify/`、`knowledge/runtime-metrics/`、全局库操作等，见 `apps/knowledge/views.py:674,907,1160,1264,1313,1402,1633`、`apps/planner/services.py:467` 等）经 `/app/login/` 会话或 DRF `BasicAuthentication`（`config/settings.py:482-485`）即可访问，**不要求 OTP**。现场 0 个已登记 TOTP 设备（staff 3、superuser 2） | `config/settings.py:482-485`；`apps/knowledge/permissions.py:25-29,65,89`；`apps/audit/views.py:26-37`；`apps/knowledge/views.py` 多处 `is_system_admin`；现场查询 `TOTPDevice` 计数 0；`tests/test_audit_read_permission.py` 只验证 staff 可读、未验证 MFA | 口令泄露（无 OTP）即可读取全库审计/系统指标并执行全局管理操作，MFA 目标被绕过 | 二选一并在 §7.2 留痕：①管理 API 的 `system admin` 权限同时要求 `user.is_verified()`（先经 `/admin/` 完成 OTP，再复用会话）；②把全局管理操作收敛到 `/admin/`，产品 API 只保留库级权限。无论哪种，需补「单因子 staff 会话访问管理 API 被拒」的回归测试 |
| **P0-3** | §6.1、§2、§6 | `ruff format --check` 失败：**283/359 文件需重排**（76 已格式化）；CI（`.github/workflows/ci.yml`）只跑 `ruff check`，无 `ruff format --check`、无类型检查、无 Docker 构建作业；`ruff`/`pip-audit` 未固定版本（`ci.yml:29`）；仓库无 remote，CI 无任何运行记录 | `artifacts/audit-20261113/ruff-format-check.log`；`ci.yml:28-33,44-51`；`git remote -v` 空；`snapshots/builds` 无 11-12 记录 | 合并前必需检查不完整，格式基线无法执行；类型契约无门禁；「CI 通过」不可作为放行证据（§0.7） | ①全仓 `ruff format` 单独提交（与行为变更分离）；②CI 增加 `ruff format --check`、`mypy`（或明确登记不适用并说明理由）、`docker build`/`docker compose config`；③固定 ruff/pip-audit 版本；④接入远端并保留成功运行记录，或按 0.8 归档等效本地证据。**阻断合并** |
| **P0-4** | 0.8、§6 | 运行镜像 `9ceccc…` 建于 2026-11-12T01:21Z，但 `snapshots/builds` 最新记录仍是 2026-11-11 13:13 的 `6f59aa7`；候选的 `deploy/https/Caddyfile`、`governance/admin_config.py`、`admin_site.py`、`provision_runtime_db.py`、`enroll_admin_totp.py`、`tests/test_admin_mfa.py` 当前已由 owner 暂存但尚未提交，而 `config/settings.py:155`、`docker-compose.unified.yml:265,276` 硬依赖它们（提交时须整体提交，评审须看 `git diff --cached` 而不只是 `git diff`） | `snapshots/builds/20261111-131335-build.json`；`git status --porcelain`（A/AM 状态）；`docker image inspect knowledgebase:app` | 源代码↔制品↔证据无法对应；只 review 未暂存差异会漏掉新实现文件；部分提交会产生「启动即崩/挂载失败」的树 | `git add -N` 纳入新文件后整体评审并提交；重建镜像并写入 `snapshots/builds`（含 gitHead/dirtyFiles/摘要）；报告与整改文档一并入库。**阻断合并** |
| **P0-5** | §6「验证数据恢复」、附录 A-6 | `docs/backup-restore-policy.md` 规定 RPO ≤24h、每日 02:00 `pg_dump` + 每周 Qdrant 快照，但同文件 §5「待补齐」承认每日备份尚未做成受管定时任务；`Get-ScheduledTask` 无匹配任务；最新 PG 备份 `snapshots/kb-office-release-20261109/postgres-cutover.dump` 为 **11-10 11:19**，Qdrant 快照 **11-09 17:17**；11-11 只做了单项 PG 恢复（0.84s），未做 PG+Qdrant+原件完整恢复计时 | 备份/快照 mtime；策略 §2/§5；`artifacts/review/fullstack-rules-20261111/logs/restore-rehearsal.txt` | **生产发布阻断；本机测试阶段不适用**：owner 于 2026-11-13 09:51 会话确认当前全部为测试数据、无需备份（§七记录），测试数据丢失由 owner 接受 | 生产数据/对外发布前必须补齐：①自动备份（含失败告警）；②完整恢复演练（分阶段计时、权限冒烟、丢失窗口核对）。本机测试阶段可只保留一次性 dump 或直接重建 |
| **P0-6** | 0.11、§7.2 | `docs/deferral-register-2026-11-11.md` 16 项延期批准人全部「待签」；D-01（MFA）/D-02（HTTPS）已在本候选实现，登记仍为「已登记」；D-14~D-16 三行位于「复评触发条件」之后、表格之外（Markdown 结构损坏）；D-15 的宽泛异常数量与实际不符（登记 ~108/91，实测 **103/86**） | 登记册 :6-28；实测计数（本次）：`except Exception` 103、`noqa: BLE001` 86 | 按 0.11「无批准/无期限的遗留视为阻断项」，延期项不能作为放行依据；登记与实际状态漂移 | 由 owner 逐项确认（批准/驳回/关闭），补齐批准人与期限；D-01/D-02 改为「已关闭（证据链接）」；修复表格结构并按实测修订 D-15 数量。**阻断合并/发布** |
| **P0-7** | 附录 A-9、§1 | 许可未闭环：`media/fonts/SOURCES.md` 仅记录方正小标宋下载页与哈希，无授权书（方正官方声明商业发布需付费授权），Windows 字体副本不可随产品分发；Redis 7.4 为 RSALv2/SSPLv1（非 OSI）；psycopg/psycopg-binary 为 LGPL-3.0-only（dist-info `License-Expression` 核对）；依赖未 `--require-hashes`（D-14） | `media/fonts/SOURCES.md`；[方正官方声明](https://www.foundertype.com/index.php/About/statement.html)；`requirements.txt`（74 个 `==`，无 hash） | 对外交付/商用存在授权风险；供应链可复现性只到版本级 | ①内部自用由 owner 书面确认并登记；②对外前替换为 Noto CJK 或购授权；③Redis/psycopg 由 owner/法务确认内部使用口径；④评估 `--require-hashes` 成本并登记。**阻断对外交付/SaaS** |

---

## 二、合规矩阵（v2.5.0）

| 条款 | 判定 | 本次依据与缺口 |
| --- | --- | --- |
| §0 范围与放行 | **不放行** | 候选身份、制品与现场可追溯（P0-4 例外）；延期无批准（P0-6）；高风险变更无 owner 确认（P0-2、§7.2）；证据对象化不完整（见 F-06/F-12） |
| §1 选型与许可 | **部分符合** | Django 6.1.3、django-otp 1.7.5、Caddy 2.11.9、Qdrant 1.16.3 均有版本/来源记录；**postgres 已于 2026-11-13 升级到 17.12 新摘要并现场复核（P0-1 闭环）**；Redis 7.4.11 落后 7.4.12（F-09）、字体/Redis/psycopg 许可未闭环（P0-7）；74 个 Python 依赖 OSV 0 命中（本次复跑） |
| §2 代码与可维护性 | **部分符合** | `ruff check` 全过；单测覆盖广（1746 用例）；但格式门禁失败（P0-3）、无类型检查；模块依赖方向倒置、可见性规则重拼、29 个函数圈复杂度 >15、宽泛异常未逐项审查（F-13） |
| §3 前端 | **部分符合** | Impeccable 产物与 28 个浏览器证据条目存在；语义化/键盘/减少动效基本到位；但真实屏读器、CWV 仍未做（D-07/D-08），320/720 覆盖与文档声明不符，证据早于 11-11 前端修复，Impeccable 版本/命令未记录（F-14） |
| §4 接口与可靠性 | **部分符合** | 授权/对象归属、分页（20）、迁移 57 条、6 个触发器、事务/CAS 路径均有实现与回归；OpenAPI 缺（D-05）、未知 API 路径返回 HTML 404 而非 JSON、错误体非 RFC 9457（或未记录等价标准）（F-16）；监控/告警阈值与响应人无正式验收记录 |
| §5 安全与差异复审 | **不放行** | 运行账号最小权限、MFA 管理站、HTTPS、容器隔离均实测通过；但 PG 高危 CVE（P0-1）、管理 API 单因子（P0-2）、无全局 CSP/Permissions-Policy（F-05）、密钥文件容器内 0777（F-07）、独立 T1 复核与 owner 确认缺失（P0-4/§7.2） |
| §6 交付与验收 | **部分符合** | 现场就绪/部署自检/回归可复核；6.1 未满足（P0-3）、构建记录缺失（P0-4）、备份/RPO 仅生产发布适用且本机测试数据已由 owner 确认无需备份（P0-5）、运维文档缺 MFA 步骤与 HTTPS 回滚/观察方案（F-15） |
| 附录 A 红线 | **部分通过** | **A-5 已于 2026-11-13 升级 17.12 后闭环**；A-8（高风险候选整体仍缺 owner 确认，仅 PG 升级已确认）、A-9（许可未核实）不满足；A-1/2/3/4/7/10 本次未发现反例（见第三节） |
| 附录 C 自检表 | **未全部勾选** | 逐项见第五节末尾 |

---

## 三、2026-11-12 整改逐项复核

| 控制 | 整改声明 | 本次核验 | 结论 |
| --- | --- | --- | --- |
| 数据库最小权限 | PG 升 17.11、迁移/运行分号、`kb_runtime` 无超级/建库/建角色（2026-11-13 已再升 17.12） | 现场：`kb_runtime`（`rolsuper/createdb/createrole/rolreplication/rolbypassrls` 全 false，无角色成员，schema CREATE=false/USAGE=true）；web/worker/scheduler/voice-worker `current_user=kb_runtime`；`django_migrations` IUD 全 false；`audit_auditevent` INSERT=true、UPDATE/DELETE=false；6 个触发器在位；迁移 57 条；**PG 17.12 升级后复核结果一致**。所有者 `kb` 仍为 superuser（F-10） | **实现达标；P0-1 已闭环** |
| 后台 MFA | `django-otp` 1.7.5 + `OTPAdminSite`，TOTP 登记/验证/重放测试 | 现场 `admin.site` 包装类 = `governance.admin_site.MfaAdminSite`；登录页含 `otp_token` 字段与 `django_otp` 样式；`has_permission` 来自 `OTPAdminSite`；`tests/test_admin_mfa.py` 覆盖「未验证会话 302」「仅口令 200」「有效 TOTP 登录 302」「重放 200/302」；现场 TOTP 设备数 0（staff 3、superuser 2），登记流程未实际演练 | **实现达标；管理 API 覆盖缺口（P0-2）** |
| Web HTTPS | Caddy 2.11.9 回环终止 TLS、Django Secure Cookie/HSTS、私网反代 | 现场 `https://localhost:8800/api/v1/health/ready/` 200 且三项检查 ok；HSTS `max-age=31536000; includeSubDomains; preload`、nosniff、`X-Frame-Options: DENY`、`Referrer-Policy: same-origin`；CSRF Cookie `Secure; SameSite=Lax`；`/admin/` 302→登录；未认证 API 401；`docker port` 只有 Caddy 回环 8800，web 8000 未发布；`docker compose config -q` 通过；Caddy 配置校验通过（有格式警告） | **实现达标** |
| CI 与复审 | Actions 按提交摘要固定、最小权限、Ruff/Bandit/pip-audit/Gitleaks/SQLite+PG/迁移/严格部署检查 | workflow 结构完整、4 个 job；Actions SHA 与上游 tag 一致（checkout `11d5960…`、setup-python `a26af69…`、gitleaks-action `dcedce4…`，2026-11-13 经 GitHub API 核对）；但仓库无 remote、无运行记录；缺格式/类型/构建门禁；gitleaks/依赖扫描无法在 CI 侧复现（本次用本地 gitleaks/OSV 替代）；独立复核仍未签署 | **配置基本达标，证据与门禁不闭合（P0-3、P0-4）** |

---

## 四、6.1 默认必需检查执行结果（本次在候选工作树复跑）

| 检查 | 命令/方式 | 结果 | 结论 |
| --- | --- | --- | --- |
| 格式检查 | `ruff format --check apps config governance tests scripts` | **283 个文件需重排、76 已格式化，exit 1** | **失败（P0-3）** |
| 静态检查 | `ruff check …`（ruff 0.14.4） | All checks passed，exit 0 | 通过 |
| 类型检查 | 项目无 mypy/ty/pyright 配置 | 未运行（不适用未登记） | **缺口（P0-3）** |
| 单元测试 | `pytest -q --kb-db=sqlite`（当前树） | 1746 用例，0 失败/0 错误，51 跳过（1695 pass），222s | 通过（证据 `pytest-sqlite-20261113.xml`） |
| 单元测试（PostgreSQL） | `pytest -q --kb-db=postgres`（升级到 17.12 后重跑，JUnit `artifacts/audit-20261113/pytest-postgres-20261113.xml`） | **1746 用例，0 失败/0 错误，28 跳过（310.7s）** | 通过（升级后新证据） |
| 构建 | CI 无构建作业；本机镜像 `9ceccc…` 存在并被运行 | 镜像可运行；无构建记录 | **证据缺口（P0-4）** |
| 依赖漏洞扫描 | OSV `querybatch`（74 个 PyPI 固定版本，2026-11-13） | 0 命中（`osv-batch-20261113.json`）；CI 的 pip-audit 无运行记录 | 通过（Python 面）；容器 OS 包未扫描（F-09） |
| 密钥扫描 | gitleaks v8.28.0：Git 历史 19 提交 + 候选 8 个代码目录 | 均无泄漏（`gitleaks-*.log`） | 通过（CI 侧无运行记录） |
| 变更说明与回滚 | `docs/security-remediation-2026-11-12.md`、`docs/deployment-and-operations.md` | 变更说明有；**HTTPS/Caddy 变更无回滚与观察方案**；MFA 登记步骤未进部署文档 | **部分缺口（F-15）** |
| 源码安全扫描 | Bandit 1.9.4 `-lll -iii` | 高危 0；medium 10、low 23（B608 迁移/触发器 SQL 拼接、B104 voice-gateway 0.0.0.0）；未逐条归档分诊 | **部分缺口（F-08）** |
| 部署自检 | 运行容器内 `manage.py check --deploy --fail-level WARNING` | 0 issues，exit 0 | 通过 |
| 迁移一致性 | `manage.py makemigrations --check --dry-run` | No changes detected | 通过 |
| 依赖完整性 | `pip check` | No broken requirements found | 通过 |

---

## 五、现场核验通过项（可作为放行证据）

- **服务就绪**：`https://localhost:8800/api/v1/health/ready/` → 200，`database/migrations/vector_data` 均 `ok`；`check --deploy` 0 警告。
- **认证边界**：`/admin/` → 302 登录页；`/admin/login/` 含 OTP 字段；未认证 `/api/v1/knowledge/stats/`、`planner/conversations/`、`audit/events/`、`voice/devices/` 均 401 且返回统一 `{"detail","code"}`；`/admin/login/`、`/app/login/` 的 CSRF Cookie 为 `Secure; SameSite=Lax`。
- **数据库**：运行账号 `kb_runtime` 无超级/建库/建角色/复制/绕过 RLS，无角色成员，public schema 无 CREATE 权限；`django_migrations` 无 IUD；`audit_auditevent` 仅 INSERT；6 个不可篡改触发器在位；57 条迁移；**库版本 PostgreSQL 17.12（2026-11-13 升级，P0-1 闭环）**；库内无 GIN/GiST 索引、无 pg_trgm 扩展，17.12 公告的 REINDEX 建议不适用。
- **容器**：web/worker/scheduler/voice-worker 以非 root `kb` 运行；office 以 `kb` 运行且 `network_mode:none`、`read_only`、`cap_drop: ALL`、`no-new-privileges`、pids/mem 限制；Qdrant 仅内部网络无宿主端口；voice-gateway（uid 10001）路径白名单 + 无 Bearer 401 + 管理路由 404（本次容器内实测）；voice-tunnel uid 10001、临时目录 tmpfs。
- **网络暴露面**：宿主仅 `127.0.0.1:8800`（Caddy）、`127.0.0.1:55432`（PG）、`127.0.0.1:56379`（Redis）有映射；web/worker/scheduler 无宿主端口。
- **依赖/供应链**：`requirements.txt` 74 项全部 `==` 固定；OSV 0 命中；Git 历史与工作树 gitleaks 0 泄漏；Actions 按 SHA 固定且与上游 tag 一致；基础镜像 `python:3.13-slim` 按 digest 固定；Office 构建的 Maven 分发按 SHA-512、jar 按 sha256 校验。
- **回归**：PostgreSQL 17.12 升级后重跑双库全量——PG **1746/0/0/28**（310.7s，`pytest-postgres-20261113.xml`）、SQLite **1746/0/0/51**（241.6s，`pytest-sqlite-20261113-post-upgrade.xml`）；`ruff check`、`bandit -lll`、`makemigrations`、`pip check` 通过。
- **证据归档**：证据索引与命令见 `artifacts/audit-20261113/`（`audit-manifest.md`）。

---

## 六、非阻断发现（按优先级）

**P1（发布前应处理）**

- **F-05 安全响应头不完整（§5）**：无全局 `Content-Security-Policy`，无 `Permissions-Policy`，无全站 `frame-ancestors`（仅有 `X-Frame-Options: DENY`）。Django 6.1.3 已内置 `django.middleware.csp.ContentSecurityPolicyMiddleware` 与 `SECURE_CSP`/`SECURE_CSP_REPORT_ONLY`（`django/conf/global_settings.py`），但 `config/settings.py:175-190` 未启用；仅 `apps/planner/rag/views.py:41,63` 两个 HTML 响应手写 CSP（含 `style-src 'unsafe-inline'`）。建议先 Report-Only 观察、再收紧；若暂不做须按 §7.1 登记偏离。
- **F-06 管理 API 与 MFA 口径不一致（§5）**：见 P0-2，需 owner 决策；同时补 `is_verified` 回归。
- **F-07 运行密钥文件在容器内为 `777 root:root`（§5 凭据保护）**：`/run/secrets/postgres_runtime` 现场 `777`、64 字节；文档称「只读文件注入」，在 Windows Docker Desktop 下 Compose file secret 的实际权限与声明不符。建议：文档如实标注平台差异；对生产（Linux）验证 0444 并留证；或改用 `secrets` 的 `environment` 注入/受限 ACL，评估爆炸半径。
- **F-08 Bandit 中危未分诊（§5「检查结果归因」）**：10 项 medium（B608 迁移 `0019_classification_source_and_note.py:89-114` 与 `governance/triggers.py:151,178,255,278` 的 f-string SQL、B104 `deploy/voice-gateway/server.py:103` 绑定 0.0.0.0）。经代码核对：B608 只拼接模块常量表名/列名、无外部输入；B104 容器内无宿主端口。**建议按「误报/受控」登记工具、规则、版本与排除依据**，否则不满足 0.5「告警分别记录」。
- **F-09 容器 OS 包与镜像层未扫描（§1/§5/6.1 依赖扫描）**：本次只覆盖 PyPI 依赖；Redis 容器实为 **7.4.11**（镜像 `created=2026-09-17`），上游 **7.4.12**（2026-10-26）已发布且含安全修复，当前镜像早于该版本（集群模式关闭，部分攻击路径不可达，风险受限）。Qdrant 部署 1.16.3；其 GitHub 安全公告页标题确认存在「分布式模式内部 gRPC 默认不鉴权」公告（GHSA-frc7-44gm-99qq），本次未能在 OSV/GitHub API 复核版本区间与评级，当前单节点部署不启用分布式模式，暂按缓解登记、随升级评估。建议：补 Docker Scout/Trivy 镜像扫描并归档，升级 Redis/Qdrant 或登记补偿控制。
- **F-10 数据库所有者仍为超级用户（§5 最小权限残余）**：`kb` 的 `rolsuper/createdb/createrole` 均 true，迁移与备份使用该身份；一旦 migrate/备份链路被利用可越过全部触发器。建议评估迁移是否可用非超级所有者（建库后降权），并把所有者口令的使用面限制到一次性 migrate 服务（当前 compose 已做到应用容器不持有）。
- **F-11 `KB_TRUST_PROXY_HEADERS` 为无效配置（§2/§4）**：`deploy/docker-compose.unified.yml:57` 设为 `1`，但 `config/settings.py` 未定义该设置（现场 `getattr(settings,'KB_TRUST_PROXY_HEADERS')` 为 `<undefined>`），`governance/middleware.py:77-82` 永远走 `REMOTE_ADDR` → 所有浏览器请求的限速键 IP 都是 Caddy 容器地址，与中间件文档「客户端 IP」不符。且若直接补上该设置而不同时处理 XFF（中间件取第一个值），存在伪造绕过风险。建议：明确客户端 IP 归属方案（Caddy 覆盖 XFF + 取最后/可信跳数），或删除该 env 并如实记录限速口径。
- **F-12 构建与证据归档（0.8）**：候选镜像无构建记录（P0-4）；`artifacts/`、`snapshots/`、`.impeccable/review/`（`*`）均被 gitignore，浏览器证据/截图/报告不在版本库；`artifacts/security-123/osv.json` 内容为 `null`（无查询元数据），`gitleaks.log` 仅 3 行、不覆盖未跟踪候选。建议：发布证据按 0.8 归档为 Git 提交或只读制品记录，扫描证据保存工具版本/命令/输入摘要/结果。
- **F-13 代码质量（§2，v2.5.0 新增重点）**：
  - 依赖方向：`apps/knowledge/classification.py:507`、`apps/knowledge/views.py:1085` 在函数内反向导入 `apps.planner.llm`，无「规避循环导入」说明；planner 侧为模块级导入（`apps/planner/services.py:24-33`）。建议下沉 LLM profile 适配层或加 import-linter 门禁。
  - 复用边界：`apps/knowledge/views.py:716` 注释称可见性口径「只有这一处」，但列表 `views.py:168-176`、统计 `views.py:1456-1464`、`apps/voice/standby.py:20-25` 各自重拼 `allowed + is_active`，且 planner/tests 直接导入视图私有函数（`apps/planner/rag/reader.py:10`、`tests/test_search_eligible_scope.py:95`）。建议抽 `knowledge/access.py` 公开入口。
  - 复杂度：以 `C901`（阈值 15）检查得到 **29 个函数超标**；实测最高为 `apps/planner/services.py:4108 _dispatch`（123）、`apps/planner/services.py:984 reply_to_message`（43）、`apps/planner/services.py:3280 _reset_library_index`（39）、`apps/planner/document_export.py:1063 document_to_docx`（34）、`apps/knowledge/pipeline.py:466 embed_chunks`（33）、`apps/knowledge/pipeline.py:1833 search`（49）；`pyproject.toml` 未启用 C901，超出「默认 ≤15，超出须说明理由」且无拆分计划。
  - 宽泛异常：`except Exception` 103 处、`noqa: BLE001` 86 处（登记为 ~108/91）；抽审 `apps/voice/management/commands/voiceworker.py:34-35,53-54` 仅打印异常类名、无 traceback/失败计数，空转时会掩盖故障；`governance/triggers.py:88-105,112-116` 兜底仅 `logger.debug` 且取列为空集时退回全部候选列，可能生成引用已删列的坏触发器。
  - 注释一致性：`apps/knowledge/models.py:184` 称状态字段「只能通过 update_parse_status() 变更」，但 `apps/knowledge/pipeline.py:309-319` CAS 直写、`vectorindex.py` 有批量写路径；建议更新说明。
  - 新增命令无测试：`provision_runtime_db`、`enroll_admin_totp` 无自动化测试（`tests/` 中 0 命中）；本次仅通过现场角色查询与人工代码审查验证。
- **F-14 前端证据链（§3/附录 C）**：
  - 文档称「11 页 × 1440/720/390/320 四档」，归档 `browser-evidence.json` 实际为 11 页 × 2 档（1440/390）+ 3 页 zoom200 + 3 页 reflow-320，共 28 条；无 LCP/INP/CLS/longtask/layout-shift 数据；无 3 次重复与基线对比；`requirements.txt` 未锁 Playwright（venv 实装 1.64.0）。
  - 证据时间：主 sweep 11-11 11:28-11:36 早于前端修复提交 `76872dc`（13:05），修复后仅 `a11y-recheck.json` 复验 4 页；证据未绑定 commit/镜像摘要。
  - Impeccable：`.impeccable/design.json`（schemaVersion 2，2026-11-09）与 surface brief 存在，但无 CLI 版本/命令/退出码记录（用户级 skill 版本 0.1.19）；`detector` 192 条无分诊产物（D-13 声称已分诊）；`.impeccable/review/.gitignore` 为 `*`，34 张评审图不在版本库。
  - 无障碍缺陷（可静态复现）：`apps/webui/templates/webui/assistant.html:18` 标题用 `span.chat-title`，h1 仅由 `kb-assistant.js:682` 在空态注入，加载既有会话时无 h1；`assistant.html:118,130,159` 三处 orphan `<label>`；`files.html:56-61` 表格无 `caption`、`th` 无 `scope`；`_navbar.html:4` 的 `aria-controls="kb-sidebar"` 指向不存在 id，且该模板无任何 include。
  - 多语言/RTL：无 gettext/`{% trans %}`、无 RTL 回归；若产品确定仅中文，须按 0.4/§3 记录「已判定不适用」及判定人。
- **F-15 运维文档（§6）**：`docs/deployment-and-operations.md` 首次部署步骤未包含 `enroll_admin_totp`（只有 README 与整改文档提到），按文档走会无法登录 `/admin/`；HTTPS/Caddy 变更无回滚步骤、观察指标、停止条件与负责人；`deploy/https/Caddyfile` 未通过 `caddy fmt` 格式检查（validate 通过）。建议补齐。
- **F-16 API 契约一致（§4）**：未匹配的 `/api/v1/*` 返回 Django HTML 404（`Content-Type: text/html`），与 JSON API 契约不一致；业务错误体为 `{"detail","code"}`，非 RFC 9457（也未记录等价标准）。建议统一 JSON 404 并记录错误契约基线（结合 D-05 OpenAPI 一起做）。
- **F-17 默认口令模板（§5 生产配置收紧）**：`.env.example:30` 保留 `KB_PG_PASSWORD=kb_local_dev_only`，compose `postgres` 也以 `kb_local_dev_only` 为缺省（`docker-compose.unified.yml:128`）；`start.ps1`/`preflight.sh` 会随机化，但手工复制 `.env.example` 的路径会留下已知弱口令。建议示例改为显式占位并在启动时拒绝该值。

**P2（记录/清理）**

- **F-18 容器加固一致性**：web/worker/scheduler 无 `cap_drop`/`no-new-privileges`（office/voice-gateway 已有）；Caddy 以 root 运行（443 需要，但可评估 capability）；`https_data` 卷（本地 CA）未纳入备份/恢复策略。属纵深防御项，建议统一加固基线。
- **F-19 规则基线版本化（过程项，非项目缺陷）**：`D:/WorkSpace/_study` 的 `全栈开发规则.md` 为 v2.5.0 但未提交（上次提交仍为 `8c987ab` v2.0.0），与文件「纳入 Git 版本控制」的自述不一致。建议 owner 提交 v2.5.0 并核对两份用户级 `AGENTS.md` 锚点。
- **F-20 未记录的不适用项**：个人信息/数据出境、i18n/RTL、插件自动更新、金额/多币种、邮件外发等条款未见「已判定不适用」的判定人/理由记录（§3/§4/§5 要求记录依据）。建议在 `docs/` 增加一份适用性判定表。

---

## 七、需人类 owner 按 §7.2 确认的事项（本报告不代为确认）

1. 确认 v2.5.0 为本次审计与后续整改的规则基线。
2. 批准/驳回高风险候选（管理 MFA、数据库最小权限、回环 HTTPS、CI 变更）的技术复审结论与放行范围（附候选身份、证据、剩余风险、回滚方式）。
3. 逐项批准延期登记册（16 项 + 本报告建议新增项），指定批准人/期限；关闭 D-01、D-02；修复表格结构并更新 D-15 数量。
4. 批准 PostgreSQL 17.12 升级窗口与回滚方案；当前为测试数据，owner 已确认无需备份（回滚方式按重建/重新上传测试数据）；若升级到生产环境则必须先备份。
5. 就「管理 API 是否也要求 OTP」作出方案选择（P0-2）。
6. 批准 CSP 实施（先 Report-Only）或按 §7.1 登记偏离。
7. 批准格式整改（全仓 `ruff format` 单独提交）与「是否引入类型检查/如何登记不适用」。
8. 就字体替换/购授权、Redis/psycopg 内部使用口径作出许可决策。
9. 确认 Caddy 本地 CA 的信任分发方式与 HTTPS 变更的回滚/观察方案。

### 已记录的 owner 判定（2026-11-13）

| 时间 | 确认人 | 内容（原话/要点） | 适用条款 | 生效范围与后果 | 剩余风险与恢复条件 |
| --- | --- | --- | --- | --- | --- |
| 2026-11-13 09:51（Asia/Shanghai） | 人类 owner（会话指令） | 「都是测试数据，不用备份」 | §6 数据恢复、0.11 登记、P0-5 | 本机 Docker 测试环境（`knowledgebase` 卷内全部为测试数据）不要求备份/RPO/RTO 证据；跳过升级前备份；测试数据丢失风险由 owner 接受 | 若后续写入真实业务数据、对外开放或转入生产发布，本条自动失效，P0-5 恢复为阻断项并要求完整恢复演练。记录文件：`artifacts/audit-20261113/owner-decisions-20261113.md` |
| 2026-11-13 10:02（Asia/Shanghai） | 人类 owner（本会话选择「现在升级（推荐）」） | 同意立即把 PostgreSQL 17.11 升级到 17.12，按测试数据口径不备份 | §5 差异复审、§6 发布与恢复、附录 A-5/A-8、7.2 | 改 compose/CI 镜像摘要并重建 postgres（同一命名卷）；回滚 = 改回 17.11 摘要重建（同大版本数据目录兼容）或按测试数据重建 | 执行记录与复核证据见 `docs/remediation-2026-11-13.md`；P0-1 已闭环 |

---

## 八、未覆盖与限制（0.7 证据边界）

- 本次已重跑 PostgreSQL 全量回归（升级后：1746 用例/0 失败/0 错误/28 跳过）与 SQLite 全量（1746/0/0/51）；向 CI/发布提交前应在同一提交上再跑一次双库全量。
- 未做真实浏览器复跑、屏幕阅读器（NVDA/TalkBack）、CWV 与四档视口全量验证；前端结论来自静态代码 + 既有产物 + 本次 HTTP 响应头核验。
- 未做完整恢复演练（PG+Qdrant+原件）、未做容器 OS 包漏洞扫描（Docker Scout/Trivy）、未做跨版本/回滚演练。恢复演练在本次 owner 判定下不适用于测试数据（P0-5 降为生产发布适用）；其余两项仍为证据缺口。
- 未验证 ASVS 5.0.0 全控制矩阵（仅复核整改文档列出的 V6/V7/V8/V12/V13 条目）；未完成正式书面风险评估。
- 未启动/停止任何服务、未写入运行数据；`check --deploy`、`shell` 查询均为只读。

---

## 九、证据索引与复跑方式

证据目录：`artifacts/audit-20261113/`（清单见 `audit-manifest.md`）。

| 证据 | 复跑命令（项目根） |
| --- | --- |
| 候选身份 | `git rev-parse HEAD`；`git status --porcelain`；`git diff --stat` |
| SQLite 全量 | `$env:DJANGO_SETTINGS_MODULE='config.test_settings'; .venv\Scripts\python -m pytest -q --kb-db=sqlite --junitxml=artifacts/audit-20261113/pytest-sqlite-20261113.xml` |
| 格式/静态 | `.venv\Scripts\ruff.exe format --check apps config governance tests scripts`；`.venv\Scripts\ruff.exe check …` |
| 迁移一致性 | `$env:DJANGO_SETTINGS_MODULE='config.test_settings'; .venv\Scripts\python manage.py makemigrations --check --dry-run` |
| 部署自检 | `docker exec knowledgebase-web python manage.py check --deploy --fail-level WARNING` |
| 依赖漏洞 | OSV `POST https://api.osv.dev/v1/querybatch`（74 个 `==` 版本，输入见 `osv-batch-20261113.json`） |
| 密钥扫描 | `docker run --rm -v "$PWD:/repo" --workdir /repo ghcr.io/gitleaks/gitleaks:v8.28.0 detect --source=/repo --redact`（历史）；`--no-git` 扫各目录 |
| 数据库权限 | `docker exec -i knowledgebase-web python manage.py shell`（查询 `pg_roles`/`has_table_privilege`，见正文数据） |
| PostgreSQL 全量（升级后） | `$env:DJANGO_SETTINGS_MODULE='config.test_settings'; .venv\Scripts\python -m pytest -q --kb-db=postgres --junitxml=artifacts/audit-20261113/pytest-postgres-20261113.xml` |
| 17.12 升级证据 | `artifacts/audit-20261113/upgrade-postgres-17.12/`（image.txt、postgres-start.log、migrate.log、ready.json、db-verification.txt、deploy-check.log）；整改记录 [docs/remediation-2026-11-13.md](remediation-2026-11-13.md) |
| owner 判定 | `artifacts/audit-20261113/owner-decisions-20261113.md`（2026-11-13 09:51 测试数据免备份；10:02 同意升级） |
| 现场头部 | `curl.exe -sS -D - -o NUL --insecure https://localhost:8800/app/login/` |
| 容器/镜像 | `docker ps`；`docker inspect <name> --format '{{.Image}} {{.Config.User}}'` |

---

## 十、附录 C 交付自检表

- [x] 范围：开工目标、验收场景、0.6 触发矩阵已记录（本报告 §一/§二；候选与现场身份可复核）
- [x] 风险：高风险判定（MFA/DB/TLS/CI/迁移）与依据已记录；T1 复审发现见 §一/§六
- [x] 证据：阶段、候选标识、复跑方式与结果已留痕（`artifacts/audit-20261113/`）
- [x] 安全：鉴权、输入校验、密钥、依赖、日志脱敏逐项核对（附录 A；Gitleaks/OSV/Bandit；残留 P0-1/P0-2/F-05/F-07）
- [ ] 前端：真实浏览器主流程、目标尺寸、无障碍键盘与对比度、CWV 基线对比 —— **未完成本次实测**（引用 11-11 产物，覆盖不足，F-14）
- [x] 代码质量：职责归属、依赖方向、复用依据、公开契约测试、注释与格式已核对（F-13；格式门禁未过 P0-3）
- [x] 后端：接口契约、数据约束、事务/并发、超时重试、迁移与回滚（§四；OpenAPI/错误契约缺口 F-16）
- [x] AI：提示注入/工具越权/上下文泄露用例（既有回归；提款确认/授权路径；未见 MFA 与管理 API 组合用例）
- [x] 复核：技术复审发现、验证证据及结论已记录；**owner 确认待第 §七 节逐项作出**
- [x] 未完成/未验证项：已按 0.11 登记建议（P0-6 及新增项）
- [x] 规则版本：v2.5.0（SHA-256 见文首）
- [x] 插件、更新和启动：无插件安装；服务归属/应用就绪/失败恢复已核（`check --deploy`、ready 探针、migrate 依赖链）

---

*审计人：DSH（鲸鲸）· 审计角色为技术复核，不代替人类 owner 的 §7.2 确认。*
