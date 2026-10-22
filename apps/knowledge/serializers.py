"""知识库序列化器。"""

from __future__ import annotations

import math

from rest_framework import serializers

from .models import (
    FILE_TYPE_GROUP_LABELS,
    DocumentChunk,
    IngestedFile,
    KnowledgeLibrary,
    LibraryMember,
    VectorCollection,
    file_type_group,
)


class LibrarySerializer(serializers.ModelSerializer):
    file_count = serializers.IntegerField(read_only=True)
    member_count = serializers.IntegerField(read_only=True)
    level_label = serializers.CharField(source="get_level_display", read_only=True)
    my_role = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeLibrary
        fields = [
            "id",
            "code",
            "name",
            "level",
            "level_label",
            "description",
            "department",
            "customer",
            "vector_collection",
            "is_active",
            "created_at",
            "updated_at",
            "file_count",
            "member_count",
            "my_role",
        ]

    def get_my_role(self, obj: KnowledgeLibrary) -> str | None:
        request = self.context.get("request")
        if request is None:
            return None
        return obj.member_role(request.user)


class LibraryMemberSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.get_username", read_only=True)
    role_label = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = LibraryMember
        fields = ["id", "library", "user", "username", "role", "role_label", "department", "created_at"]
        read_only_fields = ["created_at"]


class IngestedFileSerializer(serializers.ModelSerializer):
    library_name = serializers.CharField(source="library.name", read_only=True)
    library_code = serializers.CharField(source="library.code", read_only=True)
    status_label = serializers.CharField(source="get_parse_status_display", read_only=True)
    #: 「文件类型」列显示的是 **PDF / Excel / Word / 其他** 三类（用户 2026-10-16
    #: 要求），而不是数据库里的裸扩展名（`pdf`/`xlsx`…）。分组口径由服务端
    #: `models.file_type_group` 唯一决定：前端不重复一份映射，否则列表显示 Word、
    #: 下拉筛 Excel 这类自相矛盾的界面迟早会出现。
    file_type_group = serializers.SerializerMethodField()
    file_type_label = serializers.SerializerMethodField()
    #: 给搜索结果卡用的**安全可读**摘要：只取已入库分段的前若干字符，
    #: 不含内部路径/异常细节，普通用户也能看。
    summary = serializers.SerializerMethodField()
    size_display = serializers.SerializerMethodField()
    #: 处理失败的**技术细节**只给管理员（普通用户只需要知道"处理失败"）。
    #:
    #: 为什么必须在**服务端**隐藏：前端过滤等于内容已经发出去了再藏起来，
    #: 抓包或改 JS 就能看到内部细节（异常类名、路径、配置名）。
    #: 这里按 request.user.is_staff 决定是否真的输出内容。
    parse_error = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()

    def get_progress(self, obj):
        from .progress import file_snapshot
        return file_snapshot(obj)

    def get_summary(self, obj) -> str:
        """取首个分段的前 200 字作为摘要；没有分段就返回空串。"""
        chunk = (
            DocumentChunk.objects.filter(file=obj)
            .order_by("ordinal")
            .values_list("text", flat=True)
            .first()
        )
        if not chunk:
            return ""
        text = " ".join(str(chunk).split())
        return text[:200]

    def get_parse_error(self, obj) -> str:
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_staff", False):
            return obj.parse_error or ""
        return ""  # 非管理员：字段保留但为空，前端无需分支

    class Meta:
        model = IngestedFile
        fields = [
            "id",
            "library",
            "library_code",
            "library_name",
            "original_name",
            "content_sha256",
            "size_bytes",
            "size_display",
            "file_type",
            "file_type_group",
            "file_type_label",
            "directory_path",
            "classification",
            # 「性质」是**怎么来的**（""/auto_rule/auto_model/manual）与判定依据/
            # 失败原因。详情页与排查都读这两个字段：没有它们，用户看到「合同」
            # 无法判断是人工选的还是规则猜的，也无法判断"为什么还是待识别"。
            "classification_source",
            "classification_note",
            "customer",
            "department",
            "owner",
            "parse_status",
            "progress",
            "status_label",
            "summary",
            "amount_wan",
            "parse_error",
            "parse_attempts",
            "chunk_count",
            "embedded_chunk_count",
            "vector_status",
            "vector_collection",
            "vector_collection_name",
            "vector_generation",
            "source",
            "created_at",
            "parsed_at",
        ]
        read_only_fields = fields  # 全部只读：入库记录不可改写

    def get_file_type_group(self, obj) -> str:
        return file_type_group(obj.file_type)

    def get_file_type_label(self, obj) -> str:
        return FILE_TYPE_GROUP_LABELS[file_type_group(obj.file_type)]

    def get_size_display(self, obj: IngestedFile) -> str:
        size = float(obj.size_bytes or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return f"{size:.1f} GB"


class DocumentChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentChunk
        fields = ["id", "file", "ordinal", "text", "page", "token_estimate", "permission_code", "created_at"]
        read_only_fields = fields


class SearchRequestSerializer(serializers.Serializer):
    """混合检索请求。``libraries`` 是信息库筛选（对应前端的下拉）。"""

    q = serializers.CharField(allow_blank=False, trim_whitespace=True, max_length=500)
    libraries = serializers.ListField(
        child=serializers.CharField(max_length=64), required=False, allow_empty=True
    )
    limit = serializers.IntegerField(required=False, min_value=1, max_value=50, default=10)
    rerank = serializers.BooleanField(required=False, allow_null=True, default=None)
    #: 结构化条件：由**服务端**全量应用，前端只提交条件、不枚举候选
    department = serializers.CharField(
        required=False, allow_blank=True, max_length=64, default=""
    )
    customer = serializers.CharField(
        required=False, allow_blank=True, max_length=128, default=""
    )
    nature = serializers.CharField(
        required=False, allow_blank=True, max_length=64, default=""
    )
    amount_min = serializers.FloatField(required=False, allow_null=True, default=None)

    def validate_libraries(self, value):
        return [item.strip() for item in value if item and item.strip()]

    def validate_nature(self, value):
        """性质条件**必须**落在五项词表内：非法取值 400，不静默返回空结果。

        口径与列表页筛选（``views.validate_nature_value``）共用
        ``apps.knowledge.nature`` —— 两处各写一份词表校验迟早会漂移
        （搜索页筛得出、列表页筛不出那种自相矛盾）。

        **例外（只读哨兵）**：「待识别」``__unidentified__`` 是合法**筛选**值
        （列出性质为空的资料）。它是只读语义，不进入任何写入路径。
        """
        from .nature import NATURE_VOCABULARY, is_unidentified_filter, nature_error_message

        text = str(value or "").strip()
        if text and not is_unidentified_filter(text) and text not in NATURE_VOCABULARY:
            raise serializers.ValidationError(nature_error_message(text))
        return text

    def validate_amount_min(self, value):
        # NaN / inf 能通过 float()，但拿去比较或查库行为不可解释，必须拒
        if value is None:
            return value
        if not math.isfinite(value):
            raise serializers.ValidationError("金额下限必须是有限数字（万元）")
        if value < 0:
            raise serializers.ValidationError("金额下限不能为负数")
        return value

    @property
    def structured(self) -> bool:
        """是否带了任一结构化条件（决定要不要收紧候选集合）。"""
        data = getattr(self, "validated_data", None) or {}
        return bool(
            data.get("department")
            or data.get("customer")
            or data.get("nature")
            or data.get("amount_min") is not None
        )


class VectorCollectionSerializer(serializers.ModelSerializer):
    """向量集合登记信息。

    ``storage_path`` 是宿主机的绝对路径（例如 ``D:\\...\\vector_data\\qdrant``）。
    它的用途只有一个：证明"向量数据落在项目根目录内"。这个结论由服务端启动自检
    保证，**不需要**通过接口暴露给任何人——包括管理员（管理员要看就去看配置）。
    所以这里只暴露"是否已配置落盘位置"这个布尔值，不暴露路径本身。
    """

    storage_configured = serializers.SerializerMethodField()
    model_label = serializers.CharField(read_only=True)

    class Meta:
        model = VectorCollection
        fields = [
            "id",
            "name",
            "dimension",
            "embedding_model",
            "model_version",
            "profile",
            "model_label",
            "library",
            "storage_mode",
            "storage_configured",
            "is_active",
            # **退役待删**（持久删除状态）：接口也要如实暴露，否则 API 消费方会把
            # "正在删/删失败待重试"的代次当成普通历史代次（索引页已经显示它，
            # 两个入口口径必须一致）。
            "retiring_at",
            "chunk_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_storage_configured(self, obj: VectorCollection) -> bool:
        return bool(obj.storage_path or obj.storage_mode == "server")
