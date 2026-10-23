"""知识库领域模型。

需求 7 的"绝不能修改已入库信息"在这里体现为**结构约束**：
`IngestedFile` 只有元数据与解析状态，没有"可编辑正文"；正文以派生只读分段
`DocumentChunk` 存在。`IngestedFile` / `DocumentChunk` 都登记为 append-only 模型。
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from governance.immutable import ImmutableRecordError
from governance.registry import (
    ImmutableManager,
    controlled_save,
    guard_instance_delete,
    guard_instance_save,
)


class LibraryLevel(models.TextChoices):
    INTERNAL = "internal", "内部信息库"
    EXTERNAL = "external", "外部信息库"


class ParseStatus(models.TextChoices):
    PENDING = "pending", "待处理"
    RUNNING = "running", "处理中"
    DONE = "done", "已完成"
    FAILED = "failed", "失败"
    ARCHIVED = "archived", "已归档"


class MemberRole(models.TextChoices):
    READER = "reader", "只读"
    WRITER = "writer", "可写入"
    ADMIN = "admin", "库管理员"


class FileNature(models.TextChoices):
    """资料性质词表（用户 2026-10-16 明确要求这五个取值）。

    为什么需要固定词表：`classification` 一直是个自由文本字段，而**上传入口从来
    没有写过它** —— 于是列表里的「性质」永远是「—」，搜索页的「资料性质」筛选也
    筛不出任何东西。这里给出业务确认的五个取值：
    上传时可选、列表里可按行走受控操作（`classify_file`）修改。

    **故意不给字段加 `choices=`**：历史库里可能已经存在别的值（例如接口直接
    写入的「技术文档」，`tests/test_api_basics.py` 锁着这个行为），加 choices 会把
    既有数据判成非法。词表约束的是**新的受控写法**，不是既有数据的合法性。
    """

    APPLICATION = "申请", "申请"
    CONTRACT = "合同", "合同"
    REPORT = "报表", "报表"
    ORDINARY = "普通文档", "普通文档"
    CONFIDENTIAL = "涉密文档", "涉密文档"


class ClassificationSource(models.TextChoices):
    """性质是**怎么来的**（用户 2026-10-16 要求：自动识别 + 人工纠正并存）。

    为什么必须单独记来源（不能只看值）：

    - **人工优先**：人工用五项下拉设置过之后，任何自动识别/重试/重建都**不得**
      覆盖它。判定"是不是人工设置的"只能靠这个字段 —— 值本身分不出来
      （人工选的「合同」和规则识别出的「合同」长得一模一样）；
    - **可解释**：详情页要能回答"这个性质凭什么"，规则判定与模型判定必须分得开；
    - **失败如实**：识别失败时性质保持/置为"待识别"（空值），失败原因写在
      :attr:`IngestedFile.classification_note`，**不能**把失败强填成「普通文档」。
    """

    UNKNOWN = "", "待识别"
    AUTO_RULE = "auto_rule", "规则自动识别"
    AUTO_MODEL = "auto_model", "模型自动识别"
    MANUAL = "manual", "人工设置"


#: 「文件类型」下拉的几类（用户 2026-10-16 要求至少 PDF / Excel / Word；
#: 同一天又要求 .txt 显示成「文本文件」而不是笼统的「其他」）。
#: 键是 API 参数取值（`file_type=pdf|excel|word|text`）；值是数据库 `file_type` 列里
#: 实际会出现的小写扩展名（`conftest.make_file` 与上传接口都写扩展名）。
#: 图片格式（.png/.jpg/…）用户明确说不会上传，所以**不单列一类**，落到「其他」。
FILE_TYPE_GROUPS: dict[str, tuple[str, ...]] = {
    "pdf": ("pdf",),
    "excel": ("xls", "xlsx", "xlsm", "xlsb", "csv"),
    "word": ("doc", "docx", "rtf", "odt"),
    "text": ("txt",),
}

#: 分组显示名（含「其他」：未落在上述几组里的扩展名）。
FILE_TYPE_GROUP_LABELS: dict[str, str] = {
    "pdf": "PDF",
    "excel": "Excel",
    "word": "Word",
    "text": "文本文件",
    "other": "其他",
}


def file_type_group(file_type: str) -> str:
    """把一份资料的 `file_type` 归到 PDF / Excel / Word / 文本文件 / other 之一。

    「文件类型」列与「文件类型」下拉共用这一个函数，避免两处口径漂移
    （列表显示 Word、下拉筛 Excel 这类自相矛盾的界面）。
    """
    value = str(file_type or "").strip().lower().lstrip(".")
    for group, extensions in FILE_TYPE_GROUPS.items():
        if value in extensions:
            return group
    return "other"


class KnowledgeLibrary(models.Model):
    """信息库。搜索筛选、权限判定、向量 collection 都以它为维度。"""

    code = models.SlugField(max_length=64, unique=True, verbose_name="库编码")
    name = models.CharField(max_length=120, verbose_name="库名称")
    level = models.CharField(
        max_length=16, choices=LibraryLevel.choices, default=LibraryLevel.INTERNAL
    )
    description = models.CharField(max_length=400, blank=True, default="")
    department = models.CharField(max_length=120, blank=True, default="", verbose_name="归属部门")
    customer = models.CharField(max_length=160, blank=True, default="", verbose_name="归属客户")
    vector_collection = models.CharField(max_length=120, blank=True, default="")
    #: 当前生效的向量 generation。重建会 +1 并生成新的 collection 名，
    #: 旧 generation 的 collection 保留可回退（见 apps/knowledge/vectorindex.py）。
    active_generation = models.IntegerField(default=0)
    #: **单调递增**的 generation 计数器：下一个可用编号从这里取，只增不减。
    #: 为什么不能只用 ``active_generation + 1``（审查意见 5）：
    #: - 尚未激活时连续两次 prepare 会算出同一个编号，两个构建撞到同一个 collection；
    #: - 回退到旧 generation 后，再 prepare 会重新用已经被占用的编号，
    #:   把"旧的、已经建好的索引"当成新构建的目标去写。
    #: 单靠 active_generation 无法表达"这个编号历史上用过没有"，所以单独记一个计数器。
    next_generation = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "kb_knowledge_library"
        ordering = ["id"]
        verbose_name = "信息库"
        verbose_name_plural = "信息库"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name}({self.code})"

    def member_role(self, user) -> str | None:
        """返回该用户在此库中的角色；系统管理员视为 admin。"""
        if not getattr(user, "is_authenticated", False):
            return None
        if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
            return MemberRole.ADMIN
        membership = self.members.filter(user=user).only("role").first()
        return membership.role if membership else None


class LibraryMember(models.Model):
    library = models.ForeignKey(KnowledgeLibrary, related_name="members", on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=16, choices=MemberRole.choices, default=MemberRole.READER)
    department = models.CharField(max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "kb_library_member"
        constraints = [
            models.UniqueConstraint(fields=["library", "user"], name="uniq_library_member"),
        ]
        verbose_name = "信息库成员"
        verbose_name_plural = "信息库成员"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.user}@{self.library.code}:{self.role}"


class IngestedFile(models.Model):
    """一份已入库资料（append-only）。

    - 没有正文可写字段：解析结果只进 `DocumentChunk`，从结构上排除"改内容"。
    - 状态字段只能通过 `update_parse_status()` 变更（服务层会写审计事件）。
    """

    library = models.ForeignKey(
        KnowledgeLibrary, related_name="files", on_delete=models.PROTECT, verbose_name="信息库"
    )
    original_name = models.CharField(max_length=255, verbose_name="文件名")
    content_sha256 = models.CharField(max_length=64, verbose_name="内容指纹")
    size_bytes = models.BigIntegerField(default=0)
    file_type = models.CharField(max_length=16, blank=True, default="")
    directory_path = models.CharField(max_length=400, blank=True, default="", verbose_name="目录")
    #: 资料性质（受控词表见 :class:`FileNature`）。**受控可变**：入库时可选，
    #: 之后只能走 `classify_file` 受控操作（库管理员 + 一次确认 + 审计）修改。
    #: 2026-10-16 起**主要由正文自动识别**写入（见 ``apps.knowledge.classification``）：
    #: 识别失败保持/置为空（"待识别"），失败原因见 ``classification_note``。
    classification = models.CharField(max_length=60, blank=True, default="", verbose_name="性质")
    #: 性质由谁写入：``""``（待识别）/ ``auto_rule``（规则）/ ``auto_model``（模型）
    #: / ``manual``（人工）。**只有 manual 是不可被自动流程覆盖的**。
    classification_source = models.CharField(
        max_length=16, blank=True, default="", verbose_name="性质来源"
    )
    #: 识别依据或失败原因（≤300 字，供详情页与排查用）。
    classification_note = models.CharField(
        max_length=300, blank=True, default="", verbose_name="性质判定说明"
    )
    customer = models.CharField(max_length=160, blank=True, default="")
    department = models.CharField(max_length=120, blank=True, default="")
    #: 合同/资料金额（万元）。原型搜索页有"金额下限"这一项筛选，
    #: 因此这里必须有真实字段：**不能用"前端在已取回的一页里过滤"来顶替**，
    #: 那样统计会说谎。入库时可按需填写；未填为 NULL（不参与金额下限筛选）。
    amount_wan = models.FloatField(
        null=True, blank=True, db_index=True, verbose_name="金额（万元）"
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    parse_status = models.CharField(
        max_length=16, choices=ParseStatus.choices, default=ParseStatus.PENDING, db_index=True
    )
    parse_error = models.CharField(max_length=400, blank=True, default="")
    #: 已失败/已尝试处理次数；用于"可重试但不能无限重试"的闸门
    parse_attempts = models.IntegerField(default=0)
    #: 尝试次数上限，超过后必须人工介入（避免坏文件把队列打满）
    MAX_PARSE_ATTEMPTS = 5
    #: 原始字节在 raw_store 里的相对路径（内容寻址，见 apps.knowledge.rawstore）
    raw_path = models.CharField(max_length=400, blank=True, default="")
    #: 原始字节落盘后的实际字节数（与 size_bytes 相互印证）
    raw_size_bytes = models.BigIntegerField(default=0)
    #: 已经成功写入向量的分段数（< chunk_count 时说明向量索引不完整）
    embedded_chunk_count = models.IntegerField(default=0)
    #: 该资料当前生效的向量 generation；换模型重建时会变
    vector_generation = models.IntegerField(default=0)
    chunk_count = models.IntegerField(default=0)
    vector_collection = models.CharField(max_length=120, blank=True, default="")
    vector_point_id = models.CharField(max_length=120, blank=True, default="")
    #: 该资料在 Qdrant 里的真实 collection 名（含 generation 后缀）。
    #: 为什么不复用 ``vector_collection``：那是库级的逻辑名，而这里要回答
    #: "这份资料的向量到底写在哪一版索引里"，换模型重建时两者会分叉。
    vector_collection_name = models.CharField(max_length=160, blank=True, default="")
    #: 向量写入状态：pending / partial / indexed / failed。partial 表示
    #: 只写了一部分分段（写索引中断），重试时按已写入数量续写，不推倒重来。
    vector_status = models.CharField(max_length=16, blank=True, default="pending")
    #: 处理租约（数据库 CAS）：同一份资料不能被两条流水线同时处理。
    #: 为什么必须落库：队列模式下有多个 worker，进程内锁互相看不见；
    #: ``parse_lease_expires_at`` 让崩溃的 worker 留下的租约能被接管。
    #: 占用/释放都是"带 token 条件"的 UPDATE，旧 worker 醒来不会覆盖新 worker。
    parse_lease_token = models.CharField(max_length=64, blank=True, default="", db_index=True)
    parse_lease_started_at = models.DateTimeField(null=True, blank=True)
    parse_lease_expires_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=32, default="manual", verbose_name="来源")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    parsed_at = models.DateTimeField(null=True, blank=True)

    objects = ImmutableManager()

    # 允许通过受控领域方法变更的状态类字段
    # 注意：这张表必须与 governance.registry.CONTROLLED_MUTABLE_FIELDS 完全一致——
    # 登记表放行而模型不放行，流水线就只能绕过守卫写库，等于把不可篡改保证捅穿。
    #
    # 2026-10-16 新增 `classification`：性质是**元数据**（原件内容与分段一个字节都
    # 不动），原先被数据库触发器锁死、上传入口又从不写它，结果列表里永远显示「—」，
    # 用户既看不到也改不了。放开它必须同时满足三条：受控领域方法写（`classify_file`
    # 操作：库管理员 + 一次确认）、每次写审计事件、并刷新数据库触发器
    # （migration 0018），否则会出现「ORM 放行、触发器还在挡」的漂移。
    CONTROLLED_FIELDS = frozenset(
        {
            "classification",
            # 2026-10-16：性质自动识别引入的两个可查看元数据。它们与
            # `classification` 必须**同一次写入**（否则会出现"值变了、来源没变"
            # 的错账），所以一并纳入受控通道；它们同样是元数据，不碰原件与分段。
            "classification_source",
            "classification_note",
            "parse_status",
            "parse_error",
            "parse_attempts",
            "chunk_count",
            "embedded_chunk_count",
            "raw_path",
            "raw_size_bytes",
            "vector_collection",
            "vector_collection_name",
            "vector_point_id",
            "vector_generation",
            "vector_status",
            "parse_lease_token",
            "parse_lease_started_at",
            "parse_lease_expires_at",
            "parsed_at",
        }
    )

    # 合法状态流转：只允许"向前"，不允许回到初始态。
    # DONE → RUNNING 是**合法**的：换模型需要全量重嵌入，那是重建而不是篡改
    # （原始字节与已完成分段都不动，只重算向量）。
    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        ParseStatus.PENDING: {ParseStatus.RUNNING, ParseStatus.FAILED},
        ParseStatus.RUNNING: {ParseStatus.DONE, ParseStatus.FAILED},
        ParseStatus.DONE: {ParseStatus.ARCHIVED, ParseStatus.RUNNING},
        ParseStatus.FAILED: {ParseStatus.RUNNING},
        # **归档可以恢复**（受控入口：planner 的 `unarchive_file`，需要库管理员权限 +
        # 确认 + 审计）：恢复 = 重新按只读原件建索引，状态从"已归档"回到"处理中"，
        # 再走到 DONE。这是**唯一**的反归档通道，仍受状态机与不可篡改触发器约束。
        ParseStatus.ARCHIVED: {ParseStatus.RUNNING},
    }

    class Meta:
        db_table = "kb_ingested_file"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["library", "content_sha256"], name="uniq_file_per_library_hash"
            ),
        ]
        indexes = [
            models.Index(fields=["library", "parse_status"]),
            models.Index(fields=["created_at"]),
        ]
        verbose_name = "已入库资料"
        verbose_name_plural = "已入库资料"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.original_name}@{self.library.code}"

    # ---------------------------------------------------------------- 不可篡改 ----
    def save(self, *args, **kwargs):  # type: ignore[override]
        # 不看 _state.adding：它不能证明"库里没有这一行"。
        # 构造 IngestedFile(pk=<已存在>) 再 save() 会走 UPDATE，必须在这里挡住。
        guard_instance_save(self, _controlled_write=kwargs.pop("_controlled_write", False))
        if self.pk is None and not self.content_sha256:
            raise ImmutableRecordError(
                "knowledge.IngestedFile", "创建", "缺少内容指纹 content_sha256"
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # type: ignore[override]
        guard_instance_delete(self)
        return super().delete(*args, **kwargs)

    def update_parse_status(
        self,
        new_status: str,
        *,
        error: str = "",
        chunk_count: int | None = None,
        embedded_chunk_count: int | None = None,
        vector_collection: str | None = None,
        vector_collection_name: str | None = None,
        vector_generation: int | None = None,
        vector_status: str | None = None,
        vector_point_id: str | None = None,
        bump_attempts: bool = False,
    ) -> None:
        """受控状态流转：只允许写登记表声明的字段，且必须合法流转。"""
        fields = self._prepare_controlled_fields(
            new_status,
            error=error,
            chunk_count=chunk_count,
            embedded_chunk_count=embedded_chunk_count,
            vector_collection=vector_collection,
            vector_collection_name=vector_collection_name,
            vector_generation=vector_generation,
            vector_status=vector_status,
            vector_point_id=vector_point_id,
            bump_attempts=bump_attempts,
        )
        controlled_save(self, fields=list(fields))

    def _prepare_controlled_fields(
        self,
        new_status: str,
        *,
        error: str = "",
        chunk_count: int | None = None,
        embedded_chunk_count: int | None = None,
        vector_collection: str | None = None,
        vector_collection_name: str | None = None,
        vector_generation: int | None = None,
        vector_status: str | None = None,
        vector_point_id: str | None = None,
        bump_attempts: bool = False,
    ) -> dict:
        """校验状态流转、把值写到实例上，返回 ``{字段名: 值}``（**不落库**）。

        为什么要拆出来：队列 worker 需要用**一条 CAS UPDATE**
        （``WHERE lease_token = ? AND expires_at > now AND parse_status = ?``）
        原子地完成"核对 + 写入"，不能"先检查再无条件更新"（那是 TOCTOU）。
        所以把"算哪些字段、值是什么、流转合不合法"这部分纯逻辑抽出来共用，
        真正的落库方式由调用方决定（受控单行更新 or 带租约条件的 CAS）。

        同一状态下的元数据增量更新是合法的（例如 RUNNING 期间记
        ``vector_status='indexed'``）：那时不碰 ``parse_error``，也不允许
        ``bump_attempts``。
        """
        same_status = new_status == self.parse_status
        if not same_status:
            allowed = self.ALLOWED_TRANSITIONS.get(self.parse_status, set())
            if new_status not in allowed:
                raise ImmutableRecordError(
                    "knowledge.IngestedFile",
                    "状态流转",
                    f"{self.parse_status} → {new_status} 不是合法流转",
                )
        self.parse_status = new_status
        # 元数据增量更新时保持原有 parse_error 不变；否则按本次传入重置。
        self.parse_error = self.parse_error if same_status else (error or "")[:400]
        fields = ["parse_status"] if same_status else ["parse_status", "parse_error"]
        if bump_attempts:
            if same_status:
                raise ImmutableRecordError(
                    "knowledge.IngestedFile",
                    "状态流转",
                    "同状态调用不允许递增尝试次数（次数只随真正的重试递增）",
                )
            self.parse_attempts = (self.parse_attempts or 0) + 1
            fields.append("parse_attempts")
        if chunk_count is not None:
            self.chunk_count = chunk_count
            fields.append("chunk_count")
        if embedded_chunk_count is not None:
            self.embedded_chunk_count = embedded_chunk_count
            fields.append("embedded_chunk_count")
        if vector_collection is not None:
            self.vector_collection = vector_collection
            fields.append("vector_collection")
        if vector_collection_name is not None:
            self.vector_collection_name = vector_collection_name
            fields.append("vector_collection_name")
        if vector_generation is not None:
            self.vector_generation = vector_generation
            fields.append("vector_generation")
        if vector_status is not None:
            self.vector_status = vector_status
            fields.append("vector_status")
        if vector_point_id is not None:
            self.vector_point_id = vector_point_id
            fields.append("vector_point_id")
        if new_status == ParseStatus.DONE:
            self.parsed_at = timezone.now()
            fields.append("parsed_at")
        # 返回"字段 → 值"：调用方决定怎么落库
        # （受控单行更新走 controlled_save；队列 worker 走带租约条件的 CAS）
        return {name: getattr(self, name) for name in fields}

    @property
    def permission_code(self) -> str:
        """写进向量 payload 的权限码，检索时必须带上同一个码做过滤。"""
        return f"library:{self.library_id}"


class DocumentChunk(models.Model):
    """解析出的只读分段（派生数据，随资料产生）。"""

    file = models.ForeignKey(IngestedFile, related_name="chunks", on_delete=models.CASCADE)
    ordinal = models.IntegerField()
    text = models.TextField()
    page = models.IntegerField(null=True, blank=True)
    token_estimate = models.IntegerField(default=0)
    permission_code = models.CharField(max_length=120, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableManager()

    class Meta:
        db_table = "kb_document_chunk"
        ordering = ["file_id", "ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["file", "ordinal"], name="uniq_chunk_ordinal"),
        ]
        verbose_name = "资料分段"
        verbose_name_plural = "资料分段"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.file_id}#{self.ordinal}"

    def save(self, *args, **kwargs):  # type: ignore[override]
        guard_instance_save(self, _controlled_write=kwargs.pop("_controlled_write", False))
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # type: ignore[override]
        guard_instance_delete(self)
        return super().delete(*args, **kwargs)


class VectorCollection(models.Model):
    """向量集合登记表：collection 名 → 维度 → 嵌入模型。

    换模型时**只新建** collection，不原地改维度；``is_active`` 是切换开关，
    同一个库最多只有一个 active，切换在一个事务里完成（见
    :mod:`apps.knowledge.vectorindex`）。
    """

    name = models.CharField(max_length=120, unique=True)
    dimension = models.IntegerField(default=1024)
    embedding_model = models.CharField(max_length=120, default="bge-large-zh-v1.5")
    #: 嵌入 profile 名（如 bge1024 / bge2048）
    profile = models.CharField(max_length=64, blank=True, default="")
    #: 模型版本，写进 collection 名的同时留档，便于回退时辨认
    model_version = models.CharField(max_length=64, blank=True, default="")
    #: **端点快照**：这一代索引建立时使用的嵌入服务地址。
    #:
    #: 为什么必须存：换模型/换服务后，旧代次的查询与"增量写入"都必须回到
    #: **它自己那一代**登记的端点与维度；只按全局 active profile 取模型，
    #: 会出现"旧集合被新模型查询/写入"的串模型事故（见 embedding.profile_for_registration）。
    base_url = models.CharField(max_length=300, blank=True, default="")
    library = models.ForeignKey(
        KnowledgeLibrary, null=True, blank=True, related_name="collections", on_delete=models.SET_NULL
    )
    storage_mode = models.CharField(
        max_length=16, default="server", choices=[("server", "Qdrant server"), ("embedded", "本地嵌入")]
    )
    storage_path = models.CharField(max_length=400, blank=True, default="")
    #: 是否当前活跃。同一库内最多一个为真（见 UniqueConstraint）
    is_active = models.BooleanField(default=False)
    #: 构建状态：``building``（已占号、远端 collection 未必建好）/
    #: ``ready``（远端建好、可以写入）/ ``failed``（远端失败，编号已作废不再复用）。
    #:
    #: 为什么必须有它：占号与"在远端建 collection"是两个不同存储上的动作，
    #: 没法放进同一个原子事务。先把编号**持久化**下来，再去建远端；
    #: 远端失败就把这行标成 failed，编号**作废不复用**（避免复用孤立的
    #: collection 名把新数据写进一处没人知道的索引）。切换只认 ``ready``。
    build_status = models.CharField(
        max_length=16,
        default="ready",
        choices=[("building", "构建中"), ("ready", "就绪"), ("failed", "构建失败")],
    )
    #: 构建失败的说明（排查用；成功时为空）
    build_error = models.CharField(max_length=400, blank=True, default="")
    #: 最近一次全量重建写入的分段数
    chunk_count = models.IntegerField(default=0)
    #: **退役待删**标记（持久"删除状态锁"）：非空 = 这个代次已确定要删，只是远端
    #: collection 还没删掉/删失败。语义是**可重试**：
    #:   * `activate_generation` / `rollback_generation` 必须**明确拒绝**该状态
    #:     （绝不能把"正在删"的代次切成生效代次）；
    #:   * 远端删除**成功**才删掉这条登记行；
    #:   * 远端删除**失败**时登记行保留（唯一资源不丢），下次重建/清理入口会
    #:     重新捡起它重试，界面也据此如实提示"待删除/上次删除失败"。
    #:
    #: 为什么不直接"先删登记、再删远端"（曾被独立审查驳回）：drop 失败后登记
    #: 没了，后续任何入口都再也找不到这个 collection，而 `kbvacuum` 刻意**只报告**
    #: 未登记集合不删 —— 那就变成一个**永远无法回收的孤儿**。
    retiring_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "kb_vector_collection"
        ordering = ["-is_active", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["library"],
                condition=models.Q(is_active=True),
                name="uniq_active_collection_per_library",
            ),
            # 同一个库里的 collection 名不允许重复登记：
            # generation 分配一旦出错（并发或回退后复用），这里会直接拒绝，
            # 而不是安静地覆盖掉一条历史记录（审查意见 5）。
            models.UniqueConstraint(
                fields=["library", "name"], name="uniq_collection_name_per_library"
            ),
        ]
        verbose_name = "向量集合"
        verbose_name_plural = "向量集合"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name}({self.dimension})"

    @property
    def model_label(self) -> str:
        """人读的"模型 + 版本 + 维度"，用于日志与前端展示。"""
        version = self.model_version or "-"
        return f"{self.embedding_model or '-'}@{version}:{self.dimension}"

class LibraryWriteGate(models.Model):
    """库级写入闸门：重建/换模型期间对**本库所有写路径**的排他冻结与冲突拒绝。

    为什么需要它（审查方复核结论）：``activate_generation`` 只锁 ``KnowledgeLibrary``
    行，挡不住并发 ingest/解析/嵌入/归档删除等**实际写入者**——它们不锁那一行，
    于是会出现"切换刚发布、旧 writer 又把数据写回旧代次"的静默分叉。

    可证明性来自三条纪律，缺一不可：

    1. **所有写路径都在自己的写事务里** ``select_for_update`` 锁本行，再校验
       ``frozen_at is None``；**禁止**"先查冻结、再去写"的两段式（那有 TOCTOU 窗口）。
    2. **冻结方在同一事务里先锁本行**再写冻结节；因此"冻结生效后仍有人写"在数据库
       层面不可能发生（后来者会阻塞在行锁上，拿到锁时必然看到冻结）。
    3. **fencing**：``fence_token`` 冻结时刷新；任何在冻结前取得许可、又在冻结后
       才提交的写入者，凭旧 token 会被拒绝，不会覆盖新代次的结果。

    注意：现有 ``parse_lease`` 租约**不可被重建抢占**——重建遇到活跃租约必须
    **显式拒绝**并保留旧 job（见 ``apps.knowledge.writegate.assert_no_active_leases``）。
    """

    library = models.OneToOneField(
        KnowledgeLibrary, on_delete=models.CASCADE, related_name="write_gate"
    )
    #: 非空 = 本库处于排他冻结（重建/切换窗口）
    frozen_at = models.DateTimeField(null=True, blank=True)
    frozen_by = models.CharField(max_length=120, blank=True, default="")
    freeze_reason = models.CharField(max_length=200, blank=True, default="")
    #: 冻结代号：写入者必须携带并匹配，防止"冻结前取得许可、冻结后才提交"
    fence_token = models.CharField(max_length=64, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "kb_library_write_gate"
        verbose_name = "库写入闸门"
        verbose_name_plural = "库写入闸门"

    def __str__(self) -> str:  # pragma: no cover
        state = "frozen" if self.frozen_at else "open"
        return f"gate(library={self.library_id},{state})"


class SearchFeedback(models.Model):
    """检索命中的人工反馈（"准确 / 不准确"）。

    为什么单独建表而不是往 IngestedFile 上加字段：
    - 一条资料可能在**很多次**检索里被判准确/不准确，需要留历史；
    - 反馈属于"运营信号"，改动它**绝不能**触碰源资料（不可变硬约束），
      单独建表可以让它完全独立于 immutable 保护范围。

    为什么不做"训练"：本系统的学习规则只做**可审计的排序权重调整**，
    不修改源文档、不重写向量；因此反馈表是"权重来源"而非训练集。
    """

    class Verdict(models.TextChoices):
        ACCURATE = "accurate", "准确"
        INACCURATE = "inaccurate", "不准确"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="kb_search_feedback",
        verbose_name="反馈人",
    )
    #: 触发本次反馈的查询文本（原样留存，便于复盘）
    query = models.CharField(max_length=512, verbose_name="查询词")
    #: 被判定的命中：允许为空（例如"整次检索都没有有用结果"的反馈）
    file = models.ForeignKey(
        IngestedFile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="search_feedback",
        verbose_name="命中的资料",
    )
    #: 命中在结果里的分片序号（0 表示未指明具体分片）
    chunk_ordinal = models.PositiveIntegerField(default=0, verbose_name="分片序号")
    verdict = models.CharField(
        max_length=16, choices=Verdict.choices, verbose_name="判定"
    )
    #: 结构化检索条件（部门/客户/性质/金额/库等），与 SearchView 接受的字段一致
    conditions = models.JSONField(default=dict, blank=True, verbose_name="检索条件")
    note = models.TextField(blank=True, default="", verbose_name="备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="反馈时间")
    #: 分诊留痕：谁在什么时候把这条反馈标记为"已处理"。
    #: **只做分诊留痕，不改变任何权重**——检索加权只看 verdict 的净分，
    #: 「净分 / 待处理反馈 / 重扫未处理反馈」的口径也完全不受它影响
    #: （标记已处理不会让资料"变准"，只是运营上不再重复盯着同一条批注）。
    handled_at = models.DateTimeField(null=True, blank=True, verbose_name="处理时间")
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="kb_search_feedback_handled",
        verbose_name="处理人",
    )

    class Meta:
        verbose_name = "检索反馈"
        verbose_name_plural = "检索反馈"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="kb_fb_user_time"),
            models.Index(fields=["verdict"], name="kb_fb_verdict"),
            models.Index(fields=["file"], name="kb_fb_file"),
        ]
        constraints = [
            # 同一个人对"同一次命中的同一个分片"只保留一条判定：
            # 重复提交应当是"改判"而不是堆一堆互相矛盾的记录。
            models.UniqueConstraint(
                fields=["user", "query", "file", "chunk_ordinal"],
                name="uniq_feedback_per_hit",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.user_id}:{self.verdict}:{self.query[:20]}"


class PatrolRun(models.Model):
    """一次巡检（``knowledge.patrol``）的**真实结果留痕**。

    为什么需要单独留痕：巡检过去只把结论写进 worker 日志 —— 日志会滚动、也无法在
    界面上复盘，于是「解析看门狗 / 结果校验 / 双阶段补扫」三张状态卡长期只能显示
    "暂无状态"。把每次巡检的结果**追加写**一行之后，这三项就有了**可复盘的判定来源**。

    约束：
      * 只追加，不修改历史记录（它是运维留痕，不是业务数据）；
      * 巡检本身是**只读**的（见 ``config/celery.py`` 的 ``patrol``），
        留痕不会碰任何资料。
    """

    #: 触发来源：``beat``（周期任务）/ ``manual``（manage.py kbpatrol）
    source = models.CharField(max_length=16, default="beat", verbose_name="触发来源")
    #: 本次**实际检查**的资料份数
    checked = models.PositiveIntegerField(default=0, verbose_name="检查资料数")
    #: 发现的问题总数（counts 四项之和）
    issues = models.PositiveIntegerField(default=0, verbose_name="问题总数")
    #: 分项计数：{"failed_exhausted": n, "stalled": n, "vector_incomplete": n, "blob_missing": n}
    counts = models.JSONField(default=dict, blank=True, verbose_name="分项计数")
    #: 问题明细（每项最多留前 20 条，供复盘；不做全文留档）
    findings = models.JSONField(default=dict, blank=True, verbose_name="问题明细")
    #: 巡检耗时（毫秒）
    duration_ms = models.PositiveIntegerField(default=0, verbose_name="耗时（毫秒）")
    coverage = models.JSONField(default=dict, blank=True, verbose_name="检查范围与轮转位置")
    created_at = models.DateTimeField(
        auto_now_add=True, db_index=True, verbose_name="巡检时间"
    )

    class Meta:
        verbose_name = "巡检记录"
        verbose_name_plural = "巡检记录"
        ordering = ["-created_at", "-id"]

    @property
    def status(self) -> str:
        """返回 "ok"（没有问题）或 "issues"（有需要人看的项）。"""
        return "ok" if self.issues == 0 else "issues"

    def __str__(self) -> str:  # pragma: no cover
        return f"patrol@{self.created_at:%Y-%m-%d %H:%M:%S} issues={self.issues}"

