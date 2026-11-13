> 注：2026-11-13 同日完成 Python 3.15 适配与根目录重构（`config/`→`knowledgebase/`、运行数据移入 `data/`），本记录中的文件路径/行号按升级当时（重构前）状态保留；当前路径见 [python315-and-layout-migration-2026-11-13.md](python315-and-layout-migration-2026-11-13.md)。
# PostgreSQL 17.12 安全补丁升级记录（2026-11-13）

按《全栈开发规则》v2.5.0 第 0.8/5/6 节记录本次高风险依赖升级；owner 确认见 `artifacts/audit-20261113/owner-decisions-20261113.md`。

## 1. 范围与确认

- **触发**：2026-11-13 合规审计 P0-1——正式库 PostgreSQL 17.11 早于 17.12（2026-11-12 发布，修复 52 个 CVE，含多个 CVSS 8.8 任意代码执行）。[发布公告](https://www.postgresql.org/about/news/postgresql-187-1712-1616-1520-1425-and-19-rc-2-released-3442/) · [CVE-2026-120139](https://www.postgresql.org/support/security/CVE-2026-120139/)
- **owner 确认**：2026-11-13 09:51「都是测试数据，不用备份」；10:02 选择「现在升级（推荐）」。确认范围：compose/CI 镜像摘要、重建 postgres（同一命名卷）、复跑迁移/触发器/角色/回归；不包含应用代码改动。
- **候选基线**：Git HEAD `e9f92f6` + 工作树整改；仅基础设施镜像与文档变更。

## 2. 变更

| 文件 | 变更 |
| --- | --- |
| `deploy/docker-compose.unified.yml:117` | `postgres:17.11-alpine@sha256:e5d2edd…00d0` → `postgres:17.12-alpine@sha256:34261f594522cf41dfcb76aefd42b9d201098b0e6b23c9c68733f169e4218b6f` |
| `.github/workflows/ci.yml:60` | CI Postgres service 同步固定到 17.12 同一摘要 |
| `docs/deployment-and-operations.md:24` | 组件版本表 `PostgreSQL 17.6` → `PostgreSQL 17.12` |

应用镜像（`knowledgebase:app` `9ceccc…`）未重建；无代码、迁移或数据结构变更，使用既有命名卷 `knowledgebase-pgdata` 原地升级（同大版本，无需 `pg_upgrade`/dump-restore）。

## 3. 执行与验证（2026-11-13 10:05–10:16）

```text
docker compose config -q                                  # exit 0
docker compose up -d --wait --wait-timeout 300            # exit 0
  knowledgebase-postgres  Recreated → Healthy
  knowledgebase-migrate   Started → Exited(0)
  knowledgebase-web       Recreated → Healthy
  其余服务保持 Running/Healthy
```

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 运行镜像 | `public.ecr.aws/docker/library/postgres:17.12-alpine@sha256:34261f59…8b6f`，容器内镜像 ID 与 pin 一致 | `upgrade-postgres-17.12/image.txt`、`final-image.txt` |
| 数据库版本 | `PostgreSQL 17.12 on x86_64-pc-linux-musl` | `postgres-version.txt`、`postgres-start.log` |
| 迁移 | `No migrations to apply`（57 条迁移在位）；`guards --ensure` 六个触发器全部就位；`provision_runtime_db` → runtime role ready | `migrate.log` |
| 运行身份 | `kb_runtime`，`rolsuper/createdb/createrole` 全 false；权限面与升级前一致 | `db-verification.txt` |
| 就绪 | `/api/v1/health/ready/` 200，`database/migrations/vector_data=ok` | `ready.json` |
| 部署自检 | `check --deploy --fail-level WARNING` 0 issues | `deploy-check.log` |
| 公告后置动作 | 库内无 GIN/GiST 索引、无 `pg_trgm` 扩展（仅 `plpgsql`），17.12 公告中的 REINDEX 建议不适用 | `db-verification.txt` |
| 容器 | 12 个 knowledgebase 容器全部 Running/Healthy；voice-worker 短暂 `OperationalError` 后自动重连，最近 5 分钟无新错误 | `compose-ps-after.txt`、`voice-worker-errors.txt` |
| 回归（PostgreSQL） | **1746 用例，0 失败/0 错误，28 跳过**（310.7s） | `pytest-postgres-20261113.xml` / `.log` |
| 回归（SQLite） | **1746 用例，0 失败/0 错误，51 跳过**（241.6s） | `pytest-sqlite-20261113-post-upgrade.xml` / `.log` |

## 4. 回滚与剩余风险

- **回滚**：把两处镜像摘要改回 `postgres:17.11-alpine@sha256:e5d2edd…` 后 `docker compose up -d postgres`；17 系列同一主版本数据目录兼容。测试数据按 owner 判定可丢弃重建（不备份）。
- **剩余风险**：数据库所有者 `kb` 仍为超级用户（审计报告 F-10）；17.x 后续安全公告需持续跟进（建议纳入发布前检查，订阅 postgresql.org 安全公告）。
- **关联未闭环项**：苹果审计报告 P0-2（管理 API 单因子）、P0-3（6.1 检查缺口）、P0-4（构建记录/候选边界）、P0-6（延期登记册）、P0-7（许可）；P0-5 仅在真实数据/生产发布时恢复。

## 5. 证据与引用

- 证据目录：`artifacts/audit-20261113/upgrade-postgres-17.12/`
- 审计报告：[docs/fullstack-compliance-audit-2026-11-13.md](fullstack-compliance-audit-2026-11-13.md)
- owner 判定：[artifacts/audit-20261113/owner-decisions-20261113.md](../artifacts/audit-20261113/owner-decisions-20261113.md)
- 历史整改记录（17.11 阶段）：[docs/security-remediation-2026-11-12.md](security-remediation-2026-11-12.md)（该记录的 17.11 版本已被本次升级取代）
