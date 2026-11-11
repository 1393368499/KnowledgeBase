# 智库全栈复审与 Docker 放行记录

日期 2026-11-09，上海时区。基线为 Git `e77cbd9a9a63754a0547aadc3ccefe9ee6ebb7f3` 与当时运行的 `knowledgebase` 容器。候选范围包括现有工作区中会进入 Docker 的 Web、后台任务、检索与语音改动，以及本次修复。Android 安装包和 Note4 固件实机升级属于另一个发布面。

## 放行状态

正式环境仍运行原容器，Web 就绪检查、迁移检查和 PostgreSQL 六个不可篡改触发器均通过。候选未获放行。Debian 13 中由 LibreOffice 带入的 libxml2 与 Raptor 等依赖仍有高危公告，稳定源未提供对应修复，旧 Office 和模板转 PDF 两条路径尚缺足以排除触达的证据。全量回归还有 38 个可对照的历史失败。

最终候选为 `knowledgebase:review-system-patches-20261109-final`，镜像 ID 为 `sha256:a148d1fb7c7745cfedec4484b69b43cf46dba6fbdf152298e84fc2ce63ef92dc`。实测镜像内 APT 为 3.0.4，zlib 为 Debian 修复版 `1:1.3.dfsg+really1.3.1-1+deb13u1`，依赖一致性、Django 自检和 33 项权限与检索专项通过。

## 已确认并复测的应用问题

| 严重度 | 入口与前提 | 影响、修复及复测 |
| --- | --- | --- |
| 中 | `apps/planner/rag/reader.py:75` 的清单统计与 `apps/planner/rag/tool_evidence.py:31` 的读取鉴权。用户曾访问两个库，随后失去只出现在后页的库权限。 | 旧检查只能覆盖当前页，原引用仍返回包含被撤权库的统计。清单统计现在记录贡献库 ID，引用、操作列表、会话操作列表和进度接口读取结果时复核。回归用例确认撤权前总数可见，撤权后引用返回 404，列表与进度隐藏结果。无法证明贡献范围的旧统计引用需重新生成。 |
| 中 | `apps/voice/task_actions.py:40` 的任务删除。攻击者持有自己的设备凭据，知道同账号针对该任务的网页或另一设备的删除操作 ID。 | 旧实现允许从设备入口放弃这些待办。现按发起设备绑定准备、确认、放弃和确认重试。回归确认网页与另一设备的待办保持原状态、设备自身可确认或放弃、重复确认保持幂等。更新前未完成且缺少设备绑定的确认需从设备重新发起。 |
| 高至严重 | `requirements.txt:58`、`requirements.txt:66`、`requirements.txt:79` 和 `deploy/app/Dockerfile:94`。旧版 pypdf 处理用户上传的 PDF，Pillow 及证书库也落在公告范围。 | 已锁定 pypdf 6.21.4、Pillow 13.0.0 和 certifi 2026.10.18。两条构建路径均按同一份依赖清单安装并运行 `pip check`。候选的 Python 依赖复扫返回 0 条已知漏洞。 |

本次查证的上游发布与许可为 [pypdf 6.21.4](https://github.com/py-pdf/pypdf/releases/tag/6.21.4) 的 BSD 3-Clause、[Pillow 13.0.0](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html) 的 MIT-CMU 和 [certifi 安全公告](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2) 对应的 MPL 2.0。现有 Python 3.13 运行路径保留。整镜像扫描另列出安装工具内置的 Python 包，需与应用直接加载的包区分。

## 基础设施与发布阻断项

- **已确认高危。** 现用 Qdrant 1.15.5 在隔离复现中接受 `POST /logger` 指定的任意文件路径并写入临时文件。官方 1.16.3 镜像拒绝该路径，现有快照在该镜像恢复后四个集合精确点数为 0、3、1269、0，真实向量集成用例通过。应用的 Qdrant Python 客户端 1.19.1 仍报告版本差距超出其兼容范围，故这一步作为恢复及漏洞复测证据。生产升级须沿官方小版本路径走到受支持组合，再复测写入、搜索与备份恢复。[Qdrant 公告](https://github.com/qdrant/qdrant/security/advisories/GHSA-f632-vm87-2m2f) · [快照恢复](https://qdrant.tech/documentation/operations/snapshots/)
- **高危告警，触达验证待闭环。** Docker Scout 命中 libxml2 CVE-2026-86140、Raptor CVE-2025-71436 等。Debian 安全记录确认当前稳定发行版的受影响版本，软件源检查也未发现修复候选。要满足放行规则，需更新受影响的解析运行环境，或验证现有输入与转换隔离能够切断触发路径。APT 与 zlib 已有稳定源修复，本次构建配置已明确纳入升级。[libxml2 公告](https://security-tracker.debian.org/tracker/CVE-2026-86140) · [Raptor 公告](https://security-tracker.debian.org/tracker/CVE-2025-71436) · [zlib 公告](https://security-tracker.debian.org/tracker/DSA-6613-1)
- PostgreSQL 当前为 17.6，官方 17 系列后续更新包含安全修复。Redis 为 7.4.11，7.4.12 公告所列缺陷涉及集群总线，实测本地 Redis 的集群模式关闭。Qdrant 公告中的内部 gRPC 集群路径也不适用当前单节点部署。[PostgreSQL 17 安全公告](https://www.postgresql.org/support/security/17/) · [Redis 发布记录](https://github.com/redis/redis/releases) · [Qdrant 集群公告](https://github.com/qdrant/qdrant/security/advisories/GHSA-frc7-44gm-99qq)

## 验收与恢复证据

- PostgreSQL 全量回归在补齐测试夹具后为 1683 通过、42 跳过、38 失败。38 个失败全部命中项目已有的基线清单，主要涉及旧确认协议、已停用归档与旧版文档测试。最后一轮补查了操作列表和进度入口，249 项受影响路径专项通过。已构建的 Python 补丁镜像完成 275 项容器专项测试，涵盖检索、语音、文件预览及 LibreOffice 转换。
- 浏览器以 1440、390、320 CSS 像素宽检查登录和常用页面，五个常用路由均返回 200，页面未出现横向溢出或 JavaScript 异常。真实浏览器使用合成资料走通登录、键盘导航与焦点返回、上传、文件名搜索和无结果反馈。运行态验证了 CSRF、语音网关路径白名单和设备令牌与网页接口的隔离。设计扫描有颜色、字体和字号告警，未完成屏幕阅读器、真实硬件或完整 WCAG 验收。
- 已有模型服务的嵌入、重排和 OCR 只读探测成功。本次没有在生产库写入合成业务数据，未重新验收真实模型的复杂问答质量。
- PostgreSQL 自定义格式备份已恢复到隔离数据库，包含 54 条迁移记录。Qdrant 全库快照已恢复到隔离实例并校验四个集合的精确点数。备份位于本地忽略目录 `snapshots/kb-review-20261109`。复测与扫描输出位于 `artifacts/review/fullstack-20261109`。
