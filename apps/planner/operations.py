"""操作意图登记表：AI 能执行的全部动作都在这里声明。

登记项决定四件事：
1. 需要什么权限（permission 字段，由权限层解释）；
2. 风险级别与是否需要用户确认卡（``requires_confirmation``）；
3. 是否需要"三次确认 + 名称输入"（``requires_destructive_flow``）；
4. 是否需要一次性凭证（``ticket_scope``）。

新增能力时只改这里，API 与凭证流程自动适配（需求 10 的扩展点）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class OperationSpec:
    intent: str
    label: str
    params: dict[str, Any] = field(default_factory=dict)
    permission: str = "library:read"
    risk: str = RISK_LOW
    requires_confirmation: bool = False
    requires_destructive_flow: bool = False
    ticket_scope: str = ""
    summary: str = ""
    # 免确认执行：提案即执行，界面上不出现"待确认卡"。
    #
    # 只允许**纯只读、不写库、不产生副作用**的意图打开（``test_operation_registry``
    # 有守卫）。仍然会被完整审计：OperationRecord 照旧落库，状态从 proposed 直接
    # 走到 executed，事件照旧记 —— "不需要人点确认"不等于"不记账"。
    # 打开它的前提是用户明确要求（2026-10-19：搜索知识库每次都要点一下确认，
    # 用户反馈"搜索知识库不需要用户确认"）。
    auto_execute: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "label": self.label,
            "params": self.params,
            "permission": self.permission,
            "risk": self.risk,
            "requires_confirmation": self.requires_confirmation,
            "requires_destructive_flow": self.requires_destructive_flow,
            "ticket_scope": self.ticket_scope,
            "summary": self.summary,
            "auto_execute": self.auto_execute,
        }


OPERATIONS: dict[str, OperationSpec] = {}
# 保留旧记录的风险/权限定义供历史校验；界面、模型与公共接口不再提供这些动作。
RETIRED_INTENTS = frozenset({"archive_file", "unarchive_file"})


def register(spec: OperationSpec) -> OperationSpec:
    OPERATIONS[spec.intent] = spec
    return spec


def get(intent: str) -> OperationSpec | None:
    return OPERATIONS.get(intent)


def all_specs() -> list[OperationSpec]:
    return [spec for spec in OPERATIONS.values() if spec.intent not in RETIRED_INTENTS]


def is_mutation(intent: str) -> bool:
    spec = get(intent)
    return spec is not None and spec.risk in {RISK_MEDIUM, RISK_HIGH, RISK_DESTRUCTIVE}


# ------------------------------------------------------------------ 只读类 ----
register(OperationSpec("list_files", "列出资料", {"status": "str?", "library": "str?"},
                       permission="library:read", summary="按状态/信息库列出已入库资料"))
register(OperationSpec(
    "search_knowledge", "搜索知识库",
    {"query": "str", "library": "str?", "customer": "str?", "department": "str?", "amount": "str?"},
    permission="library:read",
    # 纯只读检索：不改任何数据、可重复执行、结果对用户即时可见。
    # 让用户为"搜一下"点一次确认纯属多余摩擦 —— 提案即执行，结果直接铺在卡片上。
    # 注意：**只读**是这里放开的前提，不要顺手把 list_files/read_file 之外的
    # 写操作也标上（写操作必须保留确认卡）。
    auto_execute=True,
    summary="关键词 + 结构化筛选检索（只读，提案即执行）",
))
# 检索反馈：让"准确/不准确"也能由首页 AI 提案提交（目标 ④）。
# 它是**写操作**（会落库 + 写审计），但不是删除，所以不套用三次确认。
#
# `requires_confirmation` 的口径（独立审计 A2 修正）：它是**风险提示**，
# 不是"确认次数开关"。真正的次数由服务端定死 —— 破坏性动作 3 次、其余一律 1 次
# （`services.propose_operation`；执行端 `execute_operation` 要求状态为 ARMING 后的 ARMED）。
# 早先的写法是 `1 if spec.requires_confirmation else 1`（两侧同值），看起来像
# "无需确认的动作零确认"，实际也是 1，容易被误读 —— 现在表达式只剩一处 1，并且：
#   * **会写库的动作必须为 True**（由 `tests/test_operation_registry.py` 锁住）；
#   * 纯只读动作可以是 False，含义只是"没有破坏性影响"，仍会经过同一个确认卡。
register(OperationSpec(
    "submit_search_feedback", "标记检索反馈",
    {"query": "str", "verdict": "str", "file_id": "int?", "chunk_ordinal": "int?", "note": "str?"},
    permission="library:read", risk=RISK_MEDIUM, requires_confirmation=True,
    summary="把某次检索命中标记为准确/不准确（可改判；不修改源资料）",
))
register(OperationSpec("runtime_metrics", "查看真实运行指标", {}, permission="system:read",
                       risk=RISK_LOW,
                       summary="真实可测指标（库/资料/分片/熔断/队列深度/停滞/待反馈）；巡检留痕让看门狗、结果校验、双阶段补扫都有真实判定来源，未实现的仍如实列出"))
register(OperationSpec(
    "run_patrol", "立即巡检", {"limit": "int?"},
    permission="system:operate", risk=RISK_LOW, requires_confirmation=False,
    summary="立即跑一次**只读**巡检（与周期任务 knowledge.patrol 同一实现）：重试次数用尽、停滞、"
            "向量不完整、原件缺失四项；结论留痕后，界面的「解析看门狗 / 结果校验」随即有真实读数",
))
register(OperationSpec("get_stats", "查看运行状态", {}, permission="system:read",
                       summary="资料/库/任务统计"))
# 反馈处理：让 AI 能提案"哪些资料被反复判为不准确"（重扫的**定位**步骤，只读）。
# 真正的重排/重建仍需人工确认，且不会改动源资料。
register(OperationSpec(
    "list_feedback_targets", "查看待重扫资料",
    {"limit": "int?"},
    permission="library:read",
    risk=RISK_LOW,
    summary="按人工反馈找出净负分的资料（被反复判不准确），供决定是否重扫/重建",
))
# 学习规则：**如实说明"这里没有训练"**。反馈加权是检索时**实时**聚合的
# （`apps/knowledge/feedback.py`，有界 ±15%），所以原型里那个"触发一次学习"
# 没有真实动作可做 —— 这个只读意图返回**当前生效的规则快照**（每份资料的
# 准确/不准确净分与权重），让界面按钮对应一个真实、可复盘的动作，而不是假成功。
register(OperationSpec(
    "feedback_learning_report", "查看学习规则",
    {"limit": "int?"},
    permission="system:read",
    risk=RISK_LOW,
    summary="只读返回当前生效的**有界**检索加权规则（按资料的准确/不准确净分与权重）；"
            "本系统不做训练：不更新模型、不改向量、不改源文档",
))
register(OperationSpec("test_model_services", "测试模型服务连通", {"probe": "str?"}, permission="system:read",
                       summary="检查 AS711 四个模型服务的**配置状态**（默认不发探测请求）"))
register(OperationSpec("read_file", "查看资料详情", {"file_id": "int"},
                       permission="library:read"))
register(OperationSpec("list_audit", "查看审计事件", {"action": "str?", "limit": "int?"},
                       permission="audit:read"))
register(OperationSpec("guide", "系统使用说明", {}, permission="library:read"))

# ------------------------------------------------------------------ 索引运维 ----
#
# 这些能力**已经存在**于管理命令与知识库 API（kbrebuild / generations / index-stats），
# 登记到这里是为了让"首页 AI 能操作一切"覆盖它们，而不是让操作目录只覆盖一部分。
# 它们全部**复用既有服务**，不另起一套逻辑。
register(OperationSpec(
    "embedding_profiles", "查看嵌入模型配置", {},
    permission="system:read",
    summary="列出嵌入 profile 的维度/模型/端点与当前生效项（只读，不探测）",
))
register(OperationSpec(
    "index_stats", "查看索引状态", {"library": "str?"},
    permission="library:read",
    summary="各库当前生效代次、collection、已索引分段数（只读）",
))
register(OperationSpec(
    "rebuild_index", "重建信息库索引", {"library": "str", "profile": "str?"},
    permission="library:admin", risk=RISK_HIGH, requires_confirmation=True,
    summary="新建一代 → 全量重建 → 真实 coverage 校验 → 原子激活；失败保留旧代",
))
register(OperationSpec(
    "activate_generation", "切换向量代次", {"library": "str", "collection": "str"},
    permission="library:admin", risk=RISK_HIGH, requires_confirmation=True,
    summary="把某个**已登记且构建完成**的代次切上线（会做真实 points coverage 校验）",
))
register(OperationSpec(
    "rollback_generation", "回退向量代次", {"library": "str", "to_generation": "int?"},
    permission="library:admin", risk=RISK_HIGH, requires_confirmation=True,
    summary="回退到上一代（或指定代次）；不重嵌入，直接按真实登记维度切换",
))

# ------------------------------------------------------------ 需确认的写入 ----
register(OperationSpec(
    "upload_file", "上传资料", {"library": "str"}, permission="library:write",
    risk=RISK_MEDIUM, requires_confirmation=True,
    summary="上传需要用户亲自选文件；服务端只负责把选中的文件入库",
))
register(OperationSpec(
    "parse_file", "解析资料", {"file_id": "int"}, permission="library:write",
    risk=RISK_MEDIUM, requires_confirmation=True, summary="触发解析与向量化",
))
register(OperationSpec(
    "add_to_library", "把资料添加到信息库", {"file_id": "int", "library": "str"},
    permission="library:write", risk=RISK_MEDIUM, requires_confirmation=True,
    summary="改变资料归属，需要确认卡",
))
register(OperationSpec(
    "create_library", "新建信息库", {"code": "str", "name": "str", "level": "str?"},
    permission="library:admin", risk=RISK_MEDIUM, requires_confirmation=True,
))
register(OperationSpec(
    "restart_stalled_task", "重启停滞任务", {"task_id": "int?"}, permission="system:operate",
    risk=RISK_MEDIUM, requires_confirmation=True,
    summary=(
        "把**租约已过期、卡在 RUNNING** 的资料重新推进（真的执行一次），"
        "并如实区分：实际推进 / 失败 / 租约被占跳过 / 未完成；"
        "租约仍有效的资料一个字节都不改"
    ),
))
register(OperationSpec(
    "reset_breaker", "重置熔断器", {}, permission="system:operate",
    risk=RISK_MEDIUM, requires_confirmation=True,
))
register(OperationSpec(
    "reparse_failed", "重扫未处理反馈", {"limit": "int?", "include_failed": "str?"},
    permission="system:operate", risk=RISK_MEDIUM, requires_confirmation=True,
    summary=(
        "对**净负分**（被反复判不准确）的资料真正重跑一次流水线：复用同一份只读原件，"
        "重建分段与向量，不改源字节；逐份如实报成功/失败/租约被占/未完成。"
        "传 include_failed=\"true\" 时**额外**一并重试解析失败的资料（两类分开计数）"
    ),
))

register(OperationSpec(
    "archive_file", "归档资料（移出检索）", {"file_id": "int", "reason": "str?"},
    permission="library:admin", risk=RISK_MEDIUM, requires_confirmation=True,
    ticket_scope="archive_file",
    summary=(
        "把一份资料**归档**：原件与已完成分段一个字节不动，只是不再参与检索与索引重建"
        "（检索与重建都显式跳过已归档资料），并从当前 collection 删掉它的向量点。"
        "归档**可以恢复**（受控入口 `unarchive_file`，同样需要库管理员权限 + 确认）："
        "恢复会按只读原件重新建索引。此操作**不删除**任何业务记录与原件"
    ),
))

register(OperationSpec(
    "unarchive_file", "恢复归档资料（重新纳入检索）", {"file_id": "int"},
    permission="library:admin", risk=RISK_MEDIUM, requires_confirmation=True,
    ticket_scope="unarchive_file",
    summary=(
        "把一份**已归档**的资料恢复：按只读原件重新解析/分段/嵌入，写回当前生效代次，"
        "让它重新参与检索。权限与归档相同（`library:admin`），同样要一次独立确认，"
        "并留审计事件（谁、什么时候、恢复了哪一份、结果如何）。"
        "原件与业务记录一行不动；只有 `ARCHIVED` 状态才能恢复（重复恢复会如实返回"
        "`already_active`，不假装又做了一次）"
    ),
))

register(OperationSpec(
    "classify_file", "修改资料性质", {"file_id": "int", "classification": "str?"},
    permission="library:admin", risk=RISK_MEDIUM, requires_confirmation=True,
    ticket_scope="classify_file",
    summary=(
        "把一份资料的**性质**改成词表里的一个取值（申请 / 合同 / 报表 / 普通文档 / "
        "涉密文档；传空字符串表示清空）。只改这一个**元数据**字段：原件字节、分段、"
        "向量与解析状态一行都不动。需要该库管理员权限 + 一次独立确认，并写审计事件"
        "（含旧值 → 新值）。值不在词表里会被拒绝，不静默写入"
    ),
))

# --------------------------------------------------------- 破坏性（三次确认） ----
register(OperationSpec(
    "delete_file", "删除资料", {"file_id": "int"}, permission="library:admin",
    risk=RISK_DESTRUCTIVE, requires_confirmation=True, requires_destructive_flow=True,
    ticket_scope="delete_file",
    summary="三次独立确认：第 1、2 次点确认，第 3 次必须准确输入文件名",
))
register(OperationSpec(
    "delete_library", "删除信息库", {"library": "str"}, permission="system:operate",
    risk=RISK_DESTRUCTIVE, requires_confirmation=True, requires_destructive_flow=True,
    ticket_scope="delete_library",
    summary="三次独立确认：第 3 次必须准确输入信息库名称",
))
register(OperationSpec(
    "reset_demo_data", "重置演示数据", {"library": "str"}, permission="system:operate",
    risk=RISK_DESTRUCTIVE, requires_confirmation=True, requires_destructive_flow=True,
    ticket_scope="reset_demo_data",
    summary=(
        "受控重置**指定的**演示库（code 必须以 demo- 开头，且三次确认覆盖"
        "该库完整资料清单）；不做隐式全局清理"
    ),
))

register(OperationSpec(
    "reset_library_index", "重置信息库索引", {"library": "str"}, permission="system:operate",
    risk=RISK_DESTRUCTIVE, requires_confirmation=True, requires_destructive_flow=True,
    ticket_scope="reset_library_index",
    summary=(
        "三次独立确认：第 3 次必须准确输入信息库名称。**只重建分段与向量**"
        "（逐份强制重跑同一份只读原件），原件与业务记录一行不动；"
        "有资料在处理中、超过本次上限或有资料无法重建时如实报 partial，"
        "且**不清任何旧代次**"
    ),
))

# ------------------------------------------------- 会话（个人聊天记录，一次确认） ----
register(OperationSpec(
    "delete_conversation", "删除会话", {"conversation": "int"},
    permission="conversation:own", risk=RISK_MEDIUM, requires_confirmation=True,
    summary=(
        "删除自己的会话与其聊天消息（一次确认）。不销毁审计：该会话关联的"
        "受控操作记录与凭证会保留，只解除与会话的关联。"
    ),
))

# ------------------------------------------------------------ 生成类（可写新文档） ----
register(OperationSpec(
    "compose_document", "编写新文档（需选参考文件）",
    # `template_id` 是**可选**的公文模板：留空 = 不用模板（用户要求：
    # "如果用户未选择，那么就不用模板"）。它是唯一一个"不许模型自己编"的参数，
    # 说明写在 services.PARAM_DESCRIPTIONS 里，会合并进工具 schema。
    # 模型提案时可以暂不填写 sources，用户必须在确认卡上选择至少一份参考文件；
    # 确认与执行均校验，空选不能回落到全库。
    {"title": "str", "topic": "str", "sources": "refs?", "template_id": "int?"},
    permission="library:read", risk=RISK_LOW,
    summary="选择至少一份参考文件后只读生成派生文档草稿（可选用一份公文模板），不覆盖来源",
))
register(OperationSpec(
    "export_document", "导出派生文档", {"document_id": "int", "format": "str?"},
    permission="library:read", risk=RISK_LOW,
))

DESTRUCTIVE_SCOPES: frozenset[str] = frozenset(
    spec.ticket_scope for spec in OPERATIONS.values() if spec.requires_destructive_flow
)
