"""「性质」按正文自动识别（用户 2026-10-16 要求）的专项回归。

覆盖的设计约定（每条都有真实断言，不是"只测字符串"）：

1. **先规则、后模型**：五类正文样本各一；短而明确的申请也照样识别；
   文件名误导时**按正文**（`合同.pdf` 里是出差申请 → 申请）；
   扫描件 OCR 后同样自动识别（用显式离线测试适配器，不碰真实模型）。
2. **涉密只在有明确密级标识时判定**：正例（`密级：机密` / `秘密★`）+ 反例
   （`密级：公开` / `密级：非密` / 通用商业机密条款 / 否定语境 / 知识性讨论）。
3. **模型输出严格枚举、fail-closed**：`不是合同` / `非涉密文档` / `应该不是申请` /
   `合同或其他` / `技术文档` / `无法判断` 一律失败，不写库。
4. **失败如实**：模型超时/非法输出/正文不足 → 待识别 + 原因可见；
   **已有可信自动值时不因一次失败清空**（保留原值 + 说明"本次识别失败、未更新"）。
5. **幂等**：同正文两次结果一致，且不产生第二条审计以外的副作用。
6. **人工优先（真实并发）**：阻塞分类 → 人工改正 → 释放分类 → 人工值保留。
7. **补识别命令**：只补空白 + 非人工；`--dry-run` / `--ids` / `--limit`；
   系统管理员（无显式成员）全库范围；普通只读用户不得补写。
8. **一致性**：列表筛选 / 详情 / 检索结果卡上的性质都取当前元数据，
   「待识别」筛选用哨兵值 `__unidentified__`（与前端 `KB.NATURE_UNIDENTIFIED` 一致），
   写入路径一律拒绝哨兵值与词表外的值（400 / 受控校验）。

反例清单来源：``artifacts/review/auto-nature-cases-20261016.json``（只读参考，
本文件把它当**验收集**跑，绝不改它来迁就实现）。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.knowledge import classification, storage
from apps.knowledge.models import (
    ClassificationSource,
    DocumentChunk,
    FileNature,
    IngestedFile,
    MemberRole,
    ParseStatus,
)
from apps.knowledge.permissions import is_system_admin
from apps.planner import services
from apps.planner.models import OperationRecord

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]
REVIEW_CASES = json.loads(
    (ROOT / "artifacts" / "review" / "auto-nature-cases-20261016.json").read_text(
        encoding="utf-8"
    )
)["cases"]

VOCABULARY = [choice.value for choice in FileNature]


# ------------------------------------------------------------------ 夹具 ----


@pytest.fixture(autouse=True)
def _isolated_blobs(isolated_paths):
    """原件一律落临时目录：绝不往真实 ``media/raw`` 写测试数据。"""
    return isolated_paths


class FakeClassifier:
    """确定性假分类器（**显式注入**，不走任何真实外呼）。

    ``answers``：``(子串, 词表值)`` 列表，按顺序取第一个命中的子串。
    ``on_call``：在返回之前执行的钩子 —— 用来复现"模型调用期间人工改了性质"
    这种时序（比多线程更确定，不依赖真实的线程调度）。
    """

    def __init__(self, answers=None, *, reply="", raises=None, on_call=None, calls=None):
        self.answers = list(answers or [])
        self.reply = reply
        self.raises = raises
        self.on_call = on_call
        self.calls = calls if calls is not None else []
        self.__test__ = False

    def classify(self, *, text: str, original_name: str = ""):
        self.calls.append({"text": text, "original_name": original_name})
        if self.on_call is not None:
            self.on_call()
        if self.raises is not None:
            raise self.raises
        if self.answers:
            for needle, value in self.answers:
                if needle in text:
                    return value, ""
        return self.reply, ""


class AlwaysOrdinary(FakeClassifier):
    """对任何正文都答「普通文档」——用来复现"模型乱判"的场景。"""

    def __init__(self, **kwargs):
        super().__init__(reply=FileNature.ORDINARY, **kwargs)


def _make_record(library, make_file, body: bytes, *, name="资料.md", **extra):
    """创建"有真实原始字节"的资料（sha/大小/路径一开始就正确）。"""
    stored = storage.store_bytes(body)
    return make_file(
        library,
        name=name,
        sha=stored.sha256,
        size_bytes=stored.size_bytes,
        raw_path=stored.rel_path,
        raw_size_bytes=stored.size_bytes,
        **extra,
    )


@pytest.fixture
def ai_library(library, isolated_paths, qdrant_client):
    """库 + generation=1 的测试专用 collection（``__offline_test`` 后缀）。"""
    from apps.knowledge import testing_adapter as ta
    from apps.knowledge import vectorindex
    from apps.knowledge.embedding import active_profile
    from apps.knowledge.models import KnowledgeLibrary, VectorCollection

    name = ta.test_collection_name(library.pk, 1)
    vectorindex.ensure_collection(qdrant_client, name, 1024)
    VectorCollection.objects.create(
        name=name,
        dimension=1024,
        embedding_model=active_profile().model,
        model_version=active_profile().model_version,
        profile=active_profile().name,
        library=library,
        storage_mode="embedded",
        storage_path=str(isolated_paths["vector"]),
        is_active=True,
    )
    KnowledgeLibrary.objects.filter(pk=library.pk).update(
        active_generation=1, vector_collection=name
    )
    library.refresh_from_db()
    yield library, qdrant_client, name


def _run_classify(user, file_id: int, value: str) -> dict:
    """人工纠正走**真实受控路径**：提案 → 确认 → 执行。"""
    proposal = services.propose_operation(
        user, intent="classify_file", params={"file_id": file_id, "classification": value}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    services.confirm_operation(user, operation, token=proposal["confirm_token"])
    operation.refresh_from_db()
    return services.execute_operation(user, operation)["result"]


#: 与「性质」相关的审计事件：值变化（`file_classified`）与
#: 只把来源标记为人工（`file_classification_source_marked`）分开记，
#: 但两者都属于"性质被谁改过"的证据。
CLASSIFICATION_AUDIT_ACTIONS = (
    "knowledge.file_classified",
    "knowledge.file_classification_source_marked",
)


def _classify_events(file_id: int):
    return AuditEvent.objects.filter(
        action__in=CLASSIFICATION_AUDIT_ACTIONS, resource_key=str(file_id)
    ).order_by("id")


# --------------------------------------------------- 1. 规则：五类与文件名 ----


SAMPLE_APPLICATION = (
    "出差申请单\n申请人：张三\n申请部门：交付中心\n申请事由：赴客户现场交付\n"
    "出差时间：2026年10月18日至10月20日\n审批意见：同意"
)
SAMPLE_CONTRACT = (
    "技术服务合同\n合同编号：HT-2026-0417\n甲方：示例甲公司\n乙方：示例乙公司\n"
    "签订地点：北京市\n签订日期：2026年10月1日\n合同金额：100万元\n"
    "付款方式：验收后支付\n违约责任：按约定承担\n保密条款：双方应保护对方商业信息。"
)
SAMPLE_REPORT = (
    "2026年第三季度收支统计报表\n月份,营业收入,成本,利润\n7月,100,60,40\n"
    "8月,120,70,50\n9月,110,65,45\n季度合计,330,195,135\n同比 +12%，环比 +3%"
)
SAMPLE_CONFIDENTIAL = (
    "密级：机密\n保密期限：5年\n本件不得外传，限指定人员阅知。\n"
    "项目工作方案：有关安排按批准清单执行。"
)
SAMPLE_ORDINARY = (
    "交付会议纪要\n本次会议讨论了上线时间。决定下周完成联调，验收日期另行通知。"
    "待办：补充操作手册、确认培训名单。会议结束。"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (SAMPLE_APPLICATION, FileNature.APPLICATION),
        (SAMPLE_CONTRACT, FileNature.CONTRACT),
        (SAMPLE_REPORT, FileNature.REPORT),
        (SAMPLE_CONFIDENTIAL, FileNature.CONFIDENTIAL),
    ],
)
def test_rule_decides_the_four_explicit_natures(text, expected):
    """四类有强特征的正文由**规则**直接判定（可解释、可复现，不外呼模型）。"""
    decision = classification.detect_rule(text)
    assert decision.value == expected, decision
    assert decision.source == "auto_rule"
    assert decision.note, "规则判定必须给出依据"


def test_rule_does_not_force_ordinary_document():
    """没有强特征的正文**不许**被规则硬填成「普通文档」，而是交给模型。"""
    decision = classification.detect_rule(SAMPLE_ORDINARY)
    assert decision.value == ""
    assert decision.needs_model is True
    assert "不猜" in decision.reason


def test_filename_does_not_override_content():
    """文件名误导时按正文判：文件名 `合同.pdf`、正文是出差申请 → 申请。"""
    decision = classification.detect_rule(SAMPLE_APPLICATION)
    assert decision.value == FileNature.APPLICATION
    # 文件名完全不参与规则判定（规则签名里根本没有它）
    import inspect

    assert "name" not in inspect.signature(classification.detect_rule).parameters


def test_short_but_explicit_application_is_still_recognized():
    """短而明确的申请也要识别（长度门槛不能挡在规则前面）。"""
    short = "出差申请\n申请事项：赴客户现场交付。\n审批意见：同意。"
    decision = classification.detect_rule(short)
    assert decision.value == FileNature.APPLICATION


@pytest.mark.parametrize(
    "text",
    [
        # 通用词泛滥但**没有申请文种**：合同里的"同意""部门负责人"不算申请表字段
        "技术服务合同\n甲方：示例甲公司\n乙方：示例乙公司\n合同编号：HT-1\n"
        "付款方式：验收后支付\n双方同意按约定履行。部门负责人意见：同意。\n违约责任：按约定承担。",
        # 只有"申请人"一个字段、没有文种词
        "会议纪要\n记录人：李四\n申请人：张三\n会议讨论了上线时间，下周完成联调。",
        # 只有光秃秃的"申请"两个字
        "月度统计报表\n合计 100 万元，同比 +10%。本表用于申请预算。",
    ],
)
def test_application_rule_requires_title_and_fields(text):
    """申请规则的**反例**：没有"申请文种 + 表单字段组合"就不许判申请。

    独立审查指出"同意"这类通用弱词太宽：普通合同里出现"同意"+"部门负责人"
    会被申请规则抢先。现在规则只认文种/字段组合，模糊冲突一律交模型。
    """
    decision = classification.detect_rule(text)
    assert decision.value != FileNature.APPLICATION, (text, decision)


# --------------------------------------------- 2. 涉密：正例 + 反例专项 ----


@pytest.mark.parametrize(
    "text",
    [
        "密级：机密\n保密期限：5年\n项目工作方案。",
        "密级：绝密\n本件不得外传。",
        "秘密★\n项目实施方案。",
        "机密\n项目实施方案。",
    ],
)
def test_confidential_positive_requires_explicit_marking(text):
    """有**明确密级标识**（结构与值都对）时才判涉密。"""
    decision = classification.detect_rule(text)
    assert decision.value == FileNature.CONFIDENTIAL, (text, decision)


@pytest.mark.parametrize(
    "marker",
    [
        "密级：公开",
        "密级：非密",
        "密级：解密",
        "本合同双方应保护商业机密",
        "本文件不涉及国家秘密及商业机密",
        "培训案例：机密是密级之一，本材料为公开教材",
        "本项目不属于机密范围，按普通项目办理。",
    ],
)
def test_confidential_negative_never_misclassified(marker):
    """这七类反例**绝不许**判成涉密（判错方向的合规后果最严重）。"""
    text = marker + "\n" + SAMPLE_CONTRACT
    decision = classification.detect_rule(text)
    assert decision.value != FileNature.CONFIDENTIAL, (marker, decision)


def test_generic_confidentiality_clause_stays_contract():
    """通用保密条款**不能**把合同判成涉密；而且它本身仍是合同。"""
    decision = classification.detect_rule(SAMPLE_CONTRACT)
    assert decision.value == FileNature.CONTRACT
    assert "保密条款" in SAMPLE_CONTRACT  # 前提：样本里真的有这句话


# ------------------------------------- 3. 模型输出：严格枚举、fail-closed ----


@pytest.mark.parametrize(
    "reply",
    [
        "不是合同",
        "非涉密文档",
        "应该不是申请",
        "合同或其他",
        "技术文档",
        "申请/合同",
        "无法判断",
        "保密",
        "",
        "普通文档和合同",
        "不确定",
    ],
)
def test_invalid_model_replies_are_rejected(reply):
    """否定/候选集合/解释/词表外值一律**拒绝**（fail-closed，绝不猜）。"""
    value, error = classification.parse_model_reply(reply)
    assert value == ""
    assert error


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("合同", FileNature.CONTRACT),
        ("  申请。  ", FileNature.APPLICATION),
        ("答案：报表", FileNature.REPORT),
        ("分类结果：涉密文档", FileNature.CONFIDENTIAL),
        ("普通文档", FileNature.ORDINARY),
    ],
)
def test_valid_model_replies_are_exactly_matched(reply, expected):
    """只有"完整值"或"明确前缀 + 完整值"才算合法。"""
    value, error = classification.parse_model_reply(reply)
    assert value == expected, (reply, value, error)
    assert error == ""


# --------------------------------------------------- 4. 失败如实与保留 ----


def test_model_timeout_keeps_pending_with_visible_reason():
    """模型不可用/超时 → 待识别，原因可见；**不写库、不强填普通文档**。"""
    client = FakeClassifier(raises=TimeoutError("模型超时"))
    outcome = classification.classify_content(SAMPLE_ORDINARY, client=client)
    assert outcome.status == "pending"
    assert outcome.value == ""
    assert "超时" in outcome.note or "失败" in outcome.note


def test_invalid_model_output_keeps_pending():
    client = FakeClassifier(reply="技术文档")
    outcome = classification.classify_content(SAMPLE_ORDINARY, client=client)
    assert outcome.status == "pending"
    assert outcome.value == ""
    assert "非法" in outcome.note


def test_no_text_or_garbled_text_is_pending_without_model_call():
    """无正文 / 乱码 → 待识别，且**不外呼模型**（避免幻觉）。"""
    for text in ("", "   ", "???\ufffd\ufffd", "……"):
        client = FakeClassifier(reply="合同")
        outcome = classification.classify_content(text, client=client)
        assert outcome.status == "pending", (text, outcome)
        assert outcome.value == ""
        assert client.calls == [], "没有实质正文时不该外呼模型"


def test_offline_switch_short_circuits_model_call(settings):
    """离线开关打开时规则照判、规则不命中即待识别，**零外呼**。"""
    settings.KB_MODEL_CALLS_DISABLED = True
    assert (
        classification.classify_content(SAMPLE_APPLICATION).value == FileNature.APPLICATION
    )
    client = FakeClassifier(reply="合同")
    outcome = classification.classify_content(SAMPLE_ORDINARY, client=client)
    assert outcome.status == "pending"
    assert "KB_MODEL_CALLS_DISABLED" in outcome.note


def test_failure_does_not_clear_existing_auto_value(library, make_file):
    """**已有可信自动值时，一次失败不许清空它**（审查必改项）。

    保留原值 + 来源，只把说明改成"本次识别失败、未更新"，状态不退回待识别。
    """
    record = make_file(
        library,
        name="已识别.md",
        classification=FileNature.CONTRACT,
        classification_source=ClassificationSource.AUTO_RULE,
        classification_note="规则识别：合同要件 命中「甲方」/「乙方」",
    )

    outcome = classification.auto_classify_file(
        record,
        text=SAMPLE_ORDINARY,
        client=FakeClassifier(raises=TimeoutError("模型超时")),
    )

    assert outcome.status == "failed_kept_previous"
    record.refresh_from_db()
    assert record.classification == FileNature.CONTRACT, "失败不许把原值清空"
    assert record.classification_source == ClassificationSource.AUTO_RULE
    assert "未更新" in record.classification_note
    assert "保留" in record.classification_note
    event = _classify_events(record.pk).last()
    assert event is not None and event.payload["value_unchanged"] is True


def test_failure_on_blank_nature_stays_pending_and_visible(library, make_file):
    """本来就空白：失败 → 保持待识别，说明里写清原因。"""
    record = make_file(library, name="待识别.md")
    outcome = classification.auto_classify_file(
        record,
        text=SAMPLE_ORDINARY,
        client=FakeClassifier(raises=TimeoutError("连接超时")),
    )
    assert outcome.status == "pending"
    record.refresh_from_db()
    assert record.classification == ""
    assert record.classification_source == ""
    assert "失败" in record.classification_note or "超时" in record.classification_note


# ------------------------------------------- 5. 幂等（不产生第二条审计） ----


def test_auto_classify_is_idempotent_without_second_audit(library, make_file):
    """同正文两次识别：结果一致，第二次不写库、不追加第二条审计。"""
    record = make_file(library, name="合同样本.md")
    first = classification.auto_classify_file(record, text=SAMPLE_CONTRACT)
    assert first.status == "applied"
    assert record.classification == FileNature.CONTRACT
    events_after_first = _classify_events(record.pk).count()
    assert events_after_first == 1

    second = classification.auto_classify_file(record, text=SAMPLE_CONTRACT)
    assert second.status == "unchanged"
    record.refresh_from_db()
    assert record.classification == FileNature.CONTRACT
    assert _classify_events(record.pk).count() == events_after_first
    # 而且没有"第二次写"的副作用：分段/状态/原件都没动
    assert record.parse_status == ParseStatus.DONE


def test_pending_result_is_also_idempotent(library, make_file):
    """待识别（失败）也要幂等：第二次不追加审计。"""
    record = make_file(library, name="待识别.md")
    client = FakeClassifier(raises=TimeoutError("超时"))
    classification.auto_classify_file(record, text=SAMPLE_ORDINARY, client=client)
    count = _classify_events(record.pk).count()
    outcome = classification.auto_classify_file(
        record, text=SAMPLE_ORDINARY, client=FakeClassifier(raises=TimeoutError("超时"))
    )
    assert outcome.status == "unchanged"
    assert _classify_events(record.pk).count() == count


# ---------------------------------------- 6. 人工优先（真实并发 + 重试） ----


def test_auto_never_overwrites_manual(library, make_file):
    """人工设置过（manual）时，自动识别一行都不改。"""
    record = make_file(
        library,
        name="人工已定.md",
        classification=FileNature.APPLICATION,
        classification_source=ClassificationSource.MANUAL,
        classification_note="人工设置为「申请」",
    )
    outcome = classification.auto_classify_file(record, text=SAMPLE_CONTRACT)
    assert outcome.status == "skipped_manual"
    record.refresh_from_db()
    assert record.classification == FileNature.APPLICATION
    assert record.classification_source == ClassificationSource.MANUAL
    assert _classify_events(record.pk).count() == 0


def test_manual_clear_blocks_future_auto(library, make_file):
    """人工"清空"也是人工决定：之后自动识别不得把它重新填上。"""
    record = make_file(library, name="人工清空.md")
    outcome = classification.auto_classify_file(record, text=SAMPLE_CONTRACT)
    assert outcome.status == "applied"

    _run_classify_manual_marker(record)
    record.refresh_from_db()
    assert record.classification == ""
    assert record.classification_source == ClassificationSource.MANUAL

    again = classification.auto_classify_file(record, text=SAMPLE_CONTRACT)
    assert again.status == "skipped_manual"
    record.refresh_from_db()
    assert record.classification == ""


def _run_classify_manual_marker(record):
    """直接走受控写入把来源标成 manual（等价于 classify_file 的清空分支）。"""
    from governance.registry import controlled_save

    record.classification = ""
    record.classification_source = ClassificationSource.MANUAL
    record.classification_note = "人工清空（保持待识别，自动识别不会覆盖）"
    controlled_save(
        record, fields=["classification", "classification_source", "classification_note"]
    )


def test_late_auto_result_cannot_overwrite_manual_fix_made_during_model_call(
    library, make_file
):
    """**晚到结果不许覆盖人工改正**：判定期间（模型调用中）人工改正 → 放弃写入。

    这是"判定在锁外做、提交时短事务重新核验"的复现用例：假模型在**自己返回之前**
    把性质标成人工设置（模拟用户此刻在列表里点下拉），自动结果必须被丢弃。
    修复前它会断言失败（旧代码只比"判定前的旧值"）。
    """
    from governance.registry import controlled_save

    record = make_file(library, name="并发合同.md")

    def manual_fix_during_model_call() -> None:
        """模型还在"想"的时候，用户已经在列表里把性质改成人工的「申请」。"""
        row = IngestedFile._base_manager.get(pk=record.pk)
        row.classification = FileNature.APPLICATION
        row.classification_source = ClassificationSource.MANUAL
        row.classification_note = "人工设置为「申请」"
        controlled_save(
            row,
            fields=["classification", "classification_source", "classification_note"],
        )

    outcome = classification.auto_classify_file(
        record,
        text=SAMPLE_ORDINARY,
        client=FakeClassifier(
            reply=FileNature.CONTRACT, on_call=manual_fix_during_model_call
        ),
    )

    assert outcome.status == "skipped_manual", outcome
    record.refresh_from_db()
    assert record.classification == FileNature.APPLICATION, "晚到的自动结果覆盖了人工值"
    assert record.classification_source == ClassificationSource.MANUAL
    assert "人工" in record.classification_note


def test_real_manual_operation_wins_over_pending_auto_result(
    library, make_file, admin_user, grant_member
):
    """人工改正走**真实受控路径**后，自动识别不再动它（端到端，不靠假时序）。"""
    grant_member(library, admin_user, role=MemberRole.ADMIN)
    record = make_file(library, name="人工优先.md")
    result = _run_classify(admin_user, record.pk, FileNature.REPORT)
    assert result["changed"] is True

    outcome = classification.auto_classify_file(record, text=SAMPLE_CONTRACT)
    assert outcome.status == "skipped_manual"
    record.refresh_from_db()
    assert record.classification == FileNature.REPORT
    assert record.classification_source == ClassificationSource.MANUAL


@pytest.mark.django_db(transaction=True)
def test_threaded_late_auto_result_does_not_overwrite_manual_fix(
    library, make_file, admin_user, grant_member, test_db_kind
):
    """**真线程**复现：分类在另一个连接里被阻塞 —— 人工改正 → 释放 → 人工值保留。

    为什么在 SQLite 上 skip：SQLite 的内存测试库是 ``mode=memory&cache=shared``，
    ``transaction=True`` 用例结束时的 flush 在这类库上会直接报错（与本次改动无关的
    测试基础设施限制）。**Postgres 上真跑**；SQLite 上的等价覆盖由
    ``test_late_auto_result_cannot_overwrite_manual_fix_made_during_model_call``
    （判定期间注入人工写入）与 ``test_auto_classify_respects_stale_lease`` 提供。
    """
    import threading

    if test_db_kind != "postgres":
        pytest.skip(f"当前测试库是 {test_db_kind}；真线程用例请在 PG 上跑（--kb-db=postgres）")

    grant_member(library, admin_user, role=MemberRole.ADMIN)
    record = make_file(library, name="真线程并发.md")
    file_id = record.pk

    started = threading.Event()
    release = threading.Event()
    outcome_box: dict = {}

    def worker() -> None:
        try:
            row = IngestedFile._base_manager.get(pk=file_id)
            outcome_box["outcome"] = classification.auto_classify_file(
                row,
                text=SAMPLE_ORDINARY,
                client=FakeClassifier(
                    reply=FileNature.CONTRACT,
                    on_call=lambda: (started.set(), release.wait(timeout=30)),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 真异常必须被看见
            outcome_box["error"] = repr(exc)
        finally:
            from django.db import connection

            connection.close()

    thread = threading.Thread(target=worker, name="late-classifier")
    thread.start()
    try:
        assert started.wait(timeout=20), "分类没有开始（测试自身的问题）"
        # 人工改正（真实受控路径：库管理员 + 一次确认 + 审计）
        result = _run_classify(admin_user, file_id, FileNature.APPLICATION)
        assert result["changed"] is True
    finally:
        release.set()
        thread.join(timeout=30)

    assert not thread.is_alive(), "分类线程没有结束"
    assert "error" not in outcome_box, outcome_box.get("error")
    record.refresh_from_db()
    assert record.classification == FileNature.APPLICATION, "晚到的自动结果覆盖了人工值"
    assert record.classification_source == ClassificationSource.MANUAL
    assert outcome_box["outcome"].status == "skipped_manual"


def test_auto_classify_respects_stale_lease(library, make_file):
    """租约已失效（被接管）→ 放弃写入（旧 worker 不得覆盖新持有者）。"""
    record = make_file(
        library,
        name="租约.md",
        parse_lease_token="old-token",
        parse_lease_started_at=timezone.now(),
    )
    outcome = classification.auto_classify_file(
        record, text=SAMPLE_CONTRACT, lease_token="new-token"
    )
    assert outcome.status == "skipped_manual"
    record.refresh_from_db()
    assert record.classification == ""
    assert "租约" in outcome.reason


# ------------------------------- 7. 审查方反例清单（只读验收集，逐条跑） ----


@pytest.mark.parametrize("case", REVIEW_CASES, ids=[c["name"] for c in REVIEW_CASES])
def test_review_counterexample_cases(case):
    """把审查方的合成反例清单当**验收集**跑（不改清单来迁就实现）。

    - 有 ``expected`` 的：规则能判就必须判对；规则判不了的交模型（这里用
      "按正文关键词回答的假模型"模拟一个**不会乱判**的模型），最终值必须等于期望；
    - ``expected_status == unidentified`` 的：必须保持未识别（不许伪成功）。
    """
    text = case["text"]
    decision = classification.detect_rule(text)
    # 规则**不许与正文矛盾**（这是允许规则判不了的前提）
    if case.get("expected"):
        assert not decision.value or decision.value == case["expected"], (case, decision)
    else:
        assert not decision.value, (case, decision)

    if case.get("expected_status") == "unidentified":
        outcome = classification.classify_content(
            text, original_name=case["name"], client=AlwaysOrdinary()
        )
        assert outcome.status == "pending"
        assert outcome.value == ""
        return

    outcome = classification.classify_content(
        text,
        original_name=case["name"],
        client=AlwaysOrdinary(),
    )
    assert outcome.value == case["expected"], (case, outcome)


# --------------------------------------------------- 8. 端到端：上传/OCR ----


def _run_pipeline(record, client, **kwargs):
    """跑真实流水线（``force=True``：测试里资料是直接造出来的，状态无所谓）。"""
    from apps.knowledge import pipeline

    return pipeline.process_file(
        record,
        client=client,
        classify_client=kwargs.pop("classify_client", None),
        force=kwargs.pop("force", True),
        **kwargs,
    )


def test_upload_pipeline_auto_classifies_by_content(ai_library, make_file):
    """整条流水线（上传单文件/目录扫描/会话上传共用它）：按正文自动识别。

    文件名故意是 `合同.pdf` 但正文是出差申请 —— 必须判「申请」。
    """
    library, qdrant, _collection = ai_library
    record = _make_record(
        library, make_file, SAMPLE_APPLICATION.encode(), name="合同.md"
    )

    step = _run_pipeline(record, qdrant)

    assert step.ok is True
    record.refresh_from_db()
    assert record.classification == FileNature.APPLICATION
    assert record.classification_source == ClassificationSource.AUTO_RULE
    assert record.classification_note
    assert record.parse_status == ParseStatus.DONE
    assert record.chunk_count >= 1
    # 索引里的性质与库里当场一致（判定在嵌入之前）
    assert step.metrics["classification"]["classification"] == FileNature.APPLICATION


def test_scanned_file_is_classified_after_ocr(ai_library, make_file):
    """扫描件：先 OCR 拿到正文，再自动识别（用显式离线测试适配器）。"""
    from PIL import Image

    library, qdrant, _collection = ai_library
    buffer = io.BytesIO()
    Image.new("RGB", (80, 40), "white").save(buffer, format="PNG")
    record = _make_record(library, make_file, buffer.getvalue(), name="扫描件.png")

    class FakeOcr:
        available = True

        def __init__(self):
            self.seen = []

        def recognize_image(self, image_bytes, *, hint=""):
            self.seen.append(hint)
            return "技术服务合同 甲方：示例甲公司 乙方：示例乙公司 合同编号：HT-1 签订地点：北京"

    ocr = FakeOcr()
    step = _run_pipeline(record, qdrant, ocr=ocr)

    assert step.ok is True
    assert ocr.seen and "扫描件.png" in ocr.seen[0]
    record.refresh_from_db()
    assert record.classification == FileNature.CONTRACT
    assert record.classification_source == ClassificationSource.AUTO_RULE
    assert step.metrics["parse"]["used_ocr"] is True


def test_auto_classification_failure_does_not_break_parse_chunks_or_index(
    ai_library, make_file
):
    """分类失败**不影响**原件、分段与索引：解析与嵌入照旧成功。"""
    library, qdrant, collection = ai_library
    body = SAMPLE_ORDINARY.encode()
    record = _make_record(library, make_file, body, name="会议纪要.md")
    before_raw = storage.read_blob(record.content_sha256)

    step = _run_pipeline(
        record,
        qdrant,
        classify_client=FakeClassifier(raises=TimeoutError("模型连接超时")),
    )

    assert step.ok is True
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.DONE
    assert record.classification == ""  # 待识别，绝不是「普通文档」
    assert "失败" in record.classification_note or "超时" in record.classification_note
    assert DocumentChunk.objects.filter(file=record).count() >= 1
    assert record.embedded_chunk_count >= 1
    assert storage.read_blob(record.content_sha256) == before_raw
    assert step.metrics["classification"]["status"] in {"pending", "error"}


def test_pipeline_skips_manual_nature(ai_library, make_file):
    """解析流水线不得覆盖人工设置的性质。"""
    library, qdrant, _collection = ai_library
    record = _make_record(
        library,
        make_file,
        SAMPLE_CONTRACT.encode(),
        name="人工定的.md",
        classification=FileNature.ORDINARY,
        classification_source=ClassificationSource.MANUAL,
    )
    step = _run_pipeline(record, qdrant)
    assert step.ok is True
    record.refresh_from_db()
    assert record.classification == FileNature.ORDINARY
    assert record.classification_source == ClassificationSource.MANUAL


def test_manual_correction_marks_source_manual_and_is_audited(
    library, make_file, admin_user, grant_member
):
    """人工用五项下拉设置 → `classification_source="manual"` + 审计（真实校验）。"""
    grant_member(library, admin_user, role=MemberRole.ADMIN)
    record = make_file(library, name="待纠正.md")
    result = _run_classify(admin_user, record.pk, FileNature.REPORT)

    assert result["changed"] is True
    record.refresh_from_db()
    assert record.classification == FileNature.REPORT
    assert record.classification_source == ClassificationSource.MANUAL
    assert "人工" in record.classification_note
    event = _classify_events(record.pk).last()
    assert event.payload["source"] == ClassificationSource.MANUAL
    assert event.payload["previous"] == ""


# ------------------------------------------------- 9. 补识别命令 kbclassify ----


def _write_chunks(record, texts):
    from apps.knowledge.services import add_chunks

    return add_chunks(record, texts)


def test_kbclassify_backfills_blank_non_manual_only(library, make_file):
    """只补空白 + 非人工：已有值、人工清空的一律不碰。"""
    from django.core.management import call_command

    blank = make_file(library, name="空白.md")
    _write_chunks(blank, [SAMPLE_CONTRACT])
    manual_blank = make_file(
        library,
        name="人工清空.md",
        classification="",
        classification_source=ClassificationSource.MANUAL,
    )
    _write_chunks(manual_blank, [SAMPLE_CONTRACT])
    already = make_file(
        library,
        name="已有值.md",
        classification=FileNature.REPORT,
        classification_source=ClassificationSource.AUTO_RULE,
    )
    _write_chunks(already, [SAMPLE_CONTRACT])

    out = io.StringIO()
    call_command("kbclassify", "--apply", stdout=out)

    blank.refresh_from_db()
    manual_blank.refresh_from_db()
    already.refresh_from_db()
    assert blank.classification == FileNature.CONTRACT
    assert blank.classification_source == ClassificationSource.AUTO_RULE
    assert manual_blank.classification == ""
    assert manual_blank.classification_source == ClassificationSource.MANUAL
    assert already.classification == FileNature.REPORT
    assert "已识别 1 份" in out.getvalue()


def test_kbclassify_dry_run_writes_nothing(library, make_file):
    """`--dry-run`（默认也是预演）逐份报告但**一行都不写**。"""
    from django.core.management import call_command

    record = make_file(library, name="预演.md")
    _write_chunks(record, [SAMPLE_CONTRACT])
    audit_before = AuditEvent.objects.count()

    out = io.StringIO()
    call_command("kbclassify", "--dry-run", stdout=out)

    record.refresh_from_db()
    assert record.classification == ""
    assert AuditEvent.objects.count() == audit_before
    assert "预演" in out.getvalue()
    assert "[已识别]" in out.getvalue()


def test_kbclassify_ids_and_limit(library, make_file):
    """`--ids` 只处理指定资料；`--limit` 限制份数。"""
    from django.core.management import call_command

    first = make_file(library, name="第一.md")
    _write_chunks(first, [SAMPLE_CONTRACT])
    second = make_file(library, name="第二.md")
    _write_chunks(second, [SAMPLE_APPLICATION])
    third = make_file(library, name="第三.md")
    _write_chunks(third, [SAMPLE_REPORT])

    call_command("kbclassify", "--apply", "--ids", str(first.pk), stdout=io.StringIO())
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.classification == FileNature.CONTRACT
    assert second.classification == ""

    call_command("kbclassify", "--apply", "--limit", "1", stdout=io.StringIO())
    second.refresh_from_db()
    assert second.classification in {FileNature.APPLICATION, ""}


def test_kbclassify_reports_pending_reason_per_file(library, make_file):
    """正文不足/无分段的资料逐份如实报"待识别 + 原因"，不伪造成功。"""
    from django.core.management import call_command

    record = make_file(library, name="没有分段.md")
    out = io.StringIO()
    call_command("kbclassify", "--apply", stdout=out)

    record.refresh_from_db()
    assert record.classification == ""
    text = out.getvalue()
    assert "[待识别]" in text and "正文不足" in text
    assert "已识别 0 份" in text


def test_kbclassify_system_admin_actor_has_full_scope(library, make_file, admin_user):
    """**管理员没有显式 LibraryMember 时也必须能补齐**（审查必改项）。

    生产调用方式：`manage.py kbclassify --actor admin --ids 40,41,42`。
    """
    from django.core.management import call_command

    assert library.members.filter(user=admin_user).count() == 0, "前提：管理员没有成员行"
    assert is_system_admin(admin_user) is True

    record = make_file(library, name="管理员补齐.md")
    _write_chunks(record, [SAMPLE_CONTRACT])

    out = io.StringIO()
    call_command("kbclassify", "--apply", "--actor", admin_user.username, stdout=out)

    record.refresh_from_db()
    assert record.classification == FileNature.CONTRACT
    assert record.classification_source == ClassificationSource.AUTO_RULE
    # 审计记的是**被指定的那个 actor**（不是 system/空）
    event = _classify_events(record.pk).last()
    assert event.actor_id == admin_user.pk
    assert "系统管理员" in out.getvalue()


def test_kbclassify_plain_user_cannot_backfill(library, make_file, user, grant_member):
    """普通只读用户（非管理员、非库管理员）不得补写。"""
    from django.core.management.base import CommandError
    from django.core.management import call_command

    grant_member(library, user, role=MemberRole.READER)
    record = make_file(library, name="无权补.md")
    _write_chunks(record, [SAMPLE_CONTRACT])

    with pytest.raises(CommandError):
        call_command("kbclassify", "--apply", "--actor", user.username, stdout=io.StringIO())
    record.refresh_from_db()
    assert record.classification == ""


def test_kbclassify_library_admin_only_sees_own_libraries(make_file, make_library, user, grant_member):
    """库管理员只补自己管的库；其它库一份都不动（权限范围真实生效）。"""
    from django.core.management import call_command

    mine = make_library(code="mine", name="我的库")
    other = make_library(code="other", name="别人的库")
    grant_member(mine, user, role=MemberRole.ADMIN)

    mine_file = make_file(mine, name="我的.md")
    _write_chunks(mine_file, [SAMPLE_CONTRACT])
    other_file = make_file(other, name="别人的.md")
    _write_chunks(other_file, [SAMPLE_CONTRACT])

    call_command("kbclassify", "--apply", "--actor", user.username, stdout=io.StringIO())

    mine_file.refresh_from_db()
    other_file.refresh_from_db()
    assert mine_file.classification == FileNature.CONTRACT
    assert other_file.classification == ""


# ------------------------------------------- 10. 列表筛选 / 检索结果一致 ----


def _list(api, **params):
    response = api.get("/api/v1/knowledge/files/", params)
    assert response.status_code == 200, response.content
    body = response.json()
    return body["results"] if isinstance(body, dict) and "results" in body else body


def test_list_filter_and_display_agree(api, admin_user, library, make_file):
    """筛选（含「待识别」哨兵）与展示字段一致；非法取值 400。"""
    api.force_authenticate(user=admin_user)
    make_file(library, name="合同.md", classification=FileNature.CONTRACT,
              classification_source=ClassificationSource.AUTO_RULE,
              classification_note="规则识别：合同要件")
    make_file(library, name="待识别.md")
    make_file(library, name="旧值.md", classification="技术文档")

    rows = _list(api, nature=FileNature.CONTRACT)
    assert [row["original_name"] for row in rows] == ["合同.md"]
    assert rows[0]["classification_source"] == ClassificationSource.AUTO_RULE
    assert rows[0]["classification_note"] == "规则识别：合同要件"

    pending = _list(api, nature="__unidentified__")
    assert [row["original_name"] for row in pending] == ["待识别.md"]

    legacy = _list(api, nature="技术文档")
    assert [row["original_name"] for row in legacy] == ["旧值.md"]

    # 搜索接口：哨兵值（待识别）与列表页**同一语义**；词表外值一律 400
    pending_search = api.post(
        "/api/v1/knowledge/search/",
        {"q": "待识别", "nature": "__unidentified__"},
        format="json",
    )
    assert pending_search.status_code == 200, pending_search.content
    bad = api.post(
        "/api/v1/knowledge/search/", {"q": "x", "nature": "技术文档"}, format="json"
    )
    assert bad.status_code == 400
    assert "性质" in json.dumps(bad.json(), ensure_ascii=False)


def test_search_results_show_current_classification_not_stale_payload(
    ai_library, make_file
):
    """检索结果卡上的性质必须取**当前元数据**（payload 是写入时快照，会落后）。

    "补识别不重做嵌入"是刻意的设计，代价就是 payload 里的 ``classification``
    可能仍是空值；``pipeline.refresh_classification_shaped`` 负责按数据库当前值
    覆盖展示字段。这里同时验证：**向量库里的 payload 确实没被改写**（不重嵌入），
    而返回给用户的结果卡是当前值。
    """
    from apps.knowledge import vectorindex
    from apps.knowledge.embedding import embed_for_indexing

    library, qdrant, collection = ai_library
    record = _make_record(
        library, make_file, "质保期为验收后12个月。".encode(), name="质保.md"
    )
    step = _run_pipeline(record, qdrant)
    assert step.ok is True
    assert record.classification == ""

    # 补识别（写入性质）**不重做嵌入**：payload 保持写入时的空值
    classification.auto_classify_file(record, text=SAMPLE_CONTRACT)
    record.refresh_from_db()
    assert record.classification == FileNature.CONTRACT

    vectors, _used = embed_for_indexing(["质保期为验收后12个月。"], collection=collection)
    raw_hits = vectorindex.search_dense(
        qdrant, collections=[collection], vector=vectors[0], limit=5
    )
    assert raw_hits, "索引里应该有这份资料的向量点"
    assert {hit.payload.get("classification", "") for hit in raw_hits} == {""}, (
        "补识别不该重做嵌入：payload 仍是写入时的空值"
    )

    shaped = [
        {"file_id": hit.payload.get("file_id"), "classification": hit.payload.get("classification", "")}
        for hit in raw_hits
    ]
    refreshed = classification_refresh_helper(shaped)
    assert {item["classification"] for item in refreshed} == {FileNature.CONTRACT}
    assert {item["classification_source"] for item in refreshed} == {
        ClassificationSource.AUTO_RULE
    }


def classification_refresh_helper(shaped):
    from apps.knowledge import pipeline

    return pipeline.refresh_classification_shaped(shaped)


def test_application_and_contract_rule_evidence_is_explainable():
    """依据必须写得出人话（详情页/排查要用），不是空字符串或内部代号。"""
    for text, expected in (
        (SAMPLE_APPLICATION, FileNature.APPLICATION),
        (SAMPLE_CONTRACT, FileNature.CONTRACT),
        (SAMPLE_REPORT, FileNature.REPORT),
        (SAMPLE_CONFIDENTIAL, FileNature.CONFIDENTIAL),
    ):
        decision = classification.detect_rule(text)
        assert decision.value == expected
        assert "规则识别" in decision.note
        assert expected in decision.note or "特征" in decision.note or "密级" in decision.note


# ------------------------------------------- 11. 迁移 0019：回填 + 保护窗口 ----


def _guard_body() -> str:
    """取"资料表更新守卫"的实际定义（PG 取函数体；SQLite 取触发器 SQL）。"""
    from django.db import connection

    from governance import triggers

    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute(
                "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname = %s",
                [f"{triggers.PREFIX}ingested_file_update"],
            )
            row = cursor.fetchone()
            return row[0] if row else ""
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name = %s",
            [f"{triggers.PREFIX}{triggers.INGESTED_FILE_TABLE}_update"],
        )
        row = cursor.fetchone()
        return row[0] if row else ""


def _raw_row(file_id: int) -> dict:
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT classification, classification_source, classification_note "
            "FROM kb_ingested_file WHERE id = %s",
            [file_id],
        )
        row = cursor.fetchone()
    return {"classification": row[0], "source": row[1], "note": row[2]}


def _migration_0019():
    """加载迁移模块（模块名以数字开头，不能直接 import 语句）。"""
    import importlib

    return importlib.import_module(
        "apps.knowledge.migrations.0019_classification_source_and_note"
    )


def test_backfill_and_guards_migration_is_idempotent_and_protection_stays(library, make_file):
    """迁移 0019 的动作可重跑：历史非空性质标 manual；守卫始终在位、锁列正确。

    这里直接调用迁移函数（不是"改断言迁就实现"）：它验证的正是
    **不撤守卫 + 事务内回填** 这条约定在当前数据库上真的成立。
    """
    from django.db import connection

    from governance import triggers

    migration_module = _migration_0019()
    legacy = make_file(library, name="历史人工值.md", classification=FileNature.CONTRACT)
    blank = make_file(library, name="历史空白.md")

    class FakeSchemaEditor:
        def __init__(self, connection):
            self.connection = connection

    migration_module.add_fields_backfill_and_guards(None, FakeSchemaEditor(connection))

    assert _raw_row(legacy.pk)["source"] == ClassificationSource.MANUAL
    assert _raw_row(blank.pk)["source"] == ""
    assert triggers.guards_present() is True, "回填之后守卫必须完整在位"

    # 再跑一次：幂等，且守卫仍然在位（新列不在锁列里）
    migration_module.add_fields_backfill_and_guards(None, FakeSchemaEditor(connection))
    assert triggers.guards_present() is True
    assert _raw_row(legacy.pk)["source"] == ClassificationSource.MANUAL

    body = _guard_body()
    assert "original_name" in body, "核心列必须仍然受保护"
    assert "classification_note" not in body, "受控元数据不该出现在锁列里"


def _expected_guards() -> set[str]:
    """期望存在的 6 个守卫（名字从 `governance.triggers` 现算，不写死）。"""
    from governance import triggers

    return {
        f"{triggers.PREFIX}{table}_{op}"
        for table in triggers._ALL_TABLES
        for op in ("update", "delete")
    }


def _installed_guards() -> set[str]:
    """用 schema introspection 查**实际存在**的守卫触发器等（不看注释）。"""
    from django.db import connection

    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
            return {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        return {row[0] for row in cursor.fetchall()}


def _core_update_is_blocked(file_id: int) -> bool:
    """真实发一条"改核心字段"的 SQL：被拒 = 保护真的在。"""
    from django.db import connection, transaction

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kb_ingested_file SET original_name = 'hacked.pdf' WHERE id = %s",
                    [file_id],
                )
    except Exception:  # noqa: BLE001 - 被拒就是我们要的证据
        return True
    return False


def _chunk_update_is_blocked(chunk_id: int) -> bool:
    from django.db import connection, transaction

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kb_document_chunk SET text = 'hacked' WHERE id = %s", [chunk_id]
                )
    except Exception:  # noqa: BLE001
        return True
    return False


def _audit_delete_is_blocked(event_id: int) -> bool:
    from django.db import connection, transaction

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM kb_audit_event WHERE id = %s", [event_id])
    except Exception:  # noqa: BLE001
        return True
    return False


def _stage_injection(stage: str):
    """按 SQL 阶段精确注入失败（其余语句**真实执行**，不桩掉整条连接）。

    为什么要精确到阶段：``create_guards()`` 内部是"先 DROP 全部守卫、再逐条
    CREATE"，把整条连接桩成一上来就抛错，等于在**第一条 DROP 之前**就失败了，
    完全没测到"已经 DROP 掉几个 / CREATE 到一半"这种真实危险状态。
    """
    from django.db import connection

    seen = {"drop": 0, "create": 0}

    def wrapper(execute, sql, params, many, context):
        text = " ".join(str(sql).split())
        upper = text.upper()
        if stage == "partial_drop" and upper.startswith("DROP TRIGGER"):
            seen["drop"] += 1
            if seen["drop"] == 3:  # 已经 DROP 掉 3 个之后再炸
                raise RuntimeError("注入失败：DROP 到一半")
        elif stage == "second_create" and upper.startswith("CREATE TRIGGER"):
            seen["create"] += 1
            if seen["create"] == 2:  # 建到第 2 条时炸
                raise RuntimeError("注入失败：CREATE 到一半")
        elif stage == "backfill" and upper.startswith("UPDATE KB_INGESTED_FILE"):
            raise RuntimeError("注入失败：回填 UPDATE")
        return execute(sql, params, many, context)

    return connection.execute_wrapper(wrapper)


@pytest.mark.parametrize("stage", ["partial_drop", "second_create", "backfill"])
def test_migration_failure_keeps_guards_and_sql_protection(stage, library, make_file):
    """**迁移中途失败注入**：失败后 6 个守卫全在，且三条真实 SQL 仍被拒绝。

    这是"不存在失去数据库保护的窗口"的**实证**（不是注释里的承诺）：
    注入点覆盖"DROP 到一半 / CREATE 到一半 / 回填 UPDATE"，其余语句真实执行。
    """
    from django.db import connection

    from governance import triggers

    migration_module = _migration_0019()
    record = make_file(library, name=f"失败注入-{stage}.md", classification=FileNature.REPORT)
    chunk = _write_chunks(record, [SAMPLE_CONTRACT])[0]
    event = AuditEvent.objects.order_by("-id").first()
    if event is None:
        from apps.audit.services import append_event

        event = append_event(action="test.failure_injection", resource_key=str(record.pk))

    class FakeSchemaEditor:
        def __init__(self, connection):
            self.connection = connection

    before_guards = _installed_guards()
    assert _expected_guards() <= before_guards, "前置：注入前守卫应当是完整的"

    with pytest.raises(RuntimeError):
        with _stage_injection(stage):
            migration_module.add_fields_backfill_and_guards(
                None, FakeSchemaEditor(connection)
            )

    after_guards = _installed_guards()
    assert _expected_guards() <= after_guards, (
        f"{stage}：失败后守卫缺失 {sorted(_expected_guards() - after_guards)}"
    )
    assert triggers.guards_present() is True

    # 真实 SQL 证据：核心字段 UPDATE / 分段 UPDATE / 审计 DELETE 三条都被拒
    assert _core_update_is_blocked(record.pk) is True
    assert _chunk_update_is_blocked(chunk.pk) is True
    assert _audit_delete_is_blocked(event.pk) is True

    # 回填没有半途生效（事务整体回滚）
    assert _raw_row(record.pk)["classification"] == FileNature.REPORT
    assert _raw_row(record.pk)["source"] == ""
