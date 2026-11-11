# Agentic RAG 改造交付记录（2026-10-30）

## 已落地并部署

- 基于现有助手的有预算多轮查证、查询调整与一次核验修正；重复调用拦截。
- 分段/分页/长段续读，版本与段落文本指纹绑定的原文引用。
- 文件清单游标分页、完整枚举记录；统计覆盖不完整时不发布全量结论。
- Decimal 来源计算、单位核对；文件数量可由完整且有审计的清单直接生成。
- E 原文证据与 T 工具事实两类引用；权限、版本、引用位置检查与独立语义核验状态。
- router/planner/runtime/retriever/reranker/reader/calculator/verifier/generator 九个可配置模块。
- 自动/快速/深入/统计入口，持久化查证记录与来源链接，真实模型评测命令 kbevalrag。
- Windows LibreOffice 转换和测试探测隐藏窗口，避免打断桌面。

## 验证

全量回归：1649通过、65跳过、40失败。40项失败在 Git HEAD 的原 planner/services.py 和 operations.py 上全部复现，涉及旧确认协议、已退役归档及旧文档测试；证据 artifacts/agentic-rag/baseline-head.xml。未把这些失败改成跳过，也未宣称全量通过。

相关功能回归曾105项通过；随后新增统计覆盖与确定性数量输出，最终 Agentic 专项26项通过。最终增量报告 artifacts/agentic-rag/final-agent-tests.xml。

浏览器：无页面脚本异常；4种方式可选；原文链接打开成功；390px手机视口无横向溢出。证据 artifacts/agentic-rag/browser-check.json 与截图。

真实模型：指定段落阅读、来源引用、语义核验成功，约10.7秒。部署后的跨文件阅读与数量查询均执行了真实工具，但模型没有给出合规引用，被系统拦截；随后加强引用修正提示，并增加完整清单的确定性数量输出。最后两项改动通过自动化测试，尚未重新完成全部真实复杂题验收。

这不是60～100题质量验收报告，不能声称95%证据支持率或跨文档准确率达标。复杂问题在当前模型上仍可能返回“未形成有效引用答案”。工程能力已部署，模型质量验收仍未完成。

## 运行

浏览器 http://127.0.0.1:8800/app/assistant/，刷新页面后使用查证方式选择器。web、worker、scheduler、voice-worker 已使用 knowledgebase:app（同 kb-app:agentic-rag-20261030）。未改数据库结构、未删除原件或向量；未提交其他未提交改动。

回退开关：KB_RAG_ENABLED=0 后重建容器；保留原有普通助手路径。旧应用镜像保留为 kb-app:before-agentic-rag-20261030，旧网页镜像仍为 kb-app:delivery。

模块接口、预算参数、API、评测方式见 docs/agentic-rag.md。运行真实评测会创建有审计的评测会话，报告只输出指标与会话ID，不写原始问题/答案。
