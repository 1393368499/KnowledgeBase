"""文件管理页的三项改动（用户 2026-10-16 要求）专项回归。

用户要求（原话）：「归属的话改成类型，性质的话包含：申请、合同、报表、普通文档、
涉密文档。文件类型下拉框要有PDF、Excel、Word三类」。本文件把这三条钉成可执行断言：

1. **类型列** = PDF / Excel / Word / 文本文件 / 其他（由服务端 ``models.file_type_group``
   唯一决定；.txt 显示「文本文件」是用户 2026-10-16 的追加要求）；
   「文件类型」下拉也按这几类（+ 其他）统计**全库真实份数**，不再罗列裸扩展名。
2. **性质** = 固定词表五项（``models.FileNature``）：上传时可选、列表里按行走受控
   操作 ``classify_file`` 修改（该库管理员 + 一次确认 + 审计含旧值→新值）。
3. 词表外的**历史值**（例如接口早期写入的「方案」）照旧存在、照旧可筛，界面把它
   原样显示并标注「历史值」—— 不能因为不在词表里就被改写成"未设置"。

治理边界一并锁死：``classification`` 进受控字段表之后，**其它**锁定列（原名、指纹、
金额…）在数据库触发器层仍然改不动。
"""

from __future__ import annotations

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.knowledge import storage
from apps.knowledge.models import (
    FILE_TYPE_GROUPS,
    FILE_TYPE_GROUP_LABELS,
    FileNature,
    IngestedFile,
    MemberRole,
    ParseStatus,
    file_type_group,
)
from apps.planner import services
from apps.planner.models import OperationRecord
from governance import triggers
from governance.immutable import ImmutableRecordError
from governance.registry import CONTROLLED_MUTABLE_FIELDS, controlled_save

pytestmark = pytest.mark.django_db

VOCABULARY = [choice.value for choice in FileNature]


@pytest.fixture(autouse=True)
def _isolated_blobs(isolated_paths):
    """原件一律落在临时目录：绝不往真实 ``media/raw`` 写测试数据。"""
    return isolated_paths


@pytest.fixture
def admin_api(auth_api, admin_user):
    """以 admin 身份登录的 API 客户端（列表/分面/上传都用它）。"""
    return auth_api(admin_user)


def _facets(admin_api):
    response = admin_api.get("/api/v1/knowledge/files/facets/")
    assert response.status_code == 200, response.content
    return response.json()


def _list(admin_api, **params):
    response = admin_api.get("/api/v1/knowledge/files/", params)
    assert response.status_code == 200, response.content
    body = response.json()
    return body["results"] if "results" in body else body


def _run_classify(user, file_id: int, classification: str) -> dict:
    """提案即执行（2026-11 起 classify_file 为元数据类非删除操作，直接执行）。

    ``classify_file`` 只改元数据字段、权限与审计都在服务端，因此不再签发确认凭证；
    测试保留"提案 → 执行结果"的业务契约，但对已自动执行的操作不再二次确认。
    """
    proposal = services.propose_operation(
        user,
        intent="classify_file",
        params={"file_id": file_id, "classification": classification},
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    if proposal.get("auto_executed"):
        assert proposal["status"] == OperationRecord.Status.EXECUTED, proposal["status"]
        operation.refresh_from_db()
        assert operation.status == OperationRecord.Status.EXECUTED, operation.status
        return proposal["result"]
    services.confirm_operation(user, operation, token=proposal["confirm_token"])
    operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.ARMED, operation.status
    return services.execute_operation(user, operation)["result"]


# ------------------------------------------------------------------ 类型三类 ----


def test_type_groups_cover_pdf_excel_word():
    """三类必须真的覆盖 PDF / Excel / Word 的常见扩展名，且显示名固定。"""
    assert FILE_TYPE_GROUPS["pdf"] == ("pdf",)
    assert "xlsx" in FILE_TYPE_GROUPS["excel"] and "xls" in FILE_TYPE_GROUPS["excel"]
    assert "docx" in FILE_TYPE_GROUPS["word"] and "doc" in FILE_TYPE_GROUPS["word"]
    assert FILE_TYPE_GROUP_LABELS["pdf"] == "PDF"
    assert FILE_TYPE_GROUP_LABELS["excel"] == "Excel"
    assert FILE_TYPE_GROUP_LABELS["word"] == "Word"


def test_txt_is_labelled_text_file_not_other():
    """.txt 必须显示「文本文件」——用户明确不要笼统的「其他」（不猜它是不是文档）。"""
    assert FILE_TYPE_GROUPS["text"] == ("txt",)
    assert FILE_TYPE_GROUP_LABELS["text"] == "文本文件"
    assert file_type_group("txt") == "text"
    assert file_type_group(".TXT") == "text"  # 大小写与前导点都不能影响归类
    # 图片格式用户明确说不会上传，因此不单列一类，落到「其他」兜底
    assert file_type_group("png") == "other"
    assert file_type_group("") == "other"


def test_facets_group_file_types_with_real_counts(admin_api, library, make_file):
    """「文件类型」下拉的份数按**全库**统计，且只列这几类（+ 其他）。"""
    for name in ("a.pdf", "b.pdf", "c.xlsx", "d.doc", "e.txt"):
        make_file(library, name=name)

    body = _facets(admin_api)
    groups = {item["value"]: item for item in body["file_type_groups"]}

    assert groups["pdf"]["label"] == "PDF" and groups["pdf"]["count"] == 2
    assert groups["excel"]["label"] == "Excel" and groups["excel"]["count"] == 1
    assert groups["word"]["label"] == "Word" and groups["word"]["count"] == 1
    assert groups["text"]["label"] == "文本文件" and groups["text"]["count"] == 1
    # 这几份都在已命名的组里 → 不该再冒出「其他」
    assert "other" not in groups


def test_other_bucket_counts_only_unlisted_extensions(admin_api, library, make_file):
    """「其他」= 没被命名的扩展名（如 png），且只有真有份数时才出现。"""
    make_file(library, name="扫描件.png")
    groups = {item["value"]: item for item in _facets(admin_api)["file_type_groups"]}
    assert groups["other"]["count"] == 1
    assert groups["other"]["label"] == "其他"


def test_other_bucket_hidden_when_there_is_nothing_outside(admin_api, library, make_file):
    make_file(library, name="only.pdf")
    values = [item["value"] for item in _facets(admin_api)["file_type_groups"]]
    assert values == ["pdf", "excel", "word", "text"]


def test_list_filters_by_type_group_and_keeps_raw_extension(admin_api, library, make_file):
    """三类是**分组**筛选；同时老链接里的裸扩展名（file_type=txt）继续可用。"""
    make_file(library, name="a.pdf")
    make_file(library, name="b.xlsx")
    make_file(library, name="c.csv")
    make_file(library, name="d.docx")
    make_file(library, name="e.txt")

    assert {row["original_name"] for row in _list(admin_api, file_type="excel")} == {
        "b.xlsx",
        "c.csv",
    }
    assert {row["original_name"] for row in _list(admin_api, file_type="word")} == {"d.docx"}
    assert {row["original_name"] for row in _list(admin_api, file_type="pdf")} == {"a.pdf"}
    # 裸扩展名：精确匹配（旧前端/旧链接的行为不能被这次改动弄坏）
    assert {row["original_name"] for row in _list(admin_api, file_type="txt")} == {"e.txt"}
    assert _list(admin_api, file_type="all") != []


def test_serializer_exposes_group_label_for_the_type_column(admin_api, library, make_file):
    """「类型」列显示的 PDF/Excel/Word 由服务端给出，前端不再自己映射一份。"""
    make_file(library, name="报表.xlsx")
    row = _list(admin_api)[0]
    assert row["file_type"] == "xlsx"
    assert row["file_type_group"] == "excel"
    assert row["file_type_label"] == "Excel"


# ------------------------------------------------------------------ 性质词表 ----


def test_nature_vocabulary_is_the_five_requested_values(admin_api, library):
    """词表就这五个，顺序固定（前后端共用同一处定义）。"""
    assert VOCABULARY == ["申请", "合同", "报表", "普通文档", "涉密文档"]
    assert _facets(admin_api)["classifications"] == VOCABULARY


def test_facets_always_list_the_five_natures_even_when_unused(admin_api, library):
    """一份资料都没填性质时，下拉也必须能列出五个可选值（否则用户看不到有哪几类）。"""
    body = _facets(admin_api)
    by_value = {item["value"]: item["count"] for item in body["natures"]}
    for value in VOCABULARY:
        assert by_value.get(value) == 0
    assert body["total"] == 0


def test_facets_keep_legacy_values_with_their_counts(admin_api, library, make_file):
    """词表外的历史值照旧出现在选项里（带真实份数），不被静默丢弃。"""
    make_file(library, name="旧资料.pdf", classification="方案")
    make_file(library, name="合同.pdf", classification="合同")

    by_value = {item["value"]: item["count"] for item in _facets(admin_api)["natures"]}
    assert by_value["方案"] == 1
    assert by_value["合同"] == 1
    # 其余四个词表值仍然是 0，但**必须在**（用户要能选中它们）
    for value in VOCABULARY:
        assert value in by_value


def test_nature_filter_still_accepts_legacy_value(admin_api, library, make_file):
    make_file(library, name="旧资料.pdf", classification="方案")
    make_file(library, name="合同.pdf", classification="合同")
    rows = _list(admin_api, nature="方案")
    assert [row["original_name"] for row in rows] == ["旧资料.pdf"]


def test_upload_persists_chosen_nature(auth_api, admin_user, library):
    """上传时选的性质要真的落库（否则列表里的下拉永远显示"未设置"）。"""
    from django.core.files.uploadedfile import SimpleUploadedFile

    response = auth_api(admin_user).post(
        "/api/v1/knowledge/files/upload/",
        {
            "file": SimpleUploadedFile("涉密文件.md", b"secret", content_type="text/markdown"),
            "library": str(library.pk),
            "classification": "涉密文档",
        },
        format="multipart",
    )
    assert response.status_code in {201, 202}
    record = IngestedFile.objects.get()
    assert record.classification == "涉密文档"


def test_upload_without_nature_is_allowed(auth_api, admin_user, library):
    """性质可以留空（目录扫描常常一时分不清）：之后在列表里按行受控修改。"""
    from django.core.files.uploadedfile import SimpleUploadedFile

    response = auth_api(admin_user).post(
        "/api/v1/knowledge/files/upload/",
        {
            "file": SimpleUploadedFile("待定.md", b"later", content_type="text/markdown"),
            "library": str(library.pk),
        },
        format="multipart",
    )
    assert response.status_code in {201, 202}
    assert IngestedFile.objects.get().classification == ""


# -------------------------------------------------------- classify_file（受控） ----


def test_spec_is_controlled_and_not_destructive():
    spec = services.get_spec("classify_file")
    assert spec is not None
    # 末尾的问号 = 可选：空字符串表示「清空/未设置」，必须允许（填错了要能改回来）
    assert spec.params == {"file_id": "int", "classification": "str?"}
    assert spec.permission == "library:admin"
    # 只改元数据：权限（库管理员）在服务端校验、写审计事件，因此直接执行；
    # 它**不是**删除类操作，不进三次确认的破坏性流程。
    assert spec.auto_execute is True
    assert spec.requires_confirmation is False
    assert spec.requires_destructive_flow is False
    # 直接执行的操作不签发确认凭证，因此没有 ticket_scope
    assert spec.ticket_scope == ""


def test_reader_cannot_change_nature(library, user, make_file, grant_member):
    grant_member(library, user, role=MemberRole.READER)
    record = make_file(library)
    with pytest.raises(services.PlannerError) as excinfo:
        services.propose_operation(
            user,
            intent="classify_file",
            params={"file_id": record.pk, "classification": "合同"},
        )
    assert "权限" in str(excinfo.value)
    record.refresh_from_db()
    assert record.classification == ""


def test_library_admin_can_change_nature(library, user, make_file, grant_member):
    grant_member(library, user, role=MemberRole.ADMIN)
    record = make_file(library, name="合同.pdf")
    proposal = services.propose_operation(
        user,
        intent="classify_file",
        params={"file_id": record.pk, "classification": "合同"},
    )
    assert proposal["operation_id"]
    assert proposal["target_label"] == "合同.pdf"


def test_classify_round_trip_changes_only_the_nature(admin_user, library, make_file):
    """只动 `classification`：原名/指纹/大小/分段数/状态全部逐项不变。"""
    body = "# 合同\n\n质保期 12 个月。".encode()
    stored = storage.store_bytes(body)
    record = make_file(
        library,
        name="真实合同.pdf",
        sha=stored.sha256,
        size_bytes=stored.size_bytes,
        raw_path=stored.rel_path,
        raw_size_bytes=stored.size_bytes,
        status=ParseStatus.DONE,
        chunk_count=7,
        parsed_at=timezone.now(),
        amount_wan=120.5,
    )
    before = IngestedFile.objects.get(pk=record.pk)
    raw_before = storage.read_blob(record.content_sha256)

    result = _run_classify(admin_user, record.pk, "合同")

    assert result["changed"] is True
    assert result["classification"] == "合同"
    assert result["previous"] == ""
    record.refresh_from_db()
    assert record.classification == "合同"
    # 其它一切都没变（尤其是原件字节与分段数）
    assert record.original_name == before.original_name
    assert record.content_sha256 == before.content_sha256
    assert record.size_bytes == before.size_bytes
    assert record.chunk_count == before.chunk_count
    assert record.amount_wan == before.amount_wan
    assert record.parse_status == ParseStatus.DONE
    assert storage.read_blob(record.content_sha256) == raw_before
    assert storage.verify_blob(record.content_sha256) is True


def test_classify_appends_audit_event_with_old_and_new(admin_user, library, make_file):
    record = make_file(library, classification="申请")
    _run_classify(admin_user, record.pk, "涉密文档")

    event = AuditEvent.objects.filter(action="knowledge.file_classified").order_by("-id").first()
    assert event is not None
    assert event.resource_key == str(record.pk)
    assert event.payload.get("previous") == "申请"
    assert event.payload.get("classification") == "涉密文档"
    assert event.payload.get("library_id") == library.pk


def test_classify_is_idempotent_without_second_event(admin_user, library, make_file):
    record = make_file(library, classification="合同")
    result = _run_classify(admin_user, record.pk, "合同")
    assert result["changed"] is False
    assert AuditEvent.objects.filter(action="knowledge.file_classified").count() == 0


def test_classify_can_clear_the_nature(admin_user, library, make_file):
    record = make_file(library, classification="报表")
    result = _run_classify(admin_user, record.pk, "")
    assert result["changed"] is True
    assert result["classification"] == ""
    record.refresh_from_db()
    assert record.classification == ""


def test_classify_rejects_value_outside_vocabulary(admin_user, library, make_file):
    """受控写入必须严格按词表：脏值直接拒绝，不静默写进去。"""
    record = make_file(library)
    with pytest.raises(services.PlannerError) as excinfo:
        services.propose_operation(
            admin_user,
            intent="classify_file",
            params={"file_id": record.pk, "classification": "技术文档"},
        )
    message = str(excinfo.value)
    assert "性质" in message
    for value in VOCABULARY:
        assert value in message
    record.refresh_from_db()
    assert record.classification == ""


def test_missing_file_is_reported(admin_user):
    with pytest.raises(services.PlannerError) as excinfo:
        services.propose_operation(
            admin_user,
            intent="classify_file",
            params={"file_id": 999999, "classification": "合同"},
        )
    assert "资料不存在" in str(excinfo.value)


def test_propose_endpoint_maps_bad_value_to_400(auth_api, admin_user, library, make_file):
    record = make_file(library)
    response = auth_api(admin_user).post(
        "/api/v1/planner/operations/propose/",
        {
            "intent": "classify_file",
            "params": {"file_id": record.pk, "classification": "随便写"},
        },
        format="json",
    )
    assert response.status_code == 400
    assert "性质" in response.json()["detail"]


# ------------------------------------------------- 治理边界：受控 ≠ 解除保护 ----


def test_nature_is_now_a_controlled_field_but_others_stay_locked():
    """受控字段表里只有 `classification` 新进来；金额/原名/指纹仍在锁列里。"""
    controlled = CONTROLLED_MUTABLE_FIELDS["knowledge.IngestedFile"]
    assert "classification" in controlled
    assert set(IngestedFile.CONTROLLED_FIELDS) == set(controlled)
    locked = triggers.locked_columns()
    assert "classification" not in locked
    for column in ("original_name", "content_sha256", "library_id", "amount_wan"):
        assert column in locked


def test_plain_save_cannot_change_nature(admin_user, library, make_file):
    """直改 ORM 一律被拒：受控通道是唯一入口（即使调用方是 superuser）。"""
    record = make_file(library)
    record.classification = "合同"
    with pytest.raises(ImmutableRecordError):
        record.save()
    record.refresh_from_db()
    assert record.classification == ""


def test_controlled_save_cannot_change_a_locked_field(admin_user, library, make_file):
    """走进受控通道也不行：越界字段被登记表白名单挡住。"""
    record = make_file(library)
    record.original_name = "改名.pdf"
    with pytest.raises(ImmutableRecordError):
        controlled_save(record, fields=["original_name"])
    record.refresh_from_db()
    assert record.original_name != "改名.pdf"


def test_db_trigger_still_blocks_locked_columns(admin_user, library, make_file):
    """数据库触发器层：锁定列（这里是原名）仍然拒绝 UPDATE。"""
    record = make_file(library)
    with pytest.raises(Exception):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kb_ingested_file SET original_name = 'hacked.pdf' WHERE id = %s",
                    [record.pk],
                )
    record.refresh_from_db()
    assert record.original_name != "hacked.pdf"
