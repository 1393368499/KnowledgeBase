"""AI 会话、操作提案、确认凭证与派生文档。"""

# 已入库资料不被 AI 流程间接改写。写入类动作记入追加型 OperationRecord，
# 破坏性删除必须取得一次性 ActionTicket。

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.knowledge.models import KnowledgeLibrary

TOKEN_BYTES = 32
HASH_ALGORITHM = "sha256"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_token() -> tuple[str, str]:
    """返回 (明文, 摘要)。明文只回给用户一次，库里只存摘要。"""
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    return raw, hash_token(raw)


class Conversation(models.Model):
    if TYPE_CHECKING:
        user_id: int
        message_count: int

    class Source(models.TextChoices):
        WEB = "web", "网页"
        API = "api", "接口"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations")
    title = models.CharField(max_length=200, blank=True)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.WEB)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at", "-id")
        indexes = [models.Index(fields=["user", "updated_at"])]

    def __str__(self) -> str:
        return self.title or f"会话 {self.pk}"


class Message(models.Model):
    if TYPE_CHECKING:
        conversation_id: int

    class Role(models.TextChoices):
        USER = "user", "用户"
        ASSISTANT = "assistant", "助手"
        SYSTEM = "system", "系统"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField()
    intent = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)
        indexes = [models.Index(fields=["conversation", "id"])]

    def __str__(self) -> str:
        return f"{self.role}: {self.content[:40]}"


class OperationRecord(models.Model):
    """一次操作的全生命周期（追加型不可篡改记录）。"""

    class Status(models.TextChoices):
        PROPOSED = "proposed", "待确认"
        CONFIRMED_1 = "confirmed_1", "第 1 次确认"
        CONFIRMED_2 = "confirmed_2", "第 2 次确认"
        ARMED = "armed", "待执行"
        EXECUTING = "executing", "执行中"
        EXECUTED = "executed", "已执行"
        REJECTED = "rejected", "已放弃"
        EXPIRED = "expired", "已过期"
        FAILED = "failed", "执行失败"

    TERMINAL_STATUSES = frozenset({Status.EXECUTED, Status.REJECTED, Status.EXPIRED, Status.FAILED})

    #: 进入 ``EXECUTING`` 之后多久没有写回结果，就视为**僵死**（进程被杀/容器重启）。
    #:
    #: 为什么必须有这道判决：``EXECUTING`` 不是终态，而放弃（``abandon_operation``）
    #: 与再执行（CAS 要求 ``ARMED``）两条路都被状态机挡住 —— 没有超时判决，
    #: 一条被 SIGKILL 打断的操作会**永久**卡在"执行中"，用户既不能放弃也不能重试。
    EXECUTING_STALE_SECONDS = 30 * 60

    if TYPE_CHECKING:
        conversation_id: int | None
        user_id: int

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="operations", null=True, blank=True
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="operations")
    intent = models.CharField(max_length=64)
    risk = models.CharField(max_length=16, default="low")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROPOSED)
    params = models.JSONField(default=dict, blank=True)
    #: 提案时用户勾选的「本次检索范围」（信息库 ``code`` 的有序列表；空 = 不限）。
    #:
    #: 为什么必须**落在这条操作上**、而不是只留在对话/用户消息里（实测
    #: 2026-10-19）：用户在输入栏勾了「开发验收资料库」再问"谁出差了?"，模型
    #: 调了 ``search_knowledge(query="出差")`` 却**没带 ``library`` 参数** ——
    #: 勾选只收窄了背景上下文（``build_context_report``），工具执行层仍按
    #: "全部可读库"检索，于是把测试库里的资料也搜了出来。确认卡可能隔几分钟、
    #: 甚至跨一轮对话才点，所以范围必须随操作一起持久化，执行时再按它收敛。
    library_scope = models.JSONField(default=list, blank=True)
    target_ref = models.CharField(max_length=200, blank=True)
    target_label = models.CharField(max_length=300, blank=True)
    confirmations = models.PositiveSmallIntegerField(default=0)
    required_confirmations = models.PositiveSmallIntegerField(default=1)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    #: 进入 ``ARMED``（确认全部完成、可执行）的时刻。
    armed_at = models.DateTimeField(null=True, blank=True)
    #: ``ARMED`` 的**失效时刻**。执行端必须核验；过期就要重新提案。
    #:
    #: 为什么必须有它（审查指出的安全边界）：确认是一次一次做的，如果
    #: ARMED 之后可以**无限期**执行，那么"三次确认"实际上只是"某一天确认过"——
    #: 用户当时确认的目标/上下文可能在几小时后就变了（价格表换了、资料补了、
    #: 库改名了），拿旧确认去执行已经不是用户当初同意的那件事。
    armed_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["intent", "status"]),
        ]

    def armed_is_expired(self, now=None) -> bool:
        """``ARMED`` 是否已过期（没有失效时刻也视为不可执行，fail-closed）。"""
        if self.armed_expires_at is None:
            return True
        return self.armed_expires_at <= (now or timezone.now())

    def executing_is_stale(self, now=None) -> bool:
        """``EXECUTING`` 是否已僵死（超过 ``EXECUTING_STALE_SECONDS`` 没写回结果）。

        参照点是 ``updated_at``：执行端把状态 CAS 到 ``EXECUTING`` 时会显式写它
        （``planner.services.execute_operation``），所以它就是"开始执行"的时刻。
        非 ``EXECUTING`` 一律返回假 —— 这道判决只用于给卡死的执行收尾。
        """
        if self.status != self.Status.EXECUTING:
            return False
        reference = self.updated_at or self.created_at
        if reference is None:
            return True
        deadline = (now or timezone.now()) - timezone.timedelta(
            seconds=self.EXECUTING_STALE_SECONDS
        )
        return reference <= deadline

    def __str__(self) -> str:
        return f"{self.intent} #{self.pk} [{self.status}]"


class ActionTicket(models.Model):
    if TYPE_CHECKING:
        user_id: int
        operation_id: int | None
        conversation_id: int | None

    """一次性确认凭证：绑定用户 + 会话 + 操作 + 对象，用过即废。"""

    class Scope(models.TextChoices):
        CONFIRM_STEP_1 = "confirm_step_1", "第 1 次确认"
        CONFIRM_STEP_2 = "confirm_step_2", "第 2 次确认"
        CONFIRM_FINAL = "confirm_final", "名称输入确认"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tickets")
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="tickets", null=True, blank=True
    )
    operation = models.ForeignKey(
        OperationRecord, on_delete=models.CASCADE, related_name="tickets", null=True, blank=True
    )
    token_hash = models.CharField(max_length=64, unique=True)
    scope = models.CharField(max_length=32, choices=Scope.choices)
    intent = models.CharField(max_length=64)
    object_ref = models.CharField(max_length=200, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    consumed_count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=["user", "expires_at"])]

    def is_expired(self, now=None) -> bool:
        now = now or timezone.now()
        return self.expires_at <= now

    def __str__(self) -> str:
        return f"{self.scope} {self.intent} #{self.pk}"


class DeleteGrant(models.Model):
    """**事务局部**的删除授权（P4 受控删除的唯一通道）。

    为什么不是"临时摘掉触发器"（审查明确否掉的做法）
    ------------------------------------------------
    ``drop_guards()`` 摘掉触发器再重建，会有两个致命问题：

    1. **存在全局窗口**：从摘掉到重建之间，任何并发请求都能删除任何资料——
       攻击者只要在窗口期发一个 DELETE 就行；
    2. 摘/建本身是 DDL，失败会留下"无保护"状态。

    正确做法是让触发器**自己去查一张授权表**：只有"同一事务里刚签发、
    尚未核销、未过期、且目标正好是这一行"的授权存在时才放行。

    为什么这样就安全
    ----------------
    ``DeleteGrant`` 由服务层在**删除的同一个事务**里插入，其他事务在提交前
    看不到它（READ COMMITTED）；事务一旦回滚，授权行也随之消失。
    所以它是一条**事务局部**的授权通道：没有全局窗口，无法被并发请求借用，
    也无法跨目标挪用（``target_kind`` + ``target_ref`` 必须精确匹配）。

    授权还会被标记 ``consumed_at``，重复删除同一个目标时不会复用。
    """

    class TargetKind(models.TextChoices):
        INGESTED_FILE = "ingested_file", "已入库资料"
        KNOWLEDGE_LIBRARY = "knowledge_library", "信息库"

    operation = models.ForeignKey(
        OperationRecord, on_delete=models.CASCADE, related_name="delete_grants"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="delete_grants"
    )
    target_kind = models.CharField(max_length=32, choices=TargetKind.choices)
    target_ref = models.CharField(max_length=200)
    reason = models.CharField(max_length=300, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "kb_delete_grant"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["target_kind", "target_ref"]),
            models.Index(fields=["expires_at"]),
        ]

    def is_expired(self, now=None) -> bool:
        return self.expires_at <= (now or timezone.now())

    def __str__(self) -> str:
        return f"grant {self.target_kind}:{self.target_ref}"


class DeletionJob(models.Model):
    """删除后的**可重试清理记录**。

    删除不是"一条 DELETE 语句"就完事：数据库行、向量点、原始字节是三处存储。
    任何一处失败都必须能被重试，并且**不能**因为"行删掉了"就报告"全部完成"。

    状态语义：

    - ``pending``   ：已授权、准备执行；
    - ``running``   ：正在清理；
    - ``partial``   ：部分完成（哪些没完成写进 ``steps``）——**可重试**；
    - ``completed`` ：数据库/向量/原件**全部**清理成功；
    - ``failed``    ：整体失败，可重试。

    判定"完成"的口径是 ``steps`` 里每一项都 ok，而不是"主记录删掉了"。
    """

    class Status(models.TextChoices):
        PENDING = "pending", "待执行"
        RUNNING = "running", "清理中"
        PARTIAL = "partial", "部分完成（可重试）"
        COMPLETED = "completed", "已完成"
        FAILED = "failed", "失败"

    operation = models.ForeignKey(
        OperationRecord, on_delete=models.CASCADE, related_name="deletion_jobs"
    )
    if TYPE_CHECKING:
        operation_id: int

    target_kind = models.CharField(max_length=32)
    target_ref = models.CharField(max_length=200)
    #: 删除前的必要信息（用于清理向量与原件）：collection、content_sha256 等
    snapshot = models.JSONField(default=dict, blank=True)
    #: 分步结果 {"database": {"ok": true, ...}, "vectors": {...}, "blob": {...}}
    steps = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "kb_deletion_job"
        ordering = ("-created_at", "-id")

    @property
    def all_steps_ok(self) -> bool:
        """``steps`` 里每一项都必须 ok。

        ``blob`` 在当前实现里按设计**保留**（内容寻址存储、可能被别的资料复用，
        ``services._cleanup_blob`` 恒返回 ok），所以把它一起纳入判定不影响今天的结果；
        但**存在就必须算**：独立审计 D3 指出，早先这里把 ``blob`` 排除在必需项之外，
        一旦 blob 步骤真的失败，作业仍会显示"已完成"，而 ``retry_deletion_job``
        的唯一 guard 正是"已完成即早退" —— 失败会被永久短路成成功。
        """
        steps = self.steps or {}
        required = {"database", "vectors"}
        if "blob" in steps:
            required.add("blob")
        for name in required:
            if not (steps.get(name) or {}).get("ok"):
                return False
        return True


class DerivedDocument(models.Model):
    """AI 生成的新文档。来源永远是已入库资料，绝不回写它们。"""

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        EDITED = "edited", "已编辑"
        EXPORTED = "exported", "已导出"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.SET_NULL, related_name="documents", null=True, blank=True
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="derived_documents"
    )
    title = models.CharField(max_length=300)
    topic = models.CharField(max_length=300, blank=True)
    content_markdown = models.TextField(blank=True)
    format_options = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    sources = models.ManyToManyField("knowledge.IngestedFile", related_name="derived_documents", blank=True)
    libraries = models.ManyToManyField(KnowledgeLibrary, related_name="derived_documents", blank=True)
    #: 撰写时套用的**公文模板**（用户上传的格式范本）。
    #:
    #: 为什么是 ``SET_NULL`` + ``template_name`` 快照，而不是 ``PROTECT``：模板是用户
    #: 自己上传的辅助资产，不是入库资料，用户应当能删掉传错的模板；但"这份文档当时
    #: 套的是哪份模板"属于**履历**，删模板不该把履历一起抹掉。所以外键置空、名字留档。
    template = models.ForeignKey(
        "DocumentTemplate",
        on_delete=models.SET_NULL,
        related_name="documents",
        null=True,
        blank=True,
    )
    if TYPE_CHECKING:
        template_id: int | None
        conversation_id: int | None

    template_name = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at", "-id")

    def __str__(self) -> str:
        return self.title


class DocumentTemplate(models.Model):
    """公文模板：用户上传的**格式范本**，撰写时只作格式参考。

    与 ``DerivedDocument`` 的区别（必须写清楚，否则很容易被当成"资料"）：
    - 模板**不是入库资料**：不入 ``IngestedFile``、不建索引、不参与检索、不写向量库，
      也不受 ``governance`` 的 append-only 保护 —— 它就是用户自己的一张范本，
      传错了要能删掉。
    - 模板的正文只用于**格式**（标题层级、条款顺序、落款、日期、称谓），
      正文内容仍然只能来自检索到的资料片段（见 ``services.compose_document``）。
    - 原件按内容寻址存在 ``<media>/templates/<sha256><后缀>``，与原始字节库
      （``data/media/raw``，受控只读）分开存放，避免把"范本"混进受控证据链。
    """

    class Category(models.TextChoices):
        OFFICIAL = "official", "公文模板"
        PRESENTATION = "presentation", "PPT模板"
        OTHER = "other", "其他模板"

    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OFFICIAL)
    description = models.CharField(max_length=1000, blank=True)
    name = models.CharField(max_length=200)
    original_name = models.CharField(max_length=300)
    suffix = models.CharField(max_length=16, blank=True)
    size_bytes = models.PositiveIntegerField(default=0)
    content_sha256 = models.CharField(max_length=64, blank=True)
    #: 相对 ``<media>/templates`` 的路径。存相对路径的理由与 ``IngestedFile.raw_path``
    #: 一致：项目目录整体搬走后记录仍然有效。
    stored_path = models.CharField(max_length=400, blank=True)
    #: 从模板里**真实提取**出来的文字（提取失败就不会有这条记录）。
    content_text = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="document_templates"
    )
    if TYPE_CHECKING:
        uploaded_by_id: int

        def get_category_display(self) -> str: ...

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")

    def __str__(self) -> str:
        return self.name


class LibraryRebuildRun(models.Model):
    """整库索引重建的**持久批次进度**（续做游标落库，不靠"再点一次碰运气"）。

    为什么必须有这张表（独立复现的缺陷）：旧实现每次执行都从库内资料列表的**第一份**
    重新开始、且一次只处理 ``RESET_LIBRARY_LIMIT=50`` 份 —— 51 份的库连续执行两次
    处理的都是同样的前 50 份，第 51 份永远进不到处理，界面却写着"可再执行一次续做"。
    续做游标只放在内存/调用参数里都不成立（进程会重启、用户会隔天再点），所以：

    * ``manifest``：本次重建**范围的指纹**（资料集合 + 内容指纹 + 已登记集合）。
      指纹变了说明确认过的范围已经不是现在这个库 → 旧 run 作废、必须重新提案，
      绝不"差不多就接着跑"。
    * ``processed_ids``：**已成功重建**的 file id（跨批次累计），续做时跳过它们。
    * ``unrebuildable``：永久无法重建的（例如缺原件）。它们**不阻塞续做游标**
      （否则整库永远到不了"游标跑完"），但**必须阻塞旧代次清理** —— 缺原件那份的
      向量可能只存在于旧代次里，清掉就等于让它彻底搜不到。两件事分开表达：
      ``cursor_done``（还有没有活可干）与 ``cleanup_allowed``（敢不敢删旧索引）。
    * ``failed``：本批次失败明细；下一批次会对"未处理"的继续，失败原因如实留痕。
    * ``owner_token`` + ``version``：**所有权 CAS**。同一库同一时刻只允许一个 RUNNING
      run（DB 层唯一约束），并且只有**持有 owner_token** 的执行者才能写进度、
      续租、清旧索引。租约过期后被接管的旧进程即使回来，也会因为 token 不匹配
      而写不进任何东西（否则它会用陈旧的进度覆盖新执行者、甚至误清新索引）。
    * ``lease_expires_at``：租约。执行中**逐份续租**（一批可能长于租约，不能在中途被接管）；
      租约未过期时第二个执行者直接拒绝，租约过期（进程被杀）才允许接管并留审计。
    * ``batches_done`` / ``scope_size``：批次进度（第几批 / 共多少份），给界面如实报账。
    """

    class Status(models.TextChoices):
        RUNNING = "running", "进行中"
        COMPLETE = "complete", "已完成"
        SUPERSEDED = "superseded", "范围已变（作废）"

    library = models.ForeignKey(
        KnowledgeLibrary, on_delete=models.CASCADE, related_name="rebuild_runs"
    )
    if TYPE_CHECKING:
        library_id: int

    operation = models.ForeignKey(
        "OperationRecord",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rebuild_runs",
        verbose_name="最近一次执行的提案",
    )
    manifest = models.CharField(max_length=64, verbose_name="范围指纹")
    scope_size = models.PositiveIntegerField(default=0, verbose_name="本次范围份数")
    batch_size = models.PositiveIntegerField(default=50, verbose_name="每批上限")
    batches_done = models.PositiveIntegerField(default=0, verbose_name="已完成批次数")
    processed_ids = models.JSONField(default=list, blank=True, verbose_name="已重建资料 id")
    failed = models.JSONField(default=list, blank=True, verbose_name="失败明细")
    unrebuildable = models.JSONField(default=list, blank=True, verbose_name="不可重建明细")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.RUNNING, verbose_name="状态"
    )
    note = models.CharField(max_length=300, blank=True, default="", verbose_name="作废/接管原因")
    #: 所有权令牌：只有持有它的执行者才能写进度/续租/清旧索引（CAS）。
    owner_token = models.CharField(max_length=64, blank=True, default="", verbose_name="所有者令牌")
    #: 进度版本：每次成功写进度 +1，便于审计"谁在什么时候推进过"。
    version = models.PositiveIntegerField(default=0, verbose_name="进度版本")
    started_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True, verbose_name="租约到期")

    class Meta:
        ordering = ("-updated_at", "-id")
        constraints = [
            # 并发重入保护的**硬约束**：同一库同一时刻只能有一个 RUNNING run。
            # 放在数据库层，跨进程/跨请求都拦得住（应用层"先查再写"会竞态）。
            models.UniqueConstraint(
                fields=["library"],
                condition=models.Q(status="running"),
                name="uniq_running_rebuild_per_library",
            ),
        ]
        indexes = [models.Index(fields=["library", "status"], name="kb_rebuild_lib_status")]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"rebuild#{self.pk} {self.library_id} {self.status} {self.batches_done}批"

    @property
    def processed_total(self) -> int:
        return len(self.processed_ids or [])

    @property
    def lease_expired(self) -> bool:
        if self.status != self.Status.RUNNING:
            return False
        if self.lease_expires_at is None:
            return True
        return self.lease_expires_at <= timezone.now()
