"""受控删除**信息库**的专项回归（用户要求的验收点）。

设计口径（都已由后端实现，本文件把它锁死）：

* **只能走 planner 受控操作**：`delete_library` 需要 `library:admin` 权限、
  **三次独立确认**，且**最后一次必须准确输入目标名称**；
* **没有不受控的 destroy 端点**：库资源**没有** DELETE 路由，
  不可能用一个 HTTP 请求绕开确认、凭证与审计；
* **目标一致性**：库被改名或库内清单变化 → 旧提案必须重新发起；
* **只清目标库**：其它库及其资料/分段**原样保留**，绝不做全表删除；
* **诚实反馈**：向量/原件清理未全部完成时 `complete=False`（可重试），不谎报完成；
* **审计留痕**：授权与完成各有事件，含操作者与影响范围。

注意：本文件全部使用**隔离**测试数据（临时库/临时资料），绝不触碰主库。
"""

from __future__ import annotations

import pytest

from apps.audit.models import AuditEvent
from apps.knowledge.models import DocumentChunk, IngestedFile, KnowledgeLibrary
from apps.planner import services
from apps.planner.models import OperationRecord

pytestmark = pytest.mark.django_db


def _chunked(library, make_file, name: str, text: str = "受控删库测试内容"):
    """造一份带分段的资料（分段是删除时必须一起清掉的对象）。"""
    record = make_file(library=library, name=name)
    DocumentChunk.objects.create(
        file=record, ordinal=0, text=text, permission_code=f"library:{library.pk}"
    )
    IngestedFile._base_manager.filter(pk=record.pk).update(chunk_count=1)
    return record


def _propose(admin_user, library):
    proposal = services.propose_operation(
        admin_user, intent="delete_library", params={"library": library.code}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    return proposal, operation


def _confirm_three(admin_user, proposal, operation, typed_name: str = ""):
    """走完三次确认；`typed_name` 为空则用提案里的目标名（正确值）。

    注意：**第 3 次之后操作已 ARMED，响应里不再有 `next_confirm_token`** ——
    这里必须容忍最后一步没有下一张票（否则用例自己会 KeyError）。
    """
    token = proposal["confirm_token"]
    name = typed_name or proposal["target_label"]
    result = None
    for _ in range(services.DESTRUCTIVE_CONFIRMATIONS):
        result = services.confirm_operation(
            admin_user, operation, token=token, typed_name=name
        )
        token = result.get("next_confirm_token") or ""
    operation.refresh_from_db()
    return result


# --------------------------------------------------------------- 权限 ----


def test_non_admin_cannot_propose_delete_library(user, library):
    """普通用户（库成员）**不能**发起删库：需要 library:admin 权限。"""
    with pytest.raises(services.PlannerError) as exc:
        services.propose_operation(
            user, intent="delete_library", params={"library": library.code}
        )
    assert "权限" in str(exc.value)


def test_no_uncontrolled_destroy_endpoint(auth_api, admin_user, library):
    """**没有**不受控的 destroy 端点：库的写操作只在受控流程里。"""
    client = auth_api(admin_user)
    url = f"/api/v1/knowledge/libraries/{library.pk}/"
    # DRF 的 ViewSet 若挂了 destroy，DELETE 会返回 204/200；这里必须是"方法不允许/未找到"
    for method in ("delete", "put", "patch"):
        response = getattr(client, method)(url, {}, content_type="application/json")
        assert response.status_code in (403, 404, 405), (
            f"{method.upper()} {url} → {response.status_code}（不应存在不受控写入口）"
        )


# ------------------------------------------------------- 三次确认与名称 ----


def test_execution_requires_three_confirmations(admin_user, library, make_file):
    _chunked(library, make_file, name="need3.md")
    proposal, operation = _propose(admin_user, library)
    # 只确认 0 次 → 拒绝执行
    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(admin_user, operation)
    assert "确认" in str(exc.value)
    # 只确认 2 次 → 仍拒绝
    token = proposal["confirm_token"]
    for _ in range(2):
        step = services.confirm_operation(
            admin_user, operation, token=token, typed_name=proposal["target_label"]
        )
        token = step["next_confirm_token"]
    operation.refresh_from_db()
    with pytest.raises(services.PlannerError) as exc2:
        services.execute_operation(admin_user, operation)
    assert "确认" in str(exc2.value)
    assert KnowledgeLibrary.objects.filter(pk=library.pk).exists(), "未足次确认却删掉了库"


def test_final_confirmation_requires_exact_library_name(admin_user, library, make_file):
    """最后一次确认必须**准确输入目标名称**；错名/空名一律拒绝。"""
    _chunked(library, make_file, name="name-gate.md")
    proposal, operation = _propose(admin_user, library)
    token = proposal["confirm_token"]
    step1 = services.confirm_operation(admin_user, operation, token=token)
    step2 = services.confirm_operation(
        admin_user, operation, token=step1["next_confirm_token"]
    )
    # 第 3 次：空名称 → 拒绝
    with pytest.raises(services.PlannerError) as exc:
        services.confirm_operation(
            admin_user, operation, token=step2["next_confirm_token"], typed_name=""
        )
    assert "名称" in str(exc.value) or "输入" in str(exc.value)
    # 第 3 次：错误名称 → 拒绝
    with pytest.raises(services.PlannerError):
        services.confirm_operation(
            admin_user, operation, token=step2["next_confirm_token"], typed_name="不是这个库"
        )
    operation.refresh_from_db()
    assert operation.status != OperationRecord.Status.ARMED, "错名竟然被武装"
    assert KnowledgeLibrary.objects.filter(pk=library.pk).exists()


# --------------------------------------------------------- 正常删除路径 ----


def test_empty_library_can_be_deleted(admin_user, make_library):
    """空库：可以删除，且如实报告完成。"""
    library = make_library(code="empty-lib", name="空库")
    proposal, operation = _propose(admin_user, library)
    _confirm_three(admin_user, proposal, operation)
    result = services.execute_operation(admin_user, operation)["result"]
    assert result["deleted"] == "knowledge_library"
    assert not KnowledgeLibrary.objects.filter(pk=library.pk).exists()
    # 空库没有向量与原件要清 → 应报告完成（而不是含糊的 partial）
    assert result["complete"] is True, result


def test_library_with_files_deletes_records_and_chunks(admin_user, library, make_file):
    """有资料的库：资料记录与分段必须一并清除。"""
    first = _chunked(library, make_file, name="a.md")
    second = _chunked(library, make_file, name="b.md")
    file_ids = [first.pk, second.pk]
    assert DocumentChunk.objects.filter(file_id__in=file_ids).count() == 2

    proposal, operation = _propose(admin_user, library)
    _confirm_three(admin_user, proposal, operation)
    result = services.execute_operation(admin_user, operation)["result"]

    assert not KnowledgeLibrary.objects.filter(pk=library.pk).exists()
    assert not IngestedFile._base_manager.filter(pk__in=file_ids).exists(), "资料记录残留"
    assert DocumentChunk.objects.filter(file_id__in=file_ids).count() == 0, "分段残留"
    assert result["complete"] is True, result


def test_only_target_library_is_cleaned(admin_user, make_library, make_file):
    """**只清目标库**：另一个库及其资料/分段原样保留（不存在全表删除）。"""
    target = make_library(code="target-lib", name="要删的库")
    keeper = make_library(code="keeper-lib", name="必须保留的库")
    _chunked(target, make_file, name="t.md")
    keep_record = _chunked(keeper, make_file, name="k.md")
    keep_chunks = DocumentChunk.objects.filter(file=keep_record).count()

    files_before = IngestedFile._base_manager.count()
    libraries_before = KnowledgeLibrary.objects.count()

    proposal, operation = _propose(admin_user, target)
    _confirm_three(admin_user, proposal, operation)
    services.execute_operation(admin_user, operation)

    assert not KnowledgeLibrary.objects.filter(pk=target.pk).exists()
    assert KnowledgeLibrary.objects.filter(pk=keeper.pk).exists(), "误删了其它库"
    assert IngestedFile._base_manager.filter(pk=keep_record.pk).exists(), "误删了其它库的资料"
    assert DocumentChunk.objects.filter(file=keep_record).count() == keep_chunks
    # 只少了一个库与它自己的资料，**不是**全表清空
    assert KnowledgeLibrary.objects.count() == libraries_before - 1
    assert IngestedFile._base_manager.count() == files_before - 1


# ----------------------------------------------------------- 一致性/审计 ----


def test_repeat_execute_does_not_delete_twice(admin_user, make_library):
    """重复执行：第二次必须被拒绝（不是"再删一次"）。"""
    library = make_library(code="twice-lib", name="重复执行库")
    proposal, operation = _propose(admin_user, library)
    _confirm_three(admin_user, proposal, operation)
    services.execute_operation(admin_user, operation)
    operation.refresh_from_db()
    with pytest.raises(services.PlannerError):
        services.execute_operation(admin_user, operation)


def test_rename_after_proposal_requires_reproposal(admin_user, library, make_file):
    """提案后被改名 → 必须重新发起（旧确认不能用来删一个"不同"的目标）。"""
    _chunked(library, make_file, name="rename-lib.md")
    proposal, operation = _propose(admin_user, library)
    KnowledgeLibrary.objects.filter(pk=library.pk).update(name="改名后的库")
    with pytest.raises(services.PlannerError) as exc:
        services.confirm_operation(
            admin_user, operation, token=proposal["confirm_token"],
            typed_name=proposal["target_label"],
        )
    assert "名称" in str(exc.value) or "变化" in str(exc.value)


def test_manifest_change_requires_reproposal(admin_user, library, make_file):
    """提案后库内又多了一份资料 → 必须重新发起。"""
    _chunked(library, make_file, name="m1.md")
    proposal, operation = _propose(admin_user, library)
    _chunked(library, make_file, name="m2.md")
    with pytest.raises(services.PlannerError) as exc:
        services.confirm_operation(
            admin_user, operation, token=proposal["confirm_token"],
            typed_name=proposal["target_label"],
        )
    assert "清单" in str(exc.value) or "变化" in str(exc.value)


def test_audit_events_record_actor_and_scope(admin_user, library, make_file):
    """审计留痕：授权与完成都有事件，且写明影响范围。"""
    _chunked(library, make_file, name="audit.md")
    proposal, operation = _propose(admin_user, library)
    _confirm_three(admin_user, proposal, operation)
    services.execute_operation(admin_user, operation)

    authorized = AuditEvent.objects.filter(action="knowledge.library_delete_authorized")
    deleted = AuditEvent.objects.filter(action="knowledge.library_deleted")
    assert authorized.exists(), "缺少授权审计事件"
    assert deleted.exists(), "缺少完成审计事件"
    event = authorized.order_by("-id").first()
    assert event.actor_id == admin_user.pk
    assert event.resource_key == library.code
    assert event.payload.get("file_count") == 1
    assert event.payload.get("confirmations") == services.DESTRUCTIVE_CONFIRMATIONS


# ------------------------------------------- 独立审核补充（5 点） ----


def _collection(library, name: str, *, active: bool = False):
    from apps.knowledge.models import VectorCollection

    return VectorCollection.objects.create(
        name=name, library=library, is_active=active, build_status="done"
    )


def test_propose_returns_authoritative_impact(admin_user, library, make_file):
    """**影响范围由后端给出**：库名/id、资料数、**全部已登记集合**（含历史代次）。

    旧缺陷：前端按 `row.library === code` 过滤（集合里 `library` 是整数 pk）
    → 永远显示"无集合"；且 `page_size=500` 被服务端封顶 200 → 显示不全。
    """
    _chunked(library, make_file, name="i1.md")
    _chunked(library, make_file, name="i2.md")
    _collection(library, "kb-impact-g1", active=True)
    _collection(library, "kb-impact-g2")
    _collection(library, "kb-impact-g3")

    proposal = services.propose_operation(
        admin_user, intent="delete_library", params={"library": library.code}
    )
    impact = proposal["impact"]
    assert impact["library_id"] == library.pk          # 身份用 **id**，不是 code 字符串
    assert impact["code"] == library.code
    assert impact["name"] == library.name
    assert impact["file_count"] == 2
    # 三个代次**全部**在列（不受分页影响）
    assert impact["collections"] == ["kb-impact-g1", "kb-impact-g2", "kb-impact-g3"], impact
    assert impact["collection_count"] == 3
    assert impact["originals_note"]


def test_impact_lists_all_collections_even_beyond_page_size(admin_user, library):
    """集合数量超过任何前端分页上限时，影响范围仍然**完整**（权威来自后端）。"""
    total = 205
    for index in range(total):
        _collection(library, f"kb-many-{index:04d}", active=(index == 0))
    proposal = services.propose_operation(
        admin_user, intent="delete_library", params={"library": library.code}
    )
    impact = proposal["impact"]
    assert impact["collection_count"] == total, impact["collection_count"]
    assert len(set(impact["collections"])) == total
    assert "kb-many-0204" in impact["collections"]


def test_ordinary_library_admin_cannot_delete_library(make_library, make_file, grant_member, user):
    """**仅系统管理员**：普通库管理员（ADMIN 角色）不得整库删除。"""
    library = make_library(code="lib-admin-only", name="库管理员测试库")
    grant_member(library, user, role="admin")     # 只是**库**管理员，不是系统管理员
    _chunked(library, make_file, name="la.md")
    with pytest.raises(services.PlannerError) as exc:
        services.propose_operation(
            user, intent="delete_library", params={"library": library.code}
        )
    assert "系统管理员" in str(exc.value)


def test_delete_file_permission_is_unchanged(library, make_file, grant_member, user):
    """**不改 delete_file 权限**：库管理员仍可删除库内单份资料。"""
    grant_member(library, user, role="admin")
    record = _chunked(library, make_file, name="df.md")
    proposal = services.propose_operation(
        user, intent="delete_file", params={"file_id": record.pk}
    )
    assert proposal["operation_id"]
    assert services.get_spec("delete_file").permission == "library:admin"


def test_in_lock_recheck_refuses_file_added_after_check(admin_user, library, make_file, monkeypatch):
    """**竞态**：`_dispatch` 的检查通过后、取锁前有人上传 → 锁内复查必须拒绝。

    这里把 `_verify_target_unchanged` 打桩成 no-op，模拟"检查已经过了"，
    以此证明**锁内那道复查**真实存在且生效（否则会删掉用户没确认过的那一份）。
    """
    _chunked(library, make_file, name="race1.md")
    proposal, operation = _propose(admin_user, library)
    _confirm_three(admin_user, proposal, operation)

    monkeypatch.setattr(services, "_verify_target_unchanged", lambda _op: None)
    added = _chunked(library, make_file, name="race2.md")   # 检查之后、锁之前多了一份

    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(admin_user, operation)
    assert "变化" in str(exc.value) or "范围" in str(exc.value)
    assert KnowledgeLibrary.objects.filter(pk=library.pk).exists(), "竞态下仍然删了库"
    assert IngestedFile._base_manager.filter(pk=added.pk).exists(), "多出来的资料被误删"


def test_in_lock_recheck_refuses_collection_added_after_check(admin_user, library, monkeypatch):
    """**集合代次变化**同样属于确认范围变化 → 锁内复查必须拒绝。"""
    proposal, operation = _propose(admin_user, library)
    _confirm_three(admin_user, proposal, operation)

    monkeypatch.setattr(services, "_verify_target_unchanged", lambda _op: None)
    _collection(library, "kb-race-new-g1", active=True)

    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(admin_user, operation)
    assert "索引" in str(exc.value) or "变化" in str(exc.value)
    assert KnowledgeLibrary.objects.filter(pk=library.pk).exists()


def test_permission_revoked_before_execute_is_refused(db):
    """**权限撤销**：提案后失去系统管理员身份 → 执行被拒（每次核验，不是只看提案时）。"""
    import pytest as _pytest

    from django.contrib.auth import get_user_model

    from apps.knowledge.models import LibraryLevel

    user_model = get_user_model()
    operator = user_model.objects.create_user(username="sysop", password="pw-x-123")
    operator.is_staff = True
    operator.save(update_fields=["is_staff"])
    library = KnowledgeLibrary.objects.create(
        code="revoke-lib", name="撤销权限库", level=LibraryLevel.INTERNAL
    )
    proposal = services.propose_operation(
        operator, intent="delete_library", params={"library": library.code}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    token = proposal["confirm_token"]
    for _ in range(services.DESTRUCTIVE_CONFIRMATIONS):
        step = services.confirm_operation(
            operator, operation, token=token, typed_name=proposal["target_label"]
        )
        token = step.get("next_confirm_token") or ""
    # 提案/确认之后被撤销系统管理员身份
    operator.is_staff = False
    operator.is_superuser = False
    operator.save(update_fields=["is_staff", "is_superuser"])
    operation.refresh_from_db()
    with _pytest.raises(services.PlannerError) as exc:
        services.execute_operation(operator, operation)
    assert "系统管理员" in str(exc.value)
    assert KnowledgeLibrary.objects.filter(pk=library.pk).exists()
