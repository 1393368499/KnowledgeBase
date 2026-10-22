# AS711 连接与功能缺口复查（2026-10-16 08:29，上海时间）

## 最新结论

AS711 已恢复连接。Windows 宿主和系统 web 容器都能连接四个模型端口，并通过真实模型调用验证嵌入、重排、大模型和清晰样图 OCR。前一份独立报告的“端口超时”是当时的检查结果，本次已更新为可连接、可推理。

产品代码和部署镜像与上一轮独立检查相同，因此上轮复现的整库重建续做、重排异常降级提示问题尚未修复。本轮另确认归档资料重传不能恢复，以及容器内 OCR 自检样图质量问题。

此次检查没有修改产品代码、启停 AS711 服务或向业务库新增资料。

## 服务器与真实模型调用

| 服务 | 端口 | 本次实际调用结果 |
|---|---:|---|
| 大模型 Qwen3.6-35B-A3B-FP8 | 10001 | 真实生产 `LlmClient` 回答 7+5，正文为 12，正常结束，14.36 秒 |
| 嵌入 bge-large-zh-v1.5 | 8000 | 真实嵌入返回 1024 维，符合当前索引配置 |
| 重排 bge-reranker-large | 8002 | 将包含质保期的候选排在首位，分数 0.97265625 |
| OCR PaddleOCR-VL-1.5 | 8003 | 真实生产 `OcrClient` 正确识别三行中英文合成样图，0.71 秒 |

证据：

- `artifacts/acceptance/as711-connectivity-recheck-20261016.json`
- `artifacts/acceptance/as711-real-probe-20261016-0825.json`
- `artifacts/acceptance/as711-llm-real-recheck-20261016-0825.json`
- `artifacts/acceptance/as711-ocr-large-font-recheck-20261016.json`

注意：第二份文件中的 OCR 自检为失败，不能隐去。Docker 镜像中命令尝试的 Windows 字体和 DejaVu 字体均不存在，回退字体的整行英文边界仅约 138×9 像素；生成的 1100×360 样图只有 4922 字节，模型没有识别出预期文字，反而输出了无关的重复内容。使用 Windows 微软雅黑 48 号字体生成的 1200×420 清晰样图，保持生产 OCR 客户端不变，立即识别成功。字体回退问题需要修复；实际 OCR 服务不能因这个自检样本失败而判为不可用。

为诊断异常，查阅了 [PaddlePaddle 官方模型说明](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.5/blob/main/README.md)，其示例提供按 OCR、表格等任务选择提示词的方式。本轮未更改生产提示词、模型配置或服务参数。

## 目前没有提供的功能

| 项目 | 当前代码实际提供什么 | 判断依据 |
|---|---|---|
| AS711 目标模型启动与推理路线切换 | 四个相关按钮仍禁用；32 个操作意图中没有启动/切换模型管理意图 | `apps/webui/static/webui/kb-pages.js` 615–641 行；`apps/planner/operations.py` |
| 文件页的整库数据库重置 | “重置数据库”只显示说明；实际替代操作为受控删除、重建索引 | `apps/webui/static/webui/kb-files.js` 574 行起 |
| 归档资料恢复/反归档 | 归档是终态，未提供恢复操作 | `apps/knowledge/models.py` 状态机与操作注册表 |
| Word/PDF 派生文档导出 | 当前仅提供 Markdown、HTML 下载 | `apps/planner/services.py` 的 `export_document`，`supported` 仅 md/html |
| 旧 Word、旧 Excel、PowerPoint 原生解析 | `.doc/.xls/.ppt/.pptx` 要先转换后上传；当前不做格式转换 | `apps/knowledge/parsers.py` 的 `parse_bytes` |
| 跨 web/worker 全局熔断重置 | 只重置处理此次请求的 Python 进程中的熔断器 | `apps/planner/services.py` 的 `reset_breaker` 与 `apps/knowledge/metrics.py` 的 `scope=process` |

上表中导出格式、旧 Office 格式、整库重置是实际能力边界，不表示用户一定要求实现所有扩展格式或无条件清库。若交付标准仍是“界面上每个操作均可执行”，禁用按钮与说明入口不能标为已完成。

“学习”当前是实时检索反馈加权和规则报告，不是模型训练；反馈加权本身已有实现，不能因没有训练而说整个反馈功能缺失。

## 已实现但存在缺陷的项目

1. **整库重建续做**：默认每次最多 50 份，超过上限后仍从第一份开始；上一轮 51 份合成资料两次执行都没有处理到第 51 份。当前 `services.py` 哈希与复现时、部署容器均相同。
2. **重排异常的降级提示**：格式错误等异常后退回普通排序，但 `degraded=false`，页面不提示。当前 `pipeline.py` 与上轮复现时、部署容器相同。
3. **归档资料重传恢复承诺不成立**：说明称“要重新启用请重新上传原件”，本轮隔离数据上真实 multipart 重传返回 HTTP 200、`created=false`、`duplicate_content`，仍是同一条 `archived` 记录，`queued=false`，没有恢复检索。证据：`artifacts/acceptance/independent-archive-reupload-20261016.json`。测试原件与嵌入式向量目录自动清理，没有写入生产资料库。
4. **OCR 自检样图在容器中失真**：见上方真实模型检查。需要给自检提供可读字体或固定、已验证的样图，不能只看端口或 HTTP 200。

## 已有实现，但本次不等于完整验收的项目

上传、原件保存、解析/分段、向量写入、普通搜索、AI 检索上下文、AI 会话、指定来源写文档、PDF/图片/文本/Office 预览、删资料/删库、反馈、巡检、索引代次切换/回退均已有代码和专项测试。

当前生产库仍为 3 个信息库、0 份资料。本次验证的是四类模型真实调用，以及一项隔离的归档重传诊断，没有把上传到下载的全部业务操作逐个重做。真实模型单次调用成功不能替代全流程、并发、重启持久性与故障恢复验收。

同一代码版本上一轮独立完整回归为：SQLite 765 通过 /24 跳过；PostgreSQL 771 通过 /18 跳过；失败、错误均为零。本次未重复运行未改变的整套回归。

当前镜像仍是 `sha256:1a943e6b1abacea626ed01d7a49b54738e520805a943cad5f2a4b01d756a89d6`。四份关键文件的宿主与部署容器 SHA256 一致：`services.py`、`pipeline.py`、`kb-pages.js`、`kb-files.js`。

本报告结论是“模型已接通，仍有明确功能缺口和缺陷”，不是“所有功能已交付”。
