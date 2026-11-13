# AI 检索 + 问答评测基线（第一周）

> 基线生成时间：2026-10-29（UTC）  ·  被测代码：`D:\AProjectScript\KnowledgeBase` 工作树（HEAD `e77cbd9`，**主链路未改动**）
> 运行镜像：`D:\WorkSpace\kb_mirror`（同字节副本；见 §2.1 为什么必须用镜像跑）
> 本文件与 `evals/` 一起是本任务的全部产出。**所有数字都来自 `evals/results/` 里可直接 `json.load` 的结果文件**，没有手填。

---

## 0. 一句话结论

1. **真实栈能用，但不是靠词法路**：真实 PostgreSQL + Qdrant + AS711 上 10 条真实合同查询 **Recall@5 = 90%**，可 `sparse_hits`（Qdrant 全文）**恒为 0**，`db_keyword` 只贡献 15.5 条/查询 —— 质量基本全由稠密向量承担。
2. **词法兜底对"人话"是失效的**：同一套 50 条题，把 query 里的空格去掉（= 用户真实输入形态），离线词法 Recall@5 从 **88% 掉到 12%**，38 条纯中文问句 **0 命中**，唯一命中的 6 条全都带 ASCII 编号/数字。
3. **rerank 目前是负收益**：真实栈开 rerank 后 Recall@5 **0.90 → 0.50**，P50 **1543ms → 2992ms**。重排模型对近重复模板合同打分饱和在 0.98–0.99（正确 chunk 0.9844 vs 错误兄弟 chunk 0.9922，差 0.008），等于把 RRF 的次序随机化。
4. **离线评测可跑、可复现、不烧 API**：`eval` 全链路 sqlite + Qdrant embedded + `KB_TEST_ADAPTER=offline`，50 条 **≈5s**、**0 次外呼**、两次连跑 case 级结果完全一致。
5. **离线指标不能当语义质量**：离线桩向量组（`--dense`）MRR 反而从 80.3% 掉到 48.8% —— 假向量只证明"链路通"，不代表检索能力。

---

## 1. 指标口径与诚实性声明

| 指标 | 定义 | 本次状态 |
|---|---|---|
| Recall@5 | 文档级：top-5 chunk 去重成文件后，命中任一 `ground_truth_doc_ids` 记 1；多条真值不摊薄 | ✅ 实测 |
| recall_all@5 | 同上但对多真值用例取"命中比例"而非 0/1 | ✅ 实测 |
| MRR@5 | 文档级去重后的首个真值名次倒数，未命中记 0 | ✅ 实测 |
| 要点命中率（context 级） | top-5 chunk 文本拼接 → NFKC + 去空白 + 小写后，逐个 `key_points` 做子串匹配 | ✅ 实测 |
| 要点命中率（answer 级） | 需要 LLM 真的生成答案后判分 | ❌ **未实测**（离线不生成答案） |
| P50 / P95 延迟 | 只计 `pipeline.search()` 本身（不含 seed/冷启动），线性插值百分位 | ✅ 实测 |
| token / 成本 | CJK 字符 / 拉丁词 / 数字串启发式估算；单价默认 0（占位，`cost_basis=estimated_placeholder_price`） | ⚠️ 估算，非真实 tokenizer 计数 |
| dense 语义质量 | 离线用 SHA-256 派生的确定性假向量 | ❌ **未实测**（不代表真实 embedding） |
| rerank 真实质量（离线） | 离线无重排服务 | ❌ 未实测 |
| 真实栈（live） | 真实 PG + Qdrant + AS711 | ⚠️ **只测了 10 条**（真实语料不可导出，见 §2.2） |

`run_eval.py --online` 目前**未实现**（按设计明确报错、退出码 2，并指向 `evals/live_check.py`）。真实栈的数字一律来自 `evals/live_check.py`，不混进离线报告。

---

## 2. 环境与语料

### 2.1 为什么在 `D:\WorkSpace\kb_mirror` 里跑

`knowledgebase/paths.py` 的 `assert_inside_project_root()` 要求 `vector_data/`、`data/media/raw/`、`db/` 等运行期目录**必须落在项目根目录内**（`StoragePathError`）。原仓库对本会话只读，所以把 `manage.py + config/ + apps/ + governance/ + pytest.ini + requirements.txt` 同字节复制到 `D:\WorkSpace\kb_mirror`，仅用于**提供可写的运行根**；代码一份未改，`evals/` 拷回原仓库后同一条命令直接可用。

### 2.2 语料：真实合同不能进仓库

真实库 `contract-archive`（library 7）有 47 份真实客户合同 / 1269 chunk，文件名与正文含**客户全名、合同金额**，按约束**不能**进入 `evals/` 或任何提交物。因此：

* **离线 50 条集**：`evals/corpus/` 下 20 篇**构造中文语料**（460–1432 字，markdown）。业务形态参考真实语料的客观结构（编号型文件名 `zy2025194-N`、目录名、`classification=合同/auto_rule`），但客户名、金额、项目名全部虚构。ground truth 由"构造即保证"：`validate_dataset.py` 逐字校验 86 个要点全部出现在真值文档正文中。
* **真实栈 10 条集**：题目从真实文件**程序化**生成（`evals/build_live_dataset.py`，要点必须逐字出现在目标文件里才通过），产物**强制落在 `evals/` 外**（`dataset-live/`）；提交版结果只保留 `doc-<sha256(文件名)[:8]>` 别名，**不含 query 原文、真实文件名、file_id、客户名、金额**（已用 `公司|有限|万元|¥|￥` 扫描验证为空）。

### 2.3 被测环境的真实状态（2026-10-29 实测）

| 组件 | 状态 |
|---|---|
| PostgreSQL `127.0.0.1:55432` / 库 `kb` | ✅ 在线，35 张表，`kb_document_chunk=1272` |
| Qdrant `127.0.0.1:56333` | ✅ 在线，4 个集合（`kb-7-g2` 1269 点 / dim 1024 / Cosine） |
| AS711 embed `172.16.100.114:8000` | ✅ 在线，`bge-large-zh-v1.5`，实测 dim=1024 |
| AS711 rerank `172.16.100.114:8002` | ✅ 在线，`bge-reranker-large` |
| Web `127.0.0.1:8800` | ✅ 在线（登录页 200，API 需鉴权） |
| Docker 引擎（本会话视角） | ❌ `docker ps` → `permission denied ... dockerDesktopLinuxEngine`（**服务是别人起好的，本会话不要再硬起**） |

> 权限口径：评测统一走**超管**路径 `permission_codes=None`（`apps/knowledge/views.py:745-849` 的 `readable_library_ids` 对 `is_system_admin` 返回 `None`），即"用户能看到全部"的上限情形；普通成员（`library:N` 过滤）未纳入本次基线。

---

## 3. 50 条评测集设计（`evals/datasets/kb-50.jsonl`）

| 桶 | 条数 | 设计意图 | 典型构造 |
|---|---|---|---|
| `typical` | 20 | 一周内会被真实问到的问题 | 单资产报价、预付比例、驻场人数、维护窗口 |
| `hard` | 20 | 需要跨文档/数字/近重复区分 | 主合同↔补充协议、同客户跨年合同、跨文档引用同一金额、缩写密集（RAG/BM25/SLA）、日期型 |
| `pitfall` | 10 | **已知会翻车的输入形态** | 纯编号 `zy2025194`、整数 `3800`、缩写 `服务等级协议`、指代 `今年签的那份`、泛化 `合同 资料 怎么 找` |

每条字段：`{id, bucket, query, ground_truth_doc_ids, key_points, notes}`；50 条 id / query 全唯一，86 个要点。
校验：`python evals/validate_dataset.py` → 通过；人为破坏 6 处能抓出 8 个问题并非零退出。

刻意埋的坑（也是真实系统的痛点，语料层面对齐）：
3 组近重复标题（`zy2025194-25`/`-29`、`8`/`9` 号迎宾馆、能源主合同/补充协议）· 3 篇跨文档数字混淆 · 4 篇表格化 · 1 篇 1432 字超长（答案在最后一节）· 1 篇缩写密集 · 1 篇编号型首行。

---

## 4. 基线结果

### 4.1 主表

| # | 配置 | n | Recall@5 | recall_all@5 | MRR@5 | 要点命中 | P50 | P95 | 结果文件 |
|---|---|---|---|---|---|---|---|---|---|
| **A** | 离线词法（默认，`dense=False`） | 50 | **88.0%** | 88.0% | **80.3%** | **93.0%** (80/86) | 29.4ms | 44.3ms | `20261029T024443Z.json` |
| **B** | 离线桩向量（`--dense`，假向量） | 50 | 80.0% | 79.0% | 48.8% | 89.5% (77/86) | 38.5ms | 54.5ms | `dense-stub-20261029T024454Z.json` |
| **C** | 离线词法 · **自然语句**（去空格） | 50 | **12.0%** | 12.0% | 10.7% | 16.3% (14/86) | 16.8ms | 31.7ms | `natural-20261029T024506Z.json` |
| **D** | **真实栈**（PG+Qdrant+AS711，dense+RRF） | 10 | **90.0%** | 85.0% | **65.8%** | **71.7%** | 1543ms | 1729ms | `live-20261029T015255Z.json` |
| **E** | 真实栈 + **rerank**（bge-reranker-large） | 10 | 50.0% | 50.0% | 50.0% | 63.3% | 2992ms | 3254ms | `live-20261029T015331Z.json` |

离线全轮耗时：seed 3.2s + 检索 1.2–2.1s（50 条）；`workdir` 跑完自动清理，`workdir_leftover=null`。token（估算）：embed 547 / rerank 0 / llm 0；成本默认单价 0（占位，未编造厂商价格）。

> 表内 5 份结果都可在 `evals/results/` 直接打开，数字与文件一一对应（`json.load` 拿到的 summary 就是表里这些值）。A–C 由**仓库根目录**内的同一条命令产出并复跑核对过；延迟随机器负载波动 ±50%，别拿毫秒数做跨机比较。B 组桩向量对语料文本敏感——最后一次语料脱敏改动后 MRR 由 47.7% 微变到 48.8%，表内已是最终值。
>
> A 与 B 的唯一差别是"是否把确定性假向量投进融合"——**假向量让 MRR 从 80.3% 掉到 48.8%**，是"离线桩只能验链路、不能验语义"的直接证据。

### 4.2 分桶

| 桶 | A 词法 Recall@5 / MRR / 要点 | B 桩向量 | C 自然语句 | D 真实栈（10 条） |
|---|---|---|---|---|
| typical | 100.0% / 0.942 / 100.0% | 85.0% / 0.507 / 94.9% | **0.0% / 0.000 / 0.0%** | 各桶见下 |
| hard | 100.0% / 0.942 / 100.0% | 90.0% / 0.567 / 91.9% | 30.0% / 0.267 / 37.8% | — |
| pitfall | **40.0% / 0.250 / 40.0%** | 50.0% / 0.295 / 60.0% | 0.0% / 0.000 / 0.0% | — |

真实栈 10 条分桶（D，rerank=false）：编号型 1.0/1.0 · 编号+资产数+金额 1.0/0.5 · 编号+资产数 1.0/1.0 · 近重复兄弟文件 1.0/1.0 · 近重复+金额 1.0/0.5 · **条款/收费标准 0.0/0.0**（唯一 R@5=0）· 单据类 1.0/1.0 · 小文档 1.0/0.33 · 日期型+近重复 1.0/0.25 · 跨文档真值 1.0/1.0。

### 4.3 可以怎样解读这两套数字

* **A 组 88% 不是"检索很强"**，而是"这批 query 恰好是空格分词、又能被 `icontains` 命中"的上限；换成连续中文（C 组）立刻 12%。
* **D 组 90% 才是真实用户路径**（稠密语义 + 关键词兜底 + 文件名），但样本只有 10 条、且是编号型为主的合同查询，**不能外推成全量 90%**。
* **E 组说明 rerank 现在别开**（见 §6-F6）。

---

## 5. 复现命令

### 5.1 离线（主力，无需任何服务、零外呼）

```powershell
# 0) 前置：仓库根放好 evals/，用项目 .venv；Windows 控制台先设 UTF-8 免得中文日志报 GBK 错
$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'
cd D:\AProjectScript\KnowledgeBase

# 1) 数据集自检（50 条、86 要点、真值逐字命中）
python evals\validate_dataset.py

# 2A) 词法基线（= 表 A；默认 offline，无需任何参数）
python evals\run_eval.py

# 2B) 只跑前 10 条 / 换结果目录 / 自定义单价（成本估算是占位，别当真实账单）
python evals\run_eval.py --limit 10 --out-dir artifacts\eval-results --prices '{"input_per_m":0.5,"output_per_m":1.5}'

# 2C) 桩向量组（= 表 B；仅验证 dense 链路通不通，不可当语义质量）
python evals\run_eval.py --dense

# 2D) 自然语句组（= 表 C：把 query 里的空格去掉，模拟真实输入形态）
python evals\make_query_variant.py --in evals\datasets\kb-50.jsonl --out artifacts\kb-50-natural.jsonl
python evals\run_eval.py --dataset artifacts\kb-50-natural.jsonl
```

结果写到 `evals/results/<UTC时间戳>.json`（`meta/summary/cases/not_measured` 四块，`json.load` 直接可读）。退出码：`0` 正常 / `2` 用法 / `3` 数据集非法 / `4` 依赖缺失 / `5` 全部查询失败（报告仍会写盘）。

### 5.2 真实栈（= 表 D/E；只读真实库，结果脱敏）

```powershell
# 前置：PG 55432 / Qdrant 56333 / AS711 8000+8002 都在线；.env 里有 KB_PG_* 与 AS711_*
$env:PYTHONIOENCODING='utf-8'

# 1) 从真实语料造 10 条题（要点必须逐字命中目标文件才收录；产物落在 evals/ 外）
python evals\build_live_dataset.py --library-code contract-archive

# 2) 跑真实检索（dense+RRF）
python evals\live_check.py --limit 10

# 3) 跑 rerank 对照（= 表 E）
python evals\live_check.py --limit 10 --rerank
```

提交版脱敏结果写到 `evals/results/live-<UTC>.json`；含 file_id / 真实文件名的明细只落在 `evals/` 外的本地目录。

### 5.3 起服务再跑全量（这次没跑的部分，runbook）

```powershell
# 1) 起基础设施（若还没起；本会话 Docker 引擎不可用，服务是别人起好的，别再硬起）
docker compose up -d --wait
python manage.py migrate

# 2) 全量离线回归（不用任何模型，适合 CI/每次改动）
python evals\run_eval.py --limit 50

# 3) 真实栈全量：需要一份"真实语料 + 人工确认过的真值"的 50 条集
#    当前 build_live_dataset.py 支持 spec 模式（--spec 传你自己写的用例 JSON），
#    live_check.py 接受任意 jsonl（--dataset），所以：
#    a. 用 --spec 造题并逐条人工确认 ground truth
#    b. python evals\live_check.py --dataset <你的50条.jsonl> --limit 20
#    这一步**本次未做**：50 条真实集的真值标注需要人工，不在本周范围。
```

---

## 6. 失败分析（5 类，都有实测证据）

### F1 中文连续问句 → 两条词法路同时归零（**最致命**）

* 机制：`apps/knowledge/pipeline.py:2377` 的 `_db_keyword_search` 用 `re.split(r"[\s\W_]+", query)` 切词 —— 汉字属于 `\w`，**没有空格/标点的中文整句会被当成一个 term** 做 `text__icontains`；而 sparse 路要的是 Qdrant `MatchText`，见 F2。
* 证据（真实库 1269 chunk 上直接调 `_db_keyword_search`）：

  | query | 命中 chunk |
  |---|---|
  | `合同金额` | 16 |
  | `预付款比例` | **0** |
  | `预付款比例是多少` | **0** |
  | `付款方式是什么` | **0** |
  | `违约责任怎么规定的` | **0** |
  | `zy2025194-8`（纯编号，正文无文件名） | **0** |
* 量化：表 C —— 同一 50 条题去掉空格，Recall@5 88% → **12%**；**38 条纯中文问句 0 命中**，仅 6 条命中且全部含 ASCII 编号/数字（`zy2025194-9`、`BJ2026-0142`、`FP2025-3382`、`content_sha256`、`33,600`）。
* 影响：线上"用户正常打字"的形态主要吃这条；dense 一旦降级/超时，系统就退化成 0 召回。

### F2 Qdrant 全文（sparse）路对自然问句恒 0

* 机制：text 索引存在（`tokenizer=multilingual, lowercase=true`），但 `MatchText` 是 **词级 + AND 语义**：查询被切出的所有 token 都必须出现。长问句里只要有"是多少/怎么/的"这类词没被切对，整条查询直接 0 命中。
* 证据（REST 直查 `kb-7-g2`）：`合同金额`=5、`付款`=5、`海南`=5、`保密义务`=5、`违约责任`=5，但 **`合同金额是多少`=0、`质保期`=0**；真实栈 10 条 `sparse_hits_mean=0.0`（两轮一致）；离线 embedded 模式**根本不建 payload 索引**（`local_mode_sparse_unsupported=True`，Qdrant 明确警告 payload index 在 local 模式无效）。
* 影响：三路召回里有一路是常年哑火的；`pipeline.py:2222` 的注释只提到"whole-token"，实际杀手是 AND。

### F3 排序缺区分度：无 IDF、无覆盖率、同分由 file_id 决定

* 机制：DB 兜底打分 `1/rank + hits/100`，`rank` 来自 DB 行序；文件名命中（`filename_search`）在 RRF 之后**整串 icontains 命中即 promote**，没有任何"匹配了多少"的权重。
* 证据：P03 `zy2025194` —— 6 份合同共用该前缀，真值（`zy2025194-9`）被挤出 top5；真实栈 L09（编号 `zh2025194-13` vs 文件名 `zy…`）MRR 0.25；L02/L05（同模板兄弟文件）MRR 0.5。A 组 `hard` 桶 MRR 0.942 但 `pitfall` 只有 0.250。
* 影响：**Recall 会掩盖排序问题**——找得到但排不进前 3，问答阶段拿到的上下文就是错的兄弟文件。

### F4 字面量/缩写/相对时间不归一

* 证据：P07 `3800` vs 正文 `3,800` → 0 命中；P08 `服务等级协议` vs 正文 `SLA 99.5%` → 0 命中；P10 `今年签的那份` 无时间锚点 → 退化到泛化词（MRR 0.5）。
* 附带：**要点判分对数字格式敏感**——真实栈 L02 要点写 `12,000.00`、文档写 `¥:12000.00`（无千分位），要点被误判 miss。这属于**指标口径缺陷**而非检索失败，已在 §9 记录。

### F5 chunk 级粒度：重复占位 + 长文档尾段/短文档淹没

* 证据：真实栈探针里 top5 出现**同一文件两条 chunk**（file 53 占 #3 与 #5）；真实栈 L06 是唯一 R@5=0 —— 目标文档只有 2 个 chunk（"邀请函"文体，载体却是"签字确认单"PDF），在 1269 个模板长合同 chunk 里被淹没；L05 文档级 rank2 但含关键数字的 chunk 没进 top5（要点 0）。
* 机制：`search()` 返回 chunk 级结果，文档级去重只发生在**指标计算**里；`split_long` 把 >1200 字文档切两段，头部 query 只召回 chunk0（`manual-kb-search-guide` 的"最多返回 10 条"就在第三节）。

### F6 rerank 目前是负收益（真实栈实测）

* 数据：Recall@5 **0.90 → 0.50**、MRR 0.658 → 0.500、P50 **1543ms → 2992ms**（+94%）、P95 1729 → 3254ms。
* 根因（直接调 `RerankClient` 定位，不是 harness bug）：正确 chunk **0.9844** vs 错误兄弟 chunk **0.9922**（差 0.008，噪声级），无关 chunk 0.1621。方向正确，但 `bge-reranker-large` 对**近重复模板条款**打分饱和在 0.98–0.99，分辨率不足以区分兄弟文件；重排 30 个模板候选 = 把 RRF 的次序随机化。给候选加的 `"文件名：<原名>\n"` 前缀实测不改变排序。
* 影响：契约/制度类"同一模板批量签"的语料正是本项目主体，rerank 默认关闭是对的；开启需按查询类型分流。

---

## 7. 最值得修的 3 个 case（按 ROI 排序）

### R1（最高 ROI）中文查询理解：让词法路对"人话"有效

* **现象**：表 C，自然语句 Recall@5 **12%**；纯中文 38 条 0 命中。
* **根因**：F1（`pipeline.py:2377` 整句 `icontains`）+ F2（`MatchText` 的 AND 语义）。
* **建议改法**（不动主链路也能先验证）：query 侧先做**中文切分**（2-gram 滑窗或 jieba），DB 兜底改成 **n-gram OR + IDF/覆盖率加权**；sparse 侧对长 query 降级为"抽取关键词子集逐个 MatchText 再并集"，而不是整句 AND。
* **预期收益**：C 组从 12% 往 A 组的 88% 靠；dense 降级/超时时不再 0 召回。
* **复现**：`python evals\make_query_variant.py ...` + `run_eval.py --dataset artifacts\kb-50-natural.jsonl`。

### R2 排序区分度：IDF + 覆盖率 + 文件名命中加权

* **现象**：pitfall 桶 MRR 0.250；P03 真值被同前缀兄弟挤出 top5；真实栈 L09/L02/L05 MRR 0.25–0.5。
* **根因**：F3（`1/rank + hits/100`、整串 promote、无 IDF）。
* **建议改法**：命中词数/查询词数做覆盖率乘子；加 IDF（"合同/资料"这类泛词降权）；文件名命中按"匹配长度占比"加权而非置顶；同级 tie-break 固定为 `(score, coverage, file_id)` 并写测试。
* **预期收益**：MRR +10pp 量级（pitfall 桶 0.25 → 0.5+），近重复兄弟文件不再靠运气。

### R3 rerank 决策：按查询类型分流 + 分数归一

* **现象**：真实栈 Recall@5 0.90 → 0.50（F6）。
* **建议改法**：① 编号/数字/金额类查询（正则命中 `[A-Za-z]{2,}\d{4,}`、`\d{3,}`）默认**跳过 rerank**；② 别用 rerank 分数**替换** RRF 分数，min-max 归一后加权融合；③ 近重复候选（同模板、文本相似度高）在重排前先去重，或只对 top-3 之外的候选重排。
* **预期收益**：把那 0.40 的 R@5 拿回来，同时省掉 1.4s P50。

（备选第 4 条：短小/非合同文种召回 + chunk 去重，见 F5；证据只有 1 条 case，先观察。）

---

## 8. 已知坑（下次别重踩）

| # | 坑 | 处理 |
|---|---|---|
| K1 | 本会话 Docker 引擎不可用（`permission denied ... dockerDesktopLinuxEngine`），但 PG/Qdrant/AS711/Web **都是别人起好的、在线** | 先 `Test-NetConnection` 探端口，别急着 `docker compose up` |
| K2 | 运行期目录必须在项目根内，否则 `knowledgebase.paths.StoragePathError` | `--workdir` 用 `<repo>/artifacts/evals/<run_id>`（默认就是） |
| K3 | 每次 run 要独立 workdir（独立 sqlite + embedded qdrant） | run_eval 默认每次生成新 workdir 并自动清理；手动调 harness 时要自己换目录 |
| K4 | 离线 embedded 模式**不建 payload 索引** → sparse 路结构性不可测 | 别拿离线 `sparse_hits=0` 当"线上也 0"的证据（线上是 F2 的 AND 语义导致的 0，机制不同） |
| K5 | `KB_TEST_ADAPTER=offline` 时 `pipeline` 会刻意忽略 `KB_SEARCH_DENSE_ENABLED` / `KB_MODEL_CALLS_DISABLED`（`pipeline.py:2038`） | harness 用"检索瞬间临时摘 adapter"实现 `dense=False`；结论以 `meta.dense` 为准 |
| K6 | Windows 控制台按 GBK 打印 UTF-8 中文 → `UnicodeEncodeError: 'gbk' codec can't encode character '\xa5'` | `$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'` |
| K7 | 本机 `HTTP_PROXY/HTTPS_PROXY=127.0.0.1:17890`，`qdrant_client`/`httpx` 直连本地会被代理搅坏（`URLPattern(key): None` 之类怪错） | 脚本里 pop 掉代理环境变量，或裸 `urllib` 直连 |
| K8 | 早期有一次 `meta.dense=false` 的运行，产出的排序与 `--dense` 组**完全一致**（根因未定位） | 基线只认"当前干净运行 + 明确参数"的结果；怀疑离线组数字时先 `--dense` 对照一次 |
| K9 | 要点命中判定对数字格式（千分位/`¥`/全角）敏感，会把"检索成功"误判成 miss | 判分前把数字归一（去千分位、去货币符号）；或把要点写成不含数字的短语 |

---

## 9. 交付物清单

```
evals/
├─ BASELINE.md              # 本文件
├─ run_eval.py              # CLI：--dataset/--limit/--offline/--dense/--rerank/--out-dir/--prices…
├─ validate_dataset.py      # 数据集 + 语料 + 要点自检（负向自测可抓 8 类问题）
├─ make_query_variant.py    # 造输入变体（去空格=自然语句、加空格=词法可控），复现表 C
├─ build_live_dataset.py    # 从真实语料造 10 条 live 题（产物强制落在 evals/ 外）
├─ live_check.py            # 真实栈评测（--limit/--rerank/--out），复用同一套 metrics
├─ offline_settings.py      # 离线 Django settings 入口（sqlite + embedded + KB_TEST_ADAPTER=offline）
├─ kb_eval/
│  ├─ harness.py            # bootstrap/seed_corpus/search/close（离线执行层）
│  ├─ live_harness.py       # bootstrap_live/search（真实栈执行层，只出 doc-<sha8> 别名）
│  ├─ metrics.py            # Recall@k / recall_all / MRR / 要点命中 / P50-P95 / token / 成本
│  ├─ report.py             # meta/summary/cases/not_measured 报告聚合
│  └─ client_http.py        # 在线 HTTP 检索客户端（已实现，未接线到 run_eval --online）
├─ corpus/*.md              # 20 篇构造语料（脱敏）
├─ datasets/kb-50.jsonl     # 50 条评测集（20/20/10）
└─ results/                 # 结果 JSON（表 A–E 各一份，全部可 json.load）
```

未实测清单（`results/*.json` 的 `not_measured` 字段里逐条带 reason）：answer 级要点命中、dense 语义质量、rerank 真实质量（离线）、真实 tokenizer token 数、50 条真实语料集、`run_eval.py --online`。

---

## 10. 修复后复测（2026-10-29，同一套题、离线词法）

本节记录针对第 5 节三个缺陷的修复与**同口径复测**。代码改动见
`apps/knowledge/textquery.py`（新增）、`pipeline._db_keyword_search`、
`pipeline._should_apply_rerank`、`vectorindex.search_sparse`；回归用例见
`tests/test_search_cjk_terms.py`（12 条）。

| 场景 | 修复前 Recall@5 | 修复后 Recall@5 | 数据 |
|---|---|---|---|
| 表 A：带空格的评测集（`datasets/kb-50.jsonl`） | 88.0% | **92.0%** | `evals/results/post-fix-20261030T020730Z.json` |
| 表 C：去掉空格的连续中文（`artifacts/kb-50-natural.jsonl`） | 12.0% | **92.0%** | `evals/results/post-fix-natural-20261030T020739Z.json` |

复现命令：

```powershell
python evals\make_query_variant.py --in evals\datasets\kb-50.jsonl --out artifacts\kb-50-natural.jsonl
python evals\run_eval.py --out-dir artifacts\eval-after-original
python evals\run_eval.py --dataset artifacts\kb-50-natural.jsonl --out-dir artifacts\eval-after-natural
```

改动要点（每条都对应一个已量化的缺陷）：

1. **中文切词**：DB 关键词兜底改用二元组（`预付款比例` → 预付/付款/款比/比例），
   并按"覆盖查询词数"打分、丢弃零覆盖候选。这是表 C 从 12% 到 92% 的直接原因。
2. **稀疏路自愈**：`search_sparse` 遇到 `MatchText` 报错（老 collection 没建
   `text` 全文索引）时补建索引重试一次；仍失败则交给 pipeline 记降级并走 DB 兜底。
   报告新增 `sparse_mode`（`match_text` / `cjk_whole_token` / `empty`），
   不再让"0 命中"看起来像"库里没有"。
3. **重排饱和保护**：新增 `KB_RERANK_MIN_SPREAD`（默认 0.05）。重排候选分数
   极差低于阈值（模板重复合同实测 0.9844 vs 0.9922，差 0.008）时**不采用重排
   顺序**，保留 RRF 融合序，报告写 `rerank.applied=false` + `skipped=saturated`
   + `score_spread`。设为 0 可恢复旧行为。

**仍未实测**：第 3 条在真实栈上的效果（Recall@5 0.50 → ?、P50 2992ms → ?）。
它需要 PG + Qdrant + AS711 起来后跑 `python evals/live_check.py --rerank --limit 10`；
本次复测只覆盖离线词法两条，重排门槛由单元测试固定，未做真实栈回归。

### 10.1 真实栈复测（2026-10-30，补上 §10 的缺口）→ **发现回归，修复不可合并**

服务都在线（PG 55432 / Qdrant 56333 / AS711 8000+8002），同一套 10 条真实题复跑：

| 组 | 配置 | 修复前 Recall@5 / MRR / 要点 | 修复后 Recall@5 / MRR / 要点 | P50 前→后 | 结果文件 |
|---|---|---|---|---|---|
| D | 真实栈 dense+RRF | 0.90 / 0.658 / 0.717 | **0.50** / 0.303 / 0.633 | 1543→1182ms | `post-fix-live-dense-20261030.json` |
| E | 真实栈 + rerank | 0.50 / 0.500 / 0.633 | 0.50 / **0.425** / 0.533 | 2992→2758ms | `post-fix-live-rerank-20261030.json` |

逐条（doc 级 R@5，前→后）：**L01 1.0→0.0、L02 1.0→0.0、L05 1.0→0.0、L09 1.0→0.0**，L03 MRR 1.0→0.20、L04 1.0→0.33，L06 仍 0，L07/L08/L10 不变。
检索报告两轮一致：`db_keyword_hits=30`（**修复前均值 15.5**）、`sparse_hits=0`、`fused=30`、`degraded=0`；rerank 组 `rerank_applied=9/10`。

**根因判断**：第 1 条把 DB 兜底改成二元组 + 覆盖率打分后，2-gram（合同/付款/服务…）几乎等长命中全库模板合同，`_db_keyword_search` 每查询稳定返回 30 条泛化候选；RRF 里它和 dense 同权，排名 1–30 的泛化 chunk 把真正的编号型文档挤出 top5。离线构造语料每篇文风差异大、2-gram 区分度高，所以离线从 12% 涨到 92%；真实语料是同一模板批量签约，2-gram 毫无区分度，于是反向。**这正是"离线指标不能外推"的第二个实证。**

第 3 条（`KB_RERANK_MIN_SPREAD=0.05`）也没生效：`rerank_applied=9/10` —— 阈值比的是 30 个候选的**全局极差**（模板候选之间仍有 0.16 vs 0.99 的差距），而不是"top-k 是否饱和"，所以该跳过的没跳过。

**建议的下一步（按顺序）**：
1. DB 兜底**只在 dense 不可用时启用**（`degraded` / `dense_hits < limit`），或给它一个远低于 dense 的融合权重（RRF 加权重参数），别让它与稠密同权进融合。
2. 二元组**加文档频率上限**：命中文档数 > 库内 5% 的 2-gram 直接丢弃（"合同""服务"这类），等价于给词法路做 IDF。
3. rerank 饱和判定改成**只看 top-k 分数极差**（或 top-3 与 #4 的 gap），`applied=false` 时确保完全回退到 RRF 序。
4. 每次改完**同时**跑离线 A/C 与真实栈 D/E 四组再决定是否合并（本次教训：只跑离线会放过真实回归）。

复现命令（真实栈，需服务在线）：

```powershell
python evals\live_check.py --limit 10 --dataset <dataset-live>\kb-live-10.jsonl --out evals\results\post-fix-live-dense-<日期>.json
python evals\live_check.py --limit 10 --rerank --dataset <dataset-live>\kb-live-10.jsonl --out evals\results\post-fix-live-rerank-<日期>.json
```

> 离线两条复测（A/C）的结果文件：`post-fix-20261030T020730Z.json`（A）、`post-fix-natural-20261030T020739Z.json`（C）。


