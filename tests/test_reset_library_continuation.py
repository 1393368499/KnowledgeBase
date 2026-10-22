"""整库重建的**批次续做**专项（独立复现缺陷：超过 50 份时永远只跑前 50 份）。

独立复现的原始现象（`artifacts/acceptance/independent-rebuild-continuation-20261016.json`）：
51 份资料连续执行两轮，两轮都处理同样的前 50 份、都返回 `remaining=1, complete=false`，
第 51 份从未进入处理；界面却写着"可再执行一次续做"。根因是**没有持久续做游标**：
每次都从库内资料列表的第一份重来。

本文件把新口径钉死：

* **持久游标**：`LibraryRebuildRun` 落库记录范围指纹、每批进度、已重建 id、失败明细；
* **真正续做**：第二轮从游标接着跑（跳过已重建的），51 份必须能跑到 `complete`；
* **范围变动**：清单指纹变了 → 旧 run 作废（留审计）+ 旧确认必须重新提案；
* **中途失败**：失败明细落库、下一批继续重试，绝不冒充成功；
* **并发重入**：租约在飞时第二个执行者被明确拒绝；租约过期（进程被杀）允许接管且留痕；
* **只有全部成功才清旧代次**：缺原件等永久不可重建的份仍然阻止清理（安全规则不变）。

全部用隔离测试数据（临时原件与隔离向量目录），不触碰主库与真实 `media/raw`。
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.knowledge import storage
from apps.knowledge.models import (
    DocumentChunk,
    IngestedFile,
    KnowledgeLibrary,
    ParseStatus,
    VectorCollection,
)
from apps.knowledge.pipeline import PipelineError, StepResult
from apps.planner import services
from apps.planner.models import LibraryRebuildRun, OperationRecord

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolated_blobs(isolated_paths):
    """原件一律落在临时目录：绝不往真实 ``media/raw`` 写测试数据。"""
    return isolated_paths


# ------------------------------------------------------------------ 辅助 ----


def _original(library, make_file, name: str, text: str = "续做测试内容"):
    """造一份**有真原件、有分段**的资料（原件落在隔离目录里）。"""
    payload = f"{name}-{text}".encode()
    blob = storage.store_bytes(payload)
    record = make_file(library=library, name=name, sha=blob.sha256)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        raw_path=blob.rel_path, chunk_count=1
    )
    DocumentChunk.objects.create(
        file=record, ordinal=0, text=text, permission_code=f"library:{library.pk}"
    )
    record.refresh_from_db()
    return record


def _prepare(admin_user, library):
    """提案 + 三次确认（第 3 次准确输入库名），返回 operation。"""
    proposal = services.propose_operation(
        admin_user, intent="reset_library_index", params={"library": library.code}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    token = proposal["confirm_token"]
    for _ in range(services.DESTRUCTIVE_CONFIRMATIONS):
        step = services.confirm_operation(
            admin_user, operation, token=token, typed_name=proposal["target_label"]
        )
        token = step.get("next_confirm_token") or ""
    operation.refresh_from_db()
    return operation


def _ok_retry(monkeypatch, calls: list | None = None):
    def fake_retry(file, *, actor=None, profile=None, client=None):
        if calls is not None:
            calls.append(file.pk)
        return StepResult(ok=True, detail="fake rebuild")

    monkeypatch.setattr("apps.knowledge.pipeline.retry_file", fake_retry)


def _spy_drop(monkeypatch, dropped: list):
    def fake_drop(name: str):
        dropped.append(name)
        return {"ok": True, "name": name, "verified_absent": True}

    monkeypatch.setattr(services, "_drop_collection", fake_drop)


def _stale_collection(library, name: str) -> VectorCollection:
    """登记一个"旧的、不再生效"的代次（清理只应发生在全部成功之后）。"""
    return VectorCollection.objects.create(
        name=name, dimension=1024, library=library, is_active=False
    )


# --------------------------- 0) 真实 PG：清理 vs 激活/回退 并发（双连接 + 线程） ----


def test_switch_paths_refuse_retiring_collection(api, admin_user, library):
    """两条切换入口都必须拒绝**退役待删**的代次（审计发现的入口确认）。

    审计澄清（我原以为直连 PUT 是可用入口，实测不是）：
      * **直连** `PUT /knowledge/generations/` 被 `@immutable_endpoint` 拦成 403
        `immutable_resource` —— 这是**有意的**设计：切换只能走受控意图，
        `tests/test_agent_console_reachability.py` 也守着"前端不许直连 PUT"。
      * **用户实际路径**是受控意图 `activate_generation` / `rollback_generation`
        （`kb-pages.js` 的「设为生效」/「回退」），它们最终调用真实
        `vectorindex.activate_generation`，因此必须被退役判断挡住。
    """
    from apps.knowledge import vectorindex

    stale_name = vectorindex.active_collection_name(library.pk, 1)
    stale = _stale_collection(library, stale_name)
    stale.retiring_at = timezone.now()
    stale.save(update_fields=["retiring_at", "updated_at"])
    before = KnowledgeLibrary.objects.get(pk=library.pk)

    api.force_authenticate(user=admin_user)
    direct = api.put(
        "/api/v1/knowledge/generations/",
        {"library": library.code, "generation": 1, "collection": stale_name},
        format="json",
    )
    assert direct.status_code == 403, direct.content[:300]
    assert direct.json().get("code") == "immutable_resource", direct.json()

    # 受控意图：提案 → 一次确认 → 执行，必须因"退役"被拒。
    # 名字改成**不含测试适配器后缀**的形式：`_generation_of` 才能解析出代次，
    # 否则会在进入退役判断前以"解析不出代次"被拒，测不到目标分支。
    parsed_name = f"{vectorindex.collection_prefix()}-{library.pk}-g1"
    VectorCollection.objects.filter(pk=stale.pk).update(name=parsed_name)
    stale.refresh_from_db()

    # 受控意图：提案 → 一次确认 → 执行，必须因"退役"被拒
    proposal = services.propose_operation(
        admin_user,
        intent="activate_generation",
        params={"library": library.code, "collection": parsed_name},
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    services.confirm_operation(admin_user, operation, token=proposal["confirm_token"])
    operation.refresh_from_db()
    with pytest.raises(Exception) as exc:
        services.execute_operation(admin_user, operation)
    assert "退役" in str(exc.value), exc.value

    after = KnowledgeLibrary.objects.get(pk=library.pk)
    assert after.active_generation == before.active_generation
    assert after.vector_collection == before.vector_collection


@pytest.mark.django_db(transaction=True)
def test_cleanup_vs_activate_and_rollback_on_real_pg_threads(
    admin_user, library, make_file, monkeypatch
):
    """**真实 PostgreSQL + 两条连接 + 线程（事件同步，不靠 sleep）**：

    清理侧在独立连接/线程里跑**真实产品流程**（`reset_library_index`），并在
    "远端删除"这一步被事件拦住 —— 此时退役标记（`retiring_at`）已经提交。
    主线程用**另一条连接**调用**真实产品函数**：

      * `vectorindex.activate_generation`：必须被拒绝（不能把正在删的代次切上线）；
      * `vectorindex.rollback_generation`：同样必须被拒绝。

    然后放行清理，断言该代次确实被回收、库指针没有指向它。
    这样证明的是"持久退役状态 + 库行锁"带来的互斥，而不是"再查一次"的快照判断。
    """
    import threading

    from django.db import connections

    from apps.knowledge import vectorindex

    if connections["default"].vendor != "postgresql":
        pytest.skip("需要真实 PostgreSQL：SQLite 没有这种行级锁语义（不计入通过）")

    _ok_retry(monkeypatch)
    # 用**产品自己的命名函数**造这个名字：否则 `_generation_of` 解不出代次，
    # 回退路径会用另一个名字去查（得到"找不到可回退的 generation"而不是"退役"），
    # 那样就测不到目标分支了。
    stale_name = vectorindex.active_collection_name(library.pk, 1)
    stale = _stale_collection(library, stale_name)
    for index in range(2):
        _original(library, make_file, name=f"并发-{index}.md")

    # **前置条件要显式检查**：清理只在"范围内全部重建成功且没有永久不可重建"时发生；
    # 若夹具/环境让库里多出别的资料，事件永远等不到，测试就会**卡 60 秒才失败**
    # 且看不出原因（独立 Linux 全量跑时就撞到这一点）。这里先断言范围，再往下走。
    scope = list(
        IngestedFile._base_manager.filter(library=library).exclude(
            parse_status=ParseStatus.ARCHIVED
        )
    )
    assert len(scope) == 2, (
        "本用例假设库里只有它自己造的两份资料（清理需要 all_rebuilt）；"
        f"实际 {len(scope)} 份：{[row.original_name for row in scope]}"
    )

    dropping = threading.Event()
    release = threading.Event()
    outcome: dict = {}
    errors: list[BaseException] = []

    def slow_drop(name: str):
        # 走到这里说明：清理已在**事务锁内**置好退役标记并提交，正在做远端删除
        dropping.set()
        release.wait(timeout=60)
        return {"ok": True, "name": name, "verified_absent": True}

    monkeypatch.setattr(services, "_drop_collection", slow_drop)

    def run_cleanup() -> None:
        try:
            # 线程用的是**自己的**连接：把它的库身份记下来，失败时能一眼看出
            # "线程是不是连到了另一个库"（独立 Linux 全量跑时就是这么抓到的）。
            outcome["thread_db"] = {
                "name": str(connections["default"].settings_dict.get("NAME")),
                "host": str(connections["default"].settings_dict.get("HOST")),
                "vendor": connections["default"].vendor,
            }
            outcome["result"] = services.execute_operation(
                admin_user, _prepare(admin_user, library)
            )["result"]
        except BaseException as exc:  # noqa: BLE001 - 线程内异常带回主线程
            errors.append(exc)
        finally:
            connections.close_all()

    worker = threading.Thread(target=run_cleanup, name="cleanup-side", daemon=True)
    worker.start()
    try:
        # 等"到远端删除"或"清理侧提前结束"两者之一，**并把原因带进失败信息**：
        # 只等事件的话，遇到异常/未完成会白等 60 秒且看不到真实原因。
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if dropping.wait(timeout=0.5):
                break
            if not worker.is_alive():
                break
        assert dropping.is_set(), (
            "清理侧没有走到远端删除。真实原因："
            f"线程异常={errors!r}；返回结果={outcome.get('result')!r}；"
            f"线程连接={outcome.get('thread_db')!r}；"
            f"主线程连接={connections['default'].settings_dict.get('NAME')!r}"
        )
        stale.refresh_from_db()
        assert stale.retiring_at is not None, "清理侧没有落下退役标记"
        library.refresh_from_db()
        plan = vectorindex.GenerationPlan(
            library_id=library.pk,
            generation=vectorindex._generation_of(library, stale_name) or 1,
            collection=stale_name,
            dimension=int(stale.dimension or 1024),
            model=stale.embedding_model or "",
            model_version=stale.model_version or "",
            previous_generation=int(library.active_generation or 0),
            previous_collection=library.vector_collection or "",
            created=False,
        )
        # ② 另一条连接（主线程）上调用真实 activate：必须被拒
        with pytest.raises(Exception) as exc_activate:
            vectorindex.activate_generation(library, plan)
        assert "退役" in str(exc_activate.value), exc_activate.value
        # ③ 回退到同一个待删代次也必须被拒。
        #    代次号取 **1**，与上面 `active_collection_name(pk, 1)` 保持同名
        #    （`_generation_of` 解析不了测试适配器后缀，不能拿它当回退参数，
        #     否则会走进"找不到该 generation"的另一个分支，测不到目标行为）。
        with pytest.raises(Exception) as exc_rollback:
            vectorindex.rollback_generation(library, to_generation=1)
        assert "退役" in str(exc_rollback.value), exc_rollback.value
    finally:
        release.set()
        worker.join(timeout=90)
    assert not errors, errors
    assert not worker.is_alive()

    result = outcome.get("result") or {}
    assert result.get("complete") is True, result
    assert not VectorCollection.objects.filter(
        library=library, name=stale_name
    ).exists(), "清理没有回收这个旧代次"
    library.refresh_from_db()
    assert library.vector_collection != stale_name, "库指针指向了被删除的代次"


# --------------------------------------------------- 1) 51 份真正续做（契约级） ----


def test_51_files_continue_across_batches_without_repeating(
    admin_user, library, make_file, monkeypatch
):
    """51 份 → 第一轮 50 份、第二轮恰好 1 份并完成；**没有任何一份被重建两次**。"""
    _ok_retry(monkeypatch)
    dropped: list[str] = []
    _spy_drop(monkeypatch, dropped)
    _stale_collection(library, f"kb-{library.pk}-g1")

    for index in range(51):
        _original(library, make_file, name=f"续做-{index:02d}.md")

    first = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]
    assert first["rebuilt"] == 50
    assert first["remaining"] == 1
    assert first["complete"] is False
    assert first["rebuild_run"]["batches_done"] == 1
    assert first["rebuild_run"]["batch_this_run"] == 50
    assert dropped == [], "第一轮没跑完就清理旧代次了"

    run = LibraryRebuildRun.objects.get(pk=first["rebuild_run"]["run_id"])
    assert run.status == LibraryRebuildRun.Status.RUNNING
    assert run.batches_done == 1
    assert len(run.processed_ids) == 50
    assert run.lease_expires_at is None, "本批结束后应释放租约，好让下一次续做"

    second = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]
    assert second["rebuilt"] == 1, "第二轮应该只重建剩下的那 1 份"
    assert second["remaining"] == 0
    assert second["complete"] is True
    assert second["rebuild_run"]["run_id"] == first["rebuild_run"]["run_id"], "应当是同一个 run 续做"
    assert second["rebuild_run"]["resumed"] is True
    assert second["rebuild_run"]["batches_done"] == 2
    assert dropped == [f"kb-{library.pk}-g1"], "全部成功后才清理旧代次"

    run.refresh_from_db()
    assert run.status == LibraryRebuildRun.Status.COMPLETE
    processed = [int(value) for value in run.processed_ids]
    assert len(processed) == 51 and len(set(processed)) == 51, "有资料被重复处理或漏处理"
    ids = list(
        IngestedFile._base_manager.filter(library=library).values_list("pk", flat=True)
    )
    assert sorted(processed) == sorted(ids)
    # 续做留痕：授权 + 完成事件都在
    actions = set(
        AuditEvent.objects.filter(resource_key=library.code).values_list("action", flat=True)
    )
    assert "knowledge.library_index_reset_authorized" in actions
    assert "knowledge.library_index_reset" in actions


# ------------------------------------------- 2) 51 份**真实流水线**专项 ----


class _KeepAliveClient:
    """转发一切调用，但 ``close()`` 不真的关（共享夹具客户端不能被关掉）。"""

    def __init__(self, inner):
        self._inner = inner

    def close(self) -> None:
        return None

    def __getattr__(self, item):
        return getattr(self._inner, item)


def test_real_pipeline_51_files_two_batches_all_indexed(
    admin_user, library, make_file, isolated_paths, library_setup_vector
):
    """**真实**流水线 51 份：真原件 → 真解析/分段 → 真向量写入，两批跑完全部完成。

    这一条不替换任何产品逻辑（只有"批次上限"仍按产品默认 50），证明续做走的就是
    生产那条 ``retry_file(force=True)`` 路径；并在完成后核对点数、分段与原件字节。
    """
    from apps.knowledge import pipeline as pipeline_module
    from apps.knowledge import testing_adapter as ta, vectorindex
    from apps.knowledge.embedding import active_profile

    client = library_setup_vector
    collection = ta.test_collection_name(library.pk, 1)
    vectorindex.ensure_collection(client, collection, 1024)
    profile = active_profile()
    VectorCollection.objects.create(
        name=collection,
        dimension=1024,
        embedding_model=profile.model,
        model_version=profile.model_version,
        profile=profile.name,
        library=library,
        storage_mode="embedded",
        storage_path=str(isolated_paths["vector"]),
        is_active=True,
    )
    KnowledgeLibrary.objects.filter(pk=library.pk).update(
        active_generation=1, vector_collection=collection
    )
    library.refresh_from_db()

    records = []
    for index in range(51):
        body = (
            f"# 续做真实用例 {index}\n\n质保期：设备验收合格之日起 12 个月。\n"
        ).encode()
        stored = storage.store_bytes(body)
        record = make_file(
            library=library,
            name=f"real-cont-{index:02d}.md",
            sha=stored.sha256,
            size_bytes=stored.size_bytes,
            raw_path=stored.rel_path,
            raw_size_bytes=stored.size_bytes,
            status=ParseStatus.DONE,  # 已入库的资料才谈得上"重建索引"（PENDING 会被明确拒绝重试）
            vector_collection_name=collection,
        )
        IngestedFile._base_manager.filter(pk=record.pk).update(
            vector_generation=1, vector_collection_name=collection
        )
        records.append(record)
    records = list(IngestedFile._base_manager.filter(library=library).order_by("pk"))
    raw_before = {
        record.pk: storage.read_blob(record.content_sha256) for record in records
    }

    original_factory = pipeline_module.QDRANT_CLIENT_FACTORY
    pipeline_module.QDRANT_CLIENT_FACTORY = lambda: _KeepAliveClient(client)
    try:
        first = services.execute_operation(
            admin_user, _prepare(admin_user, library)
        )["result"]
        assert first["rebuilt"] == 50, {
            "rebuilt": first["rebuilt"],
            "remaining": first["remaining"],
            "failed": first["failed"],
            "incomplete": first["incomplete"],
            "lease_busy": first["lease_busy"],
            "unrebuildable_count": len(first["unrebuildable"]),
            "items": [(item.get("status"), item.get("detail")) for item in first["items"][:2]],
        }
        assert first["remaining"] == 1
        assert first["complete"] is False
        second = services.execute_operation(
            admin_user, _prepare(admin_user, library)
        )["result"]
    finally:
        pipeline_module.QDRANT_CLIENT_FACTORY = original_factory

    assert second["rebuilt"] == 1, second
    assert second["complete"] is True, second

    done = list(IngestedFile._base_manager.filter(library=library))
    assert len(done) == 51
    total_points = 0
    for record in done:
        record.refresh_from_db()
        assert record.parse_status == ParseStatus.DONE, f"{record.pk} 未完成解析"
        assert record.vector_status == "indexed", f"{record.pk} 未写入向量"
        assert record.chunk_count > 0
        assert (
            DocumentChunk.objects.filter(file=record).count() == record.chunk_count
        ), f"{record.pk} 分段数与记录不一致"
        total_points += record.chunk_count
        assert storage.read_blob(record.content_sha256) == raw_before[record.pk], "原件被改"
        assert storage.verify_blob(record.content_sha256) is True
    assert vectorindex.count_points(client, collection) == total_points

    run = LibraryRebuildRun.objects.get(library=library)
    assert run.status == LibraryRebuildRun.Status.COMPLETE
    assert len(set(int(value) for value in run.processed_ids)) == 51


# --------------------------------------------------- 3) 范围变动 → 作废重来 ----


def test_scope_change_supersedes_old_run_and_needs_new_proposal(
    admin_user, library, make_file, monkeypatch
):
    _ok_retry(monkeypatch)
    monkeypatch.setattr(services, "RESET_LIBRARY_LIMIT", 2, raising=False)
    for index in range(3):
        _original(library, make_file, name=f"范围-{index}.md")

    first_op = _prepare(admin_user, library)
    first = services.execute_operation(admin_user, first_op)["result"]
    assert first["rebuilt"] == 2 and first["remaining"] == 1
    old_run_id = first["rebuild_run"]["run_id"]

    # ① 确认之后范围又变了：拿这份确认执行必须被拒绝（范围不能被悄悄扩大）
    _original(library, make_file, name="范围-新增.md")
    stale_op = _prepare(admin_user, library)  # 记录的是"新增 1 份之后"的清单
    _original(library, make_file, name="范围-再加一份.md")  # 清单再变
    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(admin_user, stale_op)
    assert "变化" in str(exc.value) and "重新发起提案" in str(exc.value)

    # ② 重新提案后执行：旧 run（旧范围指纹）作废并留审计，新 run 从新范围重来
    second = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]
    old_run = LibraryRebuildRun.objects.get(pk=old_run_id)
    assert old_run.status == LibraryRebuildRun.Status.SUPERSEDED
    assert "范围发生变化" in old_run.note
    assert second["rebuild_run"]["run_id"] != old_run_id
    assert second["rebuild_run"]["batches_done_before"] == 0
    assert (
        AuditEvent.objects.filter(action="knowledge.library_rebuild_run_superseded").count()
        >= 1
    )


# --------------------------------------------------- 4) 并发重入与租约接管 ----


def test_concurrent_reentry_is_refused_while_lease_active(
    admin_user, library, make_file, monkeypatch
):
    _ok_retry(monkeypatch)
    monkeypatch.setattr(services, "RESET_LIBRARY_LIMIT", 2, raising=False)
    for index in range(3):
        _original(library, make_file, name=f"并发-{index}.md")

    first = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]
    run_id = first["rebuild_run"]["run_id"]
    # 模拟"另一个执行者正在跑"：把租约设到未来
    LibraryRebuildRun.objects.filter(pk=run_id).update(
        lease_expires_at=timezone.now() + timedelta(minutes=10)
    )

    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(admin_user, _prepare(admin_user, library))
    assert "正在执行中" in str(exc.value) and "并发" in str(exc.value)

    run = LibraryRebuildRun.objects.get(pk=run_id)
    assert run.batches_done == 1, "被拒绝的执行不得推进批次"
    assert len(run.processed_ids) == 2


def test_expired_lease_is_taken_over_and_continues(
    admin_user, library, make_file, monkeypatch
):
    _ok_retry(monkeypatch)
    monkeypatch.setattr(services, "RESET_LIBRARY_LIMIT", 2, raising=False)
    for index in range(3):
        _original(library, make_file, name=f"接管-{index}.md")

    first = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]
    run_id = first["rebuild_run"]["run_id"]
    # 模拟上一次执行被硬杀：租约停在过去
    LibraryRebuildRun.objects.filter(pk=run_id).update(
        lease_expires_at=timezone.now() - timedelta(minutes=1)
    )

    second = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]
    assert second["rebuild_run"]["run_id"] == run_id, "接管应继续原来的 run"
    assert second["rebuilt"] == 1 and second["complete"] is True
    assert (
        AuditEvent.objects.filter(action="knowledge.library_rebuild_run_taken_over").count() == 1
    )


# --------------------------------------------------- 5) 中途失败可续做 ----


def test_failed_file_is_recorded_and_retried_in_next_batch(
    admin_user, library, make_file, monkeypatch
):
    """本批失败：明细落库、剩余数不减、下一批继续重试 —— 绝不冒充成功。"""
    fail_id: dict[str, int] = {}

    def flaky_retry(file, *, actor=None, profile=None, client=None):
        if file.pk == fail_id.get("pk"):
            raise PipelineError("模型服务不可用：连接被拒绝")
        return StepResult(ok=True, detail="ok")

    for index in range(3):
        _original(library, make_file, name=f"失败-{index}.md")
    monkeypatch.setattr(services, "RESET_LIBRARY_LIMIT", 2, raising=False)
    target = IngestedFile._base_manager.filter(library=library).order_by("pk")[1]
    fail_id["pk"] = target.pk
    monkeypatch.setattr("apps.knowledge.pipeline.retry_file", flaky_retry)

    first = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]
    assert first["complete"] is False
    assert first["rebuild_run"]["batches_done"] == 1
    run = LibraryRebuildRun.objects.get(pk=first["rebuild_run"]["run_id"])
    assert [item["id"] for item in run.failed] == [target.pk]
    assert "不可用" in run.failed[0]["reason"]
    assert target.pk not in [int(value) for value in run.processed_ids]

    # 故障恢复：下一批把它重试成功
    fail_id.clear()
    second = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]
    assert second["complete"] is True, second
    run.refresh_from_db()
    assert target.pk in [int(value) for value in run.processed_ids]
    assert run.failed == []


def test_second_batch_does_not_rebuild_already_done_files(
    admin_user, library, make_file, monkeypatch
):
    """把"每份只重建一次"钉死：第二轮调用的 id 集合与第一轮**不相交**。"""
    calls: list[int] = []
    _ok_retry(monkeypatch, calls)
    monkeypatch.setattr(services, "RESET_LIBRARY_LIMIT", 2, raising=False)
    for index in range(5):
        _original(library, make_file, name=f"游标-{index}.md")

    services.execute_operation(admin_user, _prepare(admin_user, library))
    first_call_ids = set(calls)
    calls.clear()
    services.execute_operation(admin_user, _prepare(admin_user, library))
    second_call_ids = set(calls)

    assert first_call_ids and second_call_ids
    assert first_call_ids & second_call_ids == set(), "续做重复重建了已经重建过的资料"
    assert len(first_call_ids) == 2 and len(second_call_ids) == 2


# ------------------------------------------- 6) 清理旧代次与切换互斥（TOCTOU） ----


def test_cleanup_marks_retiring_before_remote_drop(
    admin_user, library, make_file, monkeypatch
):
    """删除旧代次：**先标记退役待删**（持久状态），远端确认删掉后才删登记行。

    这是与 `activate_generation` / `rollback_generation` 的互斥证明：两边都要先锁
    同一条 `KnowledgeLibrary` 行；等锁释放时该代次已是**退役待删**，而激活/回退都
    明确拒绝退役状态（`vectorindex.validate_activation` / `rollback_generation`），
    因此它**不可能**再被切上线 —— 不是"再查一次"的快照式判断。
    同时（独立审查第 2 项）**不先丢登记行**：先丢会造出 drop 失败后谁也找不到的孤儿。
    """
    _ok_retry(monkeypatch)
    stale_name = f"kb-{library.pk}-g1"
    _stale_collection(library, stale_name)
    for index in range(2):
        _original(library, make_file, name=f"互斥-{index}.md")

    seen: dict[str, object] = {}

    def spy_drop(name: str):
        row = VectorCollection.objects.filter(library=library, name=name).first()
        seen["registered_at_drop"] = row is not None
        seen["retiring_at_drop"] = bool(row is not None and row.retiring_at)
        seen["name"] = name
        return {"ok": True, "name": name, "verified_absent": True}

    monkeypatch.setattr(services, "_drop_collection", spy_drop)
    result = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]

    assert result["complete"] is True, result
    assert seen.get("name") == stale_name
    assert seen.get("registered_at_drop") is True, "登记行被提前丢了（drop 失败就再也找不到它）"
    assert seen.get("retiring_at_drop") is True, "远端删除时没有退役标记 → 仍可能被激活"
    assert not VectorCollection.objects.filter(library=library, name=stale_name).exists()


def test_failed_remote_drop_keeps_registration_and_is_retried(
    admin_user, library, make_file, monkeypatch
):
    """远端删除**第一次失败**：登记行与退役状态都保留、如实报错；**第二次成功**才回收。

    （独立审查要求的"远端第一次失败第二次成功回收"专项。）
    """
    _ok_retry(monkeypatch)
    stale_name = f"kb-{library.pk}-g1"
    _stale_collection(library, stale_name)
    for index in range(2):
        _original(library, make_file, name=f"重试-{index}.md")

    attempts: list[str] = []

    def flaky_drop(name: str):
        attempts.append(name)
        if len(attempts) == 1:
            return {"ok": False, "name": name, "error": "模拟远端删除失败"}
        return {"ok": True, "name": name, "verified_absent": True}

    monkeypatch.setattr(services, "_drop_collection", flaky_drop)
    first = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]

    # 失败后：登记还在，且处于退役状态；结果里**如实**说明可重试
    row = VectorCollection.objects.filter(library=library, name=stale_name).first()
    assert row is not None, "drop 失败却把唯一登记丢了 → 无法重试的孤儿"
    assert row.retiring_at is not None, "drop 失败后没有保留退役状态"
    assert stale_name in first["steps"]["vectors"]["stale_skipped"]
    assert "重试" in first["steps"]["vectors"]["stale_skipped"][stale_name]
    assert first["complete"] is False, "远端删除失败不能报成完全成功"

    # 第二次：**同一入口**重新捡起退役集合重试，成功后登记才消失
    second = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]
    assert attempts.count(stale_name) >= 2, f"没有重试：{attempts}"
    assert not VectorCollection.objects.filter(library=library, name=stale_name).exists()
    assert second["complete"] is True, "重试成功后应当可以完成"


def test_retiring_collection_cannot_be_activated(admin_user, library, make_file):
    """退役待删的代次被 `validate_activation` 明确拒绝（不能切上线）。"""
    from apps.knowledge import vectorindex

    stale_name = f"kb-{library.pk}-g1"
    row = _stale_collection(library, stale_name)
    row.retiring_at = timezone.now()
    row.save(update_fields=["retiring_at", "updated_at"])

    plan = vectorindex.GenerationPlan(
        library_id=library.pk,
        generation=vectorindex._generation_of(library, stale_name) or 1,
        collection=stale_name,
        dimension=int(row.dimension or 1024),
        model=row.embedding_model or "",
        model_version=row.model_version or "",
        previous_generation=int(library.active_generation or 0),
        previous_collection=library.vector_collection or "",
        created=False,
    )
    with pytest.raises(Exception) as exc:
        vectorindex.validate_activation(library, plan, verify_target_coverage=False)
    assert "退役" in str(exc.value), exc.value


def test_cleanup_skips_collection_that_is_the_current_pointer(
    admin_user, library, make_file, monkeypatch
):
    """锁内复核发现该集合就是库指针/仍被资料引用 → **跳过并如实报原因**，绝不删。"""
    _ok_retry(monkeypatch)
    stale_name = f"kb-{library.pk}-g1"
    _stale_collection(library, stale_name)
    # 把它设成库指针（不经 activate_generation，直接改指针来模拟"刚被切过去"）
    KnowledgeLibrary.objects.filter(pk=library.pk).update(vector_collection=stale_name)
    for index in range(2):
        _original(library, make_file, name=f"指针-{index}.md")
    dropped: list[str] = []
    _spy_drop(monkeypatch, dropped)

    result = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]

    assert dropped == [], "把库指针指向的集合删掉了"
    assert stale_name in result["steps"]["vectors"]["stale_skipped"]
    assert "库指针" in result["steps"]["vectors"]["stale_skipped"][stale_name]


# ------------------------------------------- 7) 归档排除 / 在飞 / 旧 owner ----

def test_archived_files_are_excluded_from_rebuild_scope(
    admin_user, library, make_file, monkeypatch
):
    """归档资料**不参与重建**（归档语义=移出检索），而且它不阻塞游标完成。"""
    calls: list[int] = []
    _ok_retry(monkeypatch, calls)
    live = [_original(library, make_file, name=f"在库-{index}.md") for index in range(2)]
    archived = _original(library, make_file, name="已归档.md")
    archived.update_parse_status(ParseStatus.ARCHIVED)

    result = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]

    assert archived.pk not in calls, "归档资料被重建了（它不该参与）"
    assert set(calls) == {row.pk for row in live}
    # 范围 = 未归档资料（2 份）；归档件既不参与重建，也不计入本次重建范围
    assert result["checked"] == 2, result.get("checked")
    assert result["complete"] is True, "归档件不该让重建永远完不成"
    run = LibraryRebuildRun.objects.get(pk=result["rebuild_run"]["run_id"])
    assert run.scope_size == 2, "重建范围（scope_size）必须排除归档件"
    archived.refresh_from_db()
    assert archived.parse_status == ParseStatus.ARCHIVED


def test_in_flight_lease_blocks_even_when_manifest_changed(
    admin_user, library, make_file, monkeypatch
):
    """**在飞租约优先于范围变化**：旧批次还在跑时，哪怕范围也变了也必须先拒绝。

    （否则会出现"旧批次还在写、新批次又开一批"的并发写 —— 最危险的竞态。）
    """
    _ok_retry(monkeypatch)
    monkeypatch.setattr(services, "RESET_LIBRARY_LIMIT", 2, raising=False)
    for index in range(3):
        _original(library, make_file, name=f"在飞-{index}.md")
    first = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]
    run_id = first["rebuild_run"]["run_id"]
    LibraryRebuildRun.objects.filter(pk=run_id).update(
        lease_expires_at=timezone.now() + timedelta(minutes=10)
    )
    # 范围也变了（新增一份）→ 仍必须先拒绝，而不是作废旧 run 再开新的
    _original(library, make_file, name="在飞-新增.md")
    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(admin_user, _prepare(admin_user, library))
    assert "正在执行中" in str(exc.value)
    run = LibraryRebuildRun.objects.get(pk=run_id)
    assert run.status == LibraryRebuildRun.Status.RUNNING, "在飞的 run 不能被作废"
    assert run.batches_done == 1


def test_old_owner_token_cannot_write_progress_after_takeover(
    admin_user, library, make_file, monkeypatch
):
    """接管之后，旧 owner 的 CAS 写进度必须失败（不能用陈旧进度覆盖新执行者）。"""
    _ok_retry(monkeypatch)
    monkeypatch.setattr(services, "RESET_LIBRARY_LIMIT", 2, raising=False)
    for index in range(3):
        _original(library, make_file, name=f"旧owner-{index}.md")
    first = services.execute_operation(admin_user, _prepare(admin_user, library))["result"]
    run_id = first["rebuild_run"]["run_id"]
    old_token = LibraryRebuildRun.objects.get(pk=run_id).owner_token
    assert old_token

    # 租约过期 → 第二个执行者接管（owner_token 换人）
    LibraryRebuildRun.objects.filter(pk=run_id).update(
        lease_expires_at=timezone.now() - timedelta(minutes=1)
    )
    services.execute_operation(admin_user, _prepare(admin_user, library))
    run = LibraryRebuildRun.objects.get(pk=run_id)
    assert run.owner_token and run.owner_token != old_token

    before = list(run.processed_ids)
    wrote = services._cas_run_progress(run_id, old_token, processed_ids=[999_999])
    assert wrote is False, "旧 owner 竟然还能写进度"
    run.refresh_from_db()
    assert list(run.processed_ids) == before, "陈旧的进度覆盖了新执行者的结果"
