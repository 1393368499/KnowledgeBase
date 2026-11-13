# 用户九项要求的证据索引（2026-10-16）

本文件只做**索引**：每项要求 → 实现位置 → 可复现的验证命令 → 证据文件。
不替代实现，也不把"未验证"写成"已验证"。

## ① 删除模型启停/切换控件与「重置数据库」
- 实现：`apps/webui/static/webui/kb-pages.js`（删除四个控件与指引，保留"推理位置＝远程接口（AS711）"与四个「测试连通」）、`apps/webui/templates/webui/files.html` + `apps/webui/static/webui/kb-files.js`（删除「重置数据库」按钮/事件/说明；保留上传、扫描目录、全部解析、逐份删除，以及信息库页「重置索引」「删除信息库」）。
- 验证：`python artifacts/acceptance/verify-removed-and-restored-entries.py 8800`
- 证据：`artifacts/acceptance/removed-and-retained-entries.json`（16/16，含真实浏览器登录）

## ② >50 份整库重建真正续做
- 实现：`apps/planner/models.py::LibraryRebuildRun`（含 `owner_token/version/lease_expires_at/manifest`，迁移 0004/0005）、`apps/planner/services.py::_reset_library_index`（持久游标、逐份 CAS 落进度与续租、在飞优先、范围指纹、退役状态清理）。
- 验证：`pytest tests/test_reset_library_continuation.py tests/test_reset_library_index.py`
- 真实 PG 并发：`pytest tests/test_reset_library_continuation.py tests/test_cleanup_activation_lock_pg.py --kb-db=postgres` → **18 passed**（线程+事件同步；真实 `activate_generation` 在清理持锁期阻塞后拒绝；真实 `rollback_generation` 拒绝；drop 失败保留登记可重试）。

## ③ 重排失败统一降级
- 实现：`apps/knowledge/pipeline.py`（任何重排异常置 `degraded=True` 并给出原因，保留 RRF 结果）。
- 验证：`pytest tests/test_rerank_degradation.py`（含反向验证）。

## ④ 归档恢复
- 实现：`apps/planner/operations.py`(`unarchive_file`, `library:admin`+1 次确认)、`apps/planner/services.py::_unarchive_file`、`apps/knowledge/pipeline.py`（`retry_file/process_file/acquire_lease` 默认拒绝 ARCHIVED，仅受控入口传内部能力 `allow_archived=True`）、`apps/knowledge/views.py`（同内容重传如实返回 `restore_required`）、`apps/knowledge/models.py`（`ARCHIVED→RUNNING`）、归档时归零 `embedded_chunk_count/vector_status`（否则恢复会"0 点却报成功"）。
- 验证：`pytest tests/test_unarchive_file.py`（12 passed：往返重建向量、幂等、失败如实、四条越权负例、重传不静默）

## ⑤ 派生文档真实 DOCX/PDF 导出
- 实现：`apps/planner/document_export.py`（自写 OOXML DOCX＋LibreOffice 转 PDF；中文/表格/列表/段落；MIME 与中文文件名）、接线 `apps/planner/services.py::export_document` 与 `apps/planner/views.py::DerivedDocumentDownloadView`、前端按钮 `apps/webui/templates/webui/assistant.html` + `kb-assistant.js`。
- 验证：`pytest tests/test_document_export.py tests/test_export_document_real.py`；容器内 HTTP 实测四种格式（md/html/docx/pdf 200 + 正确 MIME/魔数，`rtf` → 400）。
- 证据：`artifacts/acceptance/final-ui-export.docx`、`final-ui-export.pdf`；渲染核验图 `artifacts/acceptance/render/final-ui-docx-page1.png`（标题只出现一次、表格占满正文宽度、"12 个月"未被折行；该 PNG 与 PDF 渲染逐字节相同，说明二者同源）。

## ⑥ .doc/.xls/.ppt/.pptx 安全解析
- 实现：`apps/knowledge/office_convert.py`（容器结构辨别防伪成功、隔离 profile 禁宏/禁活动内容/禁外链、超时杀**整棵进程树**、临时目录四路径清理、大小/产物/路径约束）、`apps/knowledge/parsers.py` 分派。
- 验证：`pytest tests/test_legacy_office_parsing.py`（宿主 95 passed/13 skipped；Linux 容器 108 全通过，含真实转换与外链零请求）。
- 镜像依赖：`libreoffice-writer/calc/impress/core`、`fonts-noto-cjk`、`fonts-dejavu-core`、`python-pptx==1.0.2`、`psutil==7.2.2`（`deploy/app/Dockerfile`）。

## ⑦ 熔断状态跨进程一致
- 实现：`apps/knowledge/breaker_store.py`（Redis 原子 Lua、索引 TTL 只延长、SCAN 孤儿发现、拒绝 DB0）、`apps/knowledge/resilience.py`、`apps/knowledge/metrics.py`、`apps/planner/services.py::reset_breaker`→`reset_all_breakers()`。
- 验证：`pytest tests/test_global_breaker_state.py`（独占 Redis 时 18 passed，含真跨进程）；`knowledgebase/test_settings.py` 强制 `KB_BREAKER_STORE=process`，离线回归不再污染共享状态（实测跑 `test_model_clients.py` 后 DB14 `kb:breaker*` 为空）。

## ⑧ OCR 自检与模型探测
- 实现：`apps/knowledge/ocr_selfcheck.py` + `apps/knowledge/data/ocr_selfcheck.png`（固定样图进镜像）、`kbprobe` 使用固定样图与关键词判定、`apps/knowledge/views.py` 四个探测走**真实客户端**（`RerankClient/OcrClient/LlmClient`，带鉴权与 `trust_env=False`）、`deploy/app/Dockerfile` 构建期硬自检（`soffice --version`＋CJK 字体＋PIL 墨迹，无 `|| true`）。
- 证据：`artifacts/acceptance/kbprobe-after-ocr-sample-fix.json`（embedding 1024 / rerank 0.97265625 / OCR 4/4）；探测契约测试 28 passed。

## ⑨ 隔离环境、模型联调、可视验收、界面回归
- SQLite 全量：**985 passed / 51 skipped / 0 failed**（`artifacts/acceptance/pytest-freeze4.log`、`junit-freeze4.xml`）。
- 真实 PG：并发专项 18 passed；业务库保持 `3 库 / 0 文件 / 0 分段 / 0 反馈`。
- 模型联调：`kbprobe --probe` 四路真实就绪（见 ⑧ 证据）。
- 界面回归：①的 16/16 + 助手页导出按钮/无注释泄漏（`artifacts/acceptance/assistant-after-text-fix.png`、`home-welcome-final.png`）。
- 隔离与清理：测试独占 DB/目录/队列/向量前缀并自动回收；Redis DB0 `celery` 长度始终 **193**（未消费）。

## 九项 → 用例数（冻结树上一次跑完，2026-10-16 10:12）
命令：
```
$env:KB_PYTEST_DB='sqlite'
python -m pytest tests/test_reset_library_continuation.py tests/test_reset_library_index.py \
  tests/test_rerank_degradation.py tests/test_unarchive_file.py tests/test_document_export.py \
  tests/test_export_document_real.py tests/test_legacy_office_parsing.py \
  tests/test_global_breaker_state.py tests/test_model_probe_targets.py tests/test_llm_probe_target.py \
  tests/test_search_dedupe.py tests/test_worker_settings_guard.py \
  --junit-xml=artifacts/acceptance/junit-nine-items.xml -o addopts=""
```
结果：**252 passed / 23 skipped / 0 failed / 0 error**（`artifacts/acceptance/junit-nine-items.xml`）

| 要求 | 用例文件 | 用例数 |
|---|---|---|
| ② 整库重建续做 | `tests/test_reset_library_continuation.py`（含 `test_real_pipeline_51_files_two_batches_all_indexed` 真实流水线专项） | 15 |
| ② 旧口径回归 | `tests/test_reset_library_index.py` | 17 |
| ③ 重排降级 | `tests/test_rerank_degradation.py` | 3 |
| ④ 归档恢复 | `tests/test_unarchive_file.py` | 12 |
| ⑤ 导出（模块级） | `tests/test_document_export.py` | 70 |
| ⑤ 导出（服务/意图级） | `tests/test_export_document_real.py` | 6 |
| ⑥ 旧 Office 安全解析 | `tests/test_legacy_office_parsing.py`（含"源字节只读"：`payload == original`） | 108 |
| ⑦ 跨进程熔断 | `tests/test_global_breaker_state.py` | 18 |
| ⑧ 探测契约 | `tests/test_model_probe_targets.py` + `tests/test_llm_probe_target.py` | 7 + 8 |
| ⑨ 搜索去重（新缺陷） | `tests/test_search_dedupe.py` | 8 |
| ⑨ worker 设置保护（新增） | `tests/test_worker_settings_guard.py` | 3 |

补充的**真实运行栈**证据：跨进程熔断与全局重置见
`artifacts/acceptance/independent-cross-process-breaker-20261016.json`（web 写入 → worker 立即可见 →
受控 `reset_breaker` → worker 不可见 → Redis 无残留）。

## 已知未闭合 / 需授权处置
1. 宿主只有单一 Qdrant 实例，真实 worker 用例在该环境**被安全闸门拒绝启动**（`KB_WORKER_QDRANT_URL` 指向主实例）→ 需在独占网络（独立 qdrant）复跑。
2. Qdrant 主实例存在历史未登记测试集合 `kb-bad9bbe1360a-wd6963d-3-g1`；业务库有若干 `library_id IS NULL` 的历史登记。二者均为**既有残留**，按约束未由我删除，等你授权。

## 已闭环的一条（原"未闭合"第 3 条）
**LibreOffice 是否真正采纳隔离 profile 的安全键 —— 已用真实转换闭环（2026-10-16）**：
在交付镜像的 LibreOffice 25.2.3.2 上跑真实转换，读它**退出时写回**的
`user/registrymodifications.xcu`：7 个键全部以我们设的**非默认值**出现
（`DisableMacrosExecution=true` 默认 false、`MacroSecurityLevel=3` 默认 2、
`CheckDocumentEvents=false` 默认 true 等）；schema 里不存在的键（含错路径的
`Common/LinkUpdate`）**不会被采纳**（有反例控制）。
证据：`artifacts/acceptance/lo-adoption-20261016.json`（`not_adopted=[]`、`bogus_adopted=[]`）。
工具：`artifacts/lo_verify_adoption.py`（本轮修掉它自身两个会**误报"未采纳"**的缺陷：
混写伪键会让 LO 丢弃整份文件 → 真实键与伪键必须分两次跑；回读路径需归一化前导斜杠）。
