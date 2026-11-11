# 备份、恢复与 RPO/RTO 口径

2026-11-11 制定。适用于 Docker 部署的智库实例（PostgreSQL + Qdrant 命名卷 + `media/` 原件）。

## 1. 目标

| 指标 | 取值 | 含义 | 依据 |
| --- | --- | --- | --- |
| **RPO** | ≤ 24 小时 | 可接受丢失的业务数据窗口。每日 02:00 自动 `pg_dump`（自定义格式）+ Qdrant 快照 + 原件清单哈希；人工在重大变更前额外备份。 | 业务为内部资料库，单日新增可控；原件本身不可变（内容寻址），丢失可重新上传。 |
| **RTO** | ≤ 4 小时 | 从确认故障到恢复服务并完成冒烟的时间预算。含：定位备份、恢复 PG、恢复 Qdrant 集合、恢复原件目录、启动容器、冒烟与权限核对。 | 2026-11-11 恢复演练实测：854 KB 级 PG 备份恢复到空库 0.84 秒；全量恢复瓶颈在 Qdrant 集合重建与原件体积，按现有约 18 MB 快照估算远小于 4 小时。 |

RPO/RTO 由项目负责人批准；调整需记录变更日期与批准人。

## 2. 备份对象与频率

| 对象 | 方式 | 频率 | 保留 | 存放 |
| --- | --- | --- | --- | --- |
| PostgreSQL | `pg_dump -Fc`（自定义格式，含 54 条迁移与 6 个不可篡改触发器定义） | 每日 02:00 + 变更前 | 最近 14 份 | `snapshots/kb-*`（本地，不入 Git） |
| Qdrant | 官方快照 API 全量快照（4 个集合） | 每周日 + 变更前 | 最近 4 份 | `snapshots/kb-*` |
| 原件 `media/raw` | 内容寻址、只读；记录逐文件 SHA-256 清单 | 变更前记录清单 | 随备份保留 | `snapshots/kb-*` |
| 向量冷备卷 | Docker 卷冷备副本（逐文件 SHA-256 核验） | 切换/升级前 | 最近 1 份 | `knowledgebase-qdrantdata-rollback-*` |

## 3. 恢复步骤（演练与实战同一套）

1. 停止写入：`docker compose stop web worker scheduler voice-worker`。
2. 恢复 PostgreSQL：`pg_restore -U kb -d <空库> --no-owner --no-privileges <dump>`；核对 `django_migrations` 条数与触发器 `kb_guard_*` 数量。
3. 恢复 Qdrant：按官方快照恢复接口恢复到隔离实例，核对四个集合点数与 payload 抽样。
4. 恢复原件：按 `media/raw` 清单核验 SHA-256，缺文件按内容重新上传/从冷备拷回。
5. 启动服务：`docker compose up -d --wait`；检查 `/api/v1/health/ready/`、登录、文件列表、检索、预览。
6. 记录：恢复耗时（分阶段）、丢失窗口（最后一次备份到故障时刻）、完整性核对结果，归档到 `snapshots/`。

## 4. 2026-11-11 恢复演练记录

- 恢复制动件：`snapshots/kb-office-release-20261109/postgres-cutover.dump`，854,832 字节，SHA-256 `65418FF91C4DD93061E77A3A8130CC39DA25E83F5401DD87CDC80B392FBA58B9`（与 `docs/office-docker-release-2026-11-10.md` 记录一致）。
- 目标：新建空库 `kb_restore_rehearsal_20261111`，`pg_restore --no-owner --no-privileges`。
- 结果：退出码 0，**耗时 0.84 秒**；完整性核对（恢复库 / 在线库）：迁移 54/54、信息库 4/4、资料 50/50、分段 1272/1272、操作记录 167/167、审计事件 1572/1573（差 1 为备份后新增，符合预期）、不可篡改触发器 6/6。
- 演练后目标库已删除。原始输出：`artifacts/review/fullstack-rules-20261111/logs/restore-rehearsal.txt`。
- 未覆盖：Qdrant 快照恢复耗时（本次未重做，历史证据见 `docs/office-docker-release-2026-11-10.md`：四个集合点数 1269/3/0/0、中文全文与权限过滤实测通过）；原件目录全量 SHA 核验仅抽样。

## 5. 待补齐

- 把每日备份做成受管定时任务（宿主计划任务或独立备份容器），并把最近一次备份的时间/大小/哈希写入监控；
- Qdrant 恢复耗时与原件清单全量核验纳入下一次演练。
