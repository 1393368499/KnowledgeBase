# Office 轻量化与 Qdrant 1.16.3 选型

## Office

智库需要从 `.doc`、`.xls`、`.ppt` 提取正文与表格，也需要把套过公文模板的 DOCX 输出为 PDF。现有 Docker 镜像中，LibreOffice、中文字体和系统依赖所在层约 595 MB。新工具沿用现有 DOCX 生成逻辑，并把旧格式读取和 PDF 排版分开。现有 Python 依赖已能直接读取样本 PPTX 的表格和 XLSX 的工作表，后续接入可复用。

| 路线 | 本机验证 | 取舍 |
| --- | --- | --- |
| Apache POI Scratchpad 6.0.0 + Java docx4j 17.3.6 / Apache FOP | 合成 DOC 读出了中文正文和表格单元格。合成 XLS 保留了工作表边界与数值，合成 PPT 保留了幻灯片边界和表格。PDF 保留两页 A4、中文、表格和双页页眉页脚，标题与表格的垂直间距有差异。 | 作为首选组合。可共享一份按需启动的裁剪运行时。本机 Windows 样机的运行时和 68 个依赖包合计约 98 MB，字体与 Linux 容器基础层另计。 |
| rdocx 0.16.4 | 用同一 DOCX 输出两页 A4，中文、表格、页眉页脚均可见。默认和显式指定本机字体两次输出一致。第二页正文起始位置与现用引擎不同。 | Linux x86-64 分发包 19 MB，本机 Windows 安装约 43 MB。支持现代 DOCX，本系统仍需另配旧格式读取器。作为 PDF 备选保留。 |
| ONLYOFFICE Document Builder | 官方列出旧格式和 PDF 支持。此次只核对官方资料。 | 商用授权与无水印交付条件需另行确认。 |

POI、Java docx4j 与 FOP 主组件采用 Apache 2.0，rdocx 提供 MIT 或 Apache 2.0 双许可。正式打包时需单独核对传递依赖、保留适用的许可和声明，以及只分发已获授权的字体。[POI 版本与许可](https://poi.apache.org/download.html) · [Java docx4j 许可](https://github.com/plutext/docx4j) · [docx4j/FOP 版本](https://central.sonatype.com/artifact/org.docx4j/docx4j-export-fo) · [Apache FOP](https://xmlgraphics.apache.org/fop/) · [rdocx 版本与体积](https://pypi.org/project/rdocx/0.16.4/) · [ONLYOFFICE 授权](https://github.com/ONLYOFFICE/DocumentBuilder)

正式接入时，先让专用转换进程通过合成与脱敏模板的中文字体、页码、跨页表格和多节布局检查。旧格式读取还需覆盖图片页、坏文件、超时与恶意附件。容器隔离要验证外链无请求、没有业务数据库凭据、输入输出受限。这里的尺寸是本机原型实测，最终 Linux 镜像体积和资源占用待量测。

## Qdrant

候选 Compose 已固定官方 Qdrant 1.16.3 及镜像摘要，Python 依赖已锁定同一小版本的官方 `qdrant-client==1.16.2`，采用 Apache 2.0。保留了现有 Python 调用与权限过滤。早期客户端的嵌入模式支持中文子串，已把原来要求空结果的测试改为验证命中及无权限排除。服务器的实际全文索引行为单独验收。[官方客户端 1.16.2](https://github.com/qdrant/qdrant-client/releases/tag/v1.16.2) · [Qdrant 兼容与升级规则](https://qdrant.tech/documentation/faq/qdrant-fundamentals/)

- 以官方 1.16.3 镜像恢复隔离快照后，客户端兼容检查通过，四个集合点数为 0、0、3、1269。真实服务通过了旧向量查询、新集合建索引、中文查询、权限拒绝、增删和清理。
- 候选应用镜像 `knowledgebase:qdrant-1163-candidate` 为 `sha256:7a14ef7b8c18e3c670baade443ba60ba1ff58abd07dd42cbe54fafdb2697cc69`。镜像内版本与依赖一致性核对通过，检索相关测试 71 通过、1 跳过。
- 启用真实后台任务后的首次全量回归为 1694 通过、30 跳过、39 失败。38 项来自旧基线，另 1 项是后台测试在登记向量库时沿用了原模型端点，两个客户端版本均复现。已让父进程登记与子进程读取使用同一隔离端点，重跑 5 项真实后台测试均通过，其中入库用例确认实际请求了本地测试服务。全量数字仍来自修复测试夹具前的运行。
- 本轮只在隔离环境恢复和写入测试数据。正式替换仍由整套部署放行条件控制。

本机测试输出和合成样本的截图保存在 `artifacts/review/office-qdrant-20261109`。
