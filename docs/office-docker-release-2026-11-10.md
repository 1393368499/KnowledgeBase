# 智库 Office 与向量服务发布验收

验收日期 2026-11-10，上海时区。项目根 Compose 同时加载现用语音配置。先在隔离环境验证制品、备份和回退，再替换当前容器。

| 制品 | 已验收镜像 ID |
| --- | --- |
| 应用 | `sha256:f4412ae92c757b2614435a43ebd271b727f620d6e1e2853a9f408d0d68b04044` |
| Office | `sha256:f0ffdfdfa9ddfc10375829858244bebcbce56fcedeae84f372bc5871747d26f5` |
| Qdrant | `sha256:c7a659335e726707e7ea4527f67203fa70cc370e608b2ff0084e66ac944fc727` |

## Office

- Docker 应用镜像移除 LibreOffice。DOC、XLS、PPT、PPTX 由 Apache POI 6.0.0 提取，PDF 使用 docx4j 17.3.6 与 FOP。两个入口复用既有业务权限与文件处理。宿主开发仍可使用本地 LibreOffice。应用镜像由约 1.48 GB 降到 816 MB，Office 独立镜像约 594 MB。裁剪后的 Java 与 JAR 共约 117 MiB，基础层与应用镜像共享。
- 最终容器实测四种格式的中文、表格与分段，合成 DOCX 的两页 PDF 保留了中文、表格、页眉和动态页码。智库正常导出入口也生成了可读的中文 PDF。复杂模板的排版可能与 LibreOffice 有差异。
- 转换容器无网络，以普通用户运行，只读根文件系统，挂载仅含本地套接字和只读字体。无业务数据目录或数据库凭据。实测六类不安全附件被拒绝后套接字仍可用，过期任务的 Java 进程组会被终止。上线前检查了 CPU、内存、进程数、文件大小、解包体积和转换时限。
- 72 个 Maven 运行包均核对 SHA-256，OSV 批量核对无已知报告。Apache POI、docx4j 和 FOP 采用 Apache 2.0，Java 运行时保留内附的许可与例外条款，宿主字体在本机只读挂载。Docker Scout 的 Java 数据库下载中断，因此 Java 包审查以版本锁和 OSV 记录为准。[Apache POI 安全指南](https://poi.apache.org/security.html) · [docx4j](https://github.com/plutext/docx4j) · [Apache FOP](https://xmlgraphics.apache.org/fop/)
- 应用基础层扫描仍提示 Expat 两项高危。属性计数溢出要求数 GB 的单个 XML 起始标签，XLSX 入库和预览均把解包总量限制为 200 MiB，Office 服务把 XML 单条目限制为 32 MiB。另一个告警要求创建外部实体子解析器，Office 校验直接拒绝 DTD 和外部实体，XLSX 工作表使用默认 ElementTree 路径，不主动解析外部实体。[Debian CVE-2026-118370](https://security-tracker.debian.org/tracker/CVE-2026-118370) · [Debian CVE-2026-117610](https://security-tracker.debian.org/tracker/CVE-2026-117610)

## Qdrant

- 保留官方 Qdrant 1.16.3 二进制，基于固定镜像摘要更新 Debian 安全包，移除智库未使用的独立 Qdrant Dashboard。Python 客户端固定为 1.16.2。衍生镜像扫描从 13 项严重和 114 项高危降到 0 项严重和 13 项高危，剩余条目来自上游 Rust 依赖。
- Qdrant 仅接入 Docker 内部向量网络。验证宿主无端口绑定、容器无外网路由，关闭集群、遥测、CORS 和服务 TLS。单节点无 6335 监听。应用通过固定服务地址使用正常向量和索引接口，不向上传或检索用户提供原始 Qdrant API、快照导入或 S3 配置。运维恢复使用核验后的本地快照。保留的 Rust 版本告警约束了部署用途，扩大连接对象或开放管理 API 时需重新复审。[Qdrant 集群安全公告](https://github.com/qdrant/qdrant/security/advisories/GHSA-frc7-44gm-99qq) · [Qdrant 发布记录](https://github.com/qdrant/qdrant/releases) · [LZ4 解压公告](https://rustsec.org/advisories/RUSTSEC-2026-0041.html)
- PostgreSQL 备份恢复后核对 54 条迁移记录，Django 自检、迁移检查和六个不可篡改触发器通过。Qdrant 全量快照恢复后四个集合的精确点数为 1269、3、0、0，实测中文全文、权限过滤及临时集合读写删除。最新已记录的全量运行中 1673 项通过、64 项跳过、38 项失败，失败清单与旧基线一致。最新 Office、预览和导出专项另行通过。

本机证据在 `artifacts/review/office-qdrant-20261109/local`。切换前 PostgreSQL 备份、Qdrant 快照、旧镜像映射和回退编排在 `snapshots/kb-office-release-20261109`。旧向量卷的冷备副本使用 `knowledgebase-qdrantdata-rollback-20261110`。

## 正式切换结果

2026-11-10 11:23 核验正式容器。Web、Worker、Scheduler、Office 和 Qdrant 就绪，语音 Worker 在运行，语音网关访问 Web 就绪入口返回 200。PostgreSQL 与 Redis 复用原容器，迁移服务确认无待应用迁移、六个数据库触发器在位并退出 0。正式 Web 使用 `qdrant-client==1.16.2` 连接 Qdrant 1.16.3，四个集合精确计数与切换前一致。正式应用走套接字生成了可提取中文的 PDF，Web 和 Worker 均不存在 `soffice`。

向量冷备逐文件核验 1020 个 SHA-256。上线前 PostgreSQL 备份 `postgres-cutover.dump` 的 SHA-256 为 `65418ff91c4dd93061e77a3a8130cc39da25e83f5401dd87cdc80b392fba58b9`。正式镜像和容器映射记录在 `after-containers.txt`，回退镜像和备份保留。若上线后已有新向量写入，回退前先保全新卷并核对与 PostgreSQL 的差异。
