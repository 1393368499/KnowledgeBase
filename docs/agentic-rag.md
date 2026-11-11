# Agentic RAG 与模块化改造

本次改造在既有 Django / PostgreSQL / Qdrant / OpenAI 兼容模型接口上工作，没有增加运行依赖或数据库迁移。

## 用户可见行为

助手输入框可选择自动查证、快速问答、深入查证、完整统计。自动方式根据问题选择流程；模型在流程预算内自主调用检索、规划、原文阅读、关联候选搜索与计算工具。最终答案携带可点击的原文引用，消息下方保留查证步骤、来源与核验状态，刷新后仍存在。

`read_file` 保持文件详情语义；新增 `read_passages` 读取正文，返回 `next_cursor`。段落过长时使用 `char_offset` 续读，不能把截断内容当作整份文件。

`list_files` 使用 `after_id` / `next_after_id` 分页，报告真实总数与 `has_more`。运行记录会核对是否从首批开始并覆盖所有 ID；实时清单不是事务快照，并发变动时不能保证统计时点一致。

`find_related_files` 只返回同库中的关联候选与原文证据，不自动断定合同、附件之间的法律或业务效力。

`calculate_evidence` 对逐字来源中的数值做 Decimal 计算，核对版本、数值、相邻单位及重复输入，不执行表达式。提供项的总和不等于全库总额；业务去重、合同变更与时点口径还需原文判断。

## 运行与停止

快速问答沿用 `KB_LLM_MAX_TOOL_ROUNDS`（默认3轮）；深入查证/统计默认最多6轮。全部工具调用默认最多12次，LLM调用最多10次，其中保留最终回答额度。相同只读工具和参数只执行一次，后续回填重复提示。核验发现缺口后允许一次修正/补查。预算用尽、缺引用、来源失效、语义不支持均有明确状态。

默认120秒为应用调用预算；模型HTTP超时和重试受剩余时间限制，已开始的外部调用和并发闸门不具备强制线程取消能力，因此不是硬实时截止保证。

来源 ID 绑定文件 ID、原件指纹、段号、段落文本指纹。返回前再次校验权限与原文，拒绝伪造或失效来源。原文页面鉴权、转义 HTML、禁止缓存。

引用位置校验与语义核验分别记录。模型核验可能误判，`supported` 不等于独立事实保证；未经真实模型评测不能宣称答案准确率达标。

## 可替换模块

实现位于 `apps/planner/rag/`，通过 `KB_RAG_MODULES` 的部署配置加载 adapter；请求参数不能指定任意模块路径。

|模块名|默认实现|接口|
|---|---|---|
|router|routing.FlowRouter|choose(question, requested), tools(question, schemas)|
|planner|routing.ResearchPlanner|instruction(mode)|
|runtime|runtime.ResearchRun|运行状态、预算、observe、assess、finish、receipt|
|retriever|retrieval.HybridRetriever|search(backend=..., strategy=..., **kwargs), evidence(user,hits,scope), related(...)|
|reranker|apps.knowledge.rerank.RerankClient|rerank(query, documents, top_n=...)|
|reader|reader.SourceReader|read(user,...), enumerate(user,...), file(user,id,scope)|
|calculator|calculator.EvidenceCalculator|calculate(user,operation,items,unit,scope)|
|verifier|verifier.EvidenceVerifier|validate(user,answer,evidence,scope), review(client,...), render(answer,evidence)|
|generator|apps.planner.llm.LlmClient|chat(messages,tools=...) -> LlmReply|

除明确标注的完整路径外，表中类均位于 `apps.planner.rag` 下。替换模块必须遵守权限、预算与 Evidence 字段契约；替换 reader/retriever 不能绕过服务端范围控制。

示例：

```dotenv
KB_RAG_MODULES={"reader":"my_app.rag.CustomReader"}
KB_RAG_RETRIEVAL_POLICIES={"default":{"rerank":false,"limit":10},"deep":{"rerank":false,"limit":20}}
```

助手重排默认关闭；是否启用应通过真实语料对照。检索页面原有默认设置独立保留。

## API

`POST /api/v1/planner/chat/` 接受 `research_mode=auto|fast|deep|statistics`，不传为 auto；原有 message、conversation_id、library_codes 保留。响应与消息 payload 新增 `research`，包含 schema_version、mode、tasks、missing、steps、sources、verification、coverage、budget、usage。

`GET /api/v1/planner/evidence/<file_id>/?ordinal=...&version=...&text_hash=...` 返回引用段落。来源不可读返回404，版本/段落变化返回409。

## 开关与回退

`KB_RAG_ENABLED=0` 关闭新调度、补查核验与页面回执；新增只读工具及分页能力仍可用。无需回滚数据库，不删除原件或向量。所有环境变量见 `.env.example`。

## 验证口径

`tests/test_agentic_rag.py` 使用真实隔离SQL来源和脚本模型验证读取、引用、补查、预算、计算、权限、模块替换。它证明工程行为，不代表真实模型能力。完整测试结果和真实模型验证另附交付报告。

## 统计引用与真实评测

文件清单与计算记录使用 T 开头的证据ID，引用受鉴权保护的 `/api/v1/planner/research-record/<id>/?digest=...`。纯数量查询在完整枚举后可直接呈现真实工具计数；金额等内容统计还必须读取清单中的文件。

真实问题集为JSONL，每行提供 id、question、library_codes、key_points（可选）、mode（可选）。运行 `python manage.py kbevalrag --dataset <路径> --username <真实账号> --output <报告路径> --variant both` 比较当前代码的功能开启/关闭两组。要点字符串命中率不能代替人工事实判定。
