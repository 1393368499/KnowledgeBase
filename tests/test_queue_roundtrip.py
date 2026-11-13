"""队列往返测试：真实派发 + 重复派发只处理一次。

口径说明
--------
- **离线可跑**：`CELERY_TASK_ALWAYS_EAGER=True` 时 `apply_async` 会同步执行，
  所以这里验证的是"任务真的被派发、真的执行、状态真的落库"这条链路，
  **不假装**测过真实 broker。
- **真实 broker 往返**标了 ``real_broker``：需要 Redis 在跑，用
  ``--kb-broker`` 开（或设 ``KB_TEST_BROKER=1``）。它才真正走
  Redis 投递 → 进程内 worker 执行。
- 重复派发只处理一次：靠**数据库 CAS 租约** + "已 DONE 就跳过"两道保证。
"""

from __future__ import annotations

import os
import socket

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.knowledge import pipeline
from apps.knowledge.models import IngestedFile, ParseStatus

pytestmark = pytest.mark.django_db


def _redis_reachable() -> bool:
    host, _, port = os.environ.get("KB_REDIS_URL", "redis://127.0.0.1:56379/0").split("//")[-1].partition(":")
    port = port.split("/")[0] or "6379"
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=1.5):
            return True
    except OSError:
        return False


@pytest.fixture
def uploaded_file(api, admin_user, library, isolated_paths):
    """通过真实 multipart 上传一份资料（走 upload 接口，不手工造模型）。

    注意：这里**不依赖 .env 的 KB_UPLOAD_PROCESS_MODE**——本地为了浏览器验收
    常把它设成 inline，那样上传时就已经处理完了，队列用例就测不到"待处理 → 任务接手"
    这条链路。所以显式用 queue 模式。
    """
    api.force_authenticate(user=admin_user)
    with override_settings(KB_UPLOAD_PROCESS_MODE="queue", CELERY_TASK_ALWAYS_EAGER=False):
        response = api.post(
            "/api/v1/knowledge/files/upload/",
            {
                "library": library.code,
                "file": SimpleUploadedFile("queue.txt", "质保期 12 个月".encode()),
            },
            format="multipart",
        )
    assert response.status_code in {200, 201, 202}, response.content[:400]
    return IngestedFile.objects.get(pk=response.json()["file"]["id"])


# ------------------------------------------------------------ 离线往返 ----


@override_settings(
    KB_UPLOAD_PROCESS_MODE="queue",
    CELERY_TASK_ALWAYS_EAGER=True,
    KB_TEST_ADAPTER="offline",
)
def test_task_roundtrip_via_eager(uploaded_file, library_setup_vector):
    """eager 往返：任务被派发 → 真的执行 → 状态落库为 done。"""
    from knowledgebase.celery import process_ingested_file

    record = uploaded_file
    assert record.parse_status == ParseStatus.PENDING

    result = process_ingested_file.apply(
        args=[record.pk],
        kwargs={
            "content_sha256": record.content_sha256,
            "library_code": record.library.code,
        },
    ).get()

    record.refresh_from_db()
    assert result["ok"] is True, result
    assert record.parse_status == ParseStatus.DONE
    assert record.vector_status == "indexed"
    assert record.chunk_count > 0


@override_settings(
    KB_UPLOAD_PROCESS_MODE="queue",
    CELERY_TASK_ALWAYS_EAGER=True,
    KB_TEST_ADAPTER="offline",
)
def test_duplicate_dispatch_processes_once(uploaded_file, library_setup_vector):
    """重复派发同一份资料：只处理一次（第二次因为已 DONE 而跳过）。"""
    from knowledgebase.celery import process_ingested_file

    record = uploaded_file
    bound = {
        "content_sha256": record.content_sha256,
        "library_code": record.library.code,
    }
    first = process_ingested_file.apply(args=[record.pk], kwargs=bound).get()
    record.refresh_from_db()
    first_state = (record.parse_status, record.chunk_count, record.parse_attempts)
    assert first["ok"] is True

    # 再派一次（模拟队列重复投递 / 用户重复点重试）
    second = process_ingested_file.apply(args=[record.pk], kwargs=bound).get()
    record.refresh_from_db()

    assert second["ok"] is True
    assert second["metrics"].get("skipped") is True  # 走了"已处理，跳过"分支
    assert (record.parse_status, record.chunk_count, record.parse_attempts) == first_state


@override_settings(
    KB_UPLOAD_PROCESS_MODE="queue",
    CELERY_TASK_ALWAYS_EAGER=True,
    KB_TEST_ADAPTER="offline",
)
def test_concurrent_workers_only_one_wins_lease(uploaded_file, library_setup_vector):
    """同一份资料被两个 worker 同时抢：只有拿到租约的那个能处理。"""
    record = uploaded_file
    token = pipeline.acquire_lease(record)
    try:
        with pytest.raises(pipeline.LeaseNotAcquired):
            pipeline.acquire_lease(record)
    finally:
        pipeline.release_lease(record, token)

    # 释放之后可以被别人接管
    token2 = pipeline.acquire_lease(record)
    pipeline.release_lease(record, token2)


@override_settings(KB_UPLOAD_PROCESS_MODE="queue", KB_TEST_ADAPTER="offline")
def test_expired_lease_can_be_taken_over(uploaded_file, library_setup_vector):
    """租约过期（worker 崩溃场景）后允许接管，不会永久卡死。"""
    import datetime

    from django.utils import timezone

    record = uploaded_file
    token = pipeline.acquire_lease(record)
    # 人为把租约改成已过期
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_lease_expires_at=timezone.now() - datetime.timedelta(seconds=1)
    )
    try:
        new_token = pipeline.acquire_lease(record)  # 应当抢得到
        assert new_token != token
    finally:
        pipeline.release_lease(record, token)


@override_settings(KB_UPLOAD_PROCESS_MODE="queue", KB_TEST_ADAPTER="offline")
def test_stale_owner_cannot_overwrite_new_owner(uploaded_file, library_setup_vector):
    """旧 owner 的租约过期被接管后，它再写状态必须被拒绝（CAS 命中 0 行）。"""
    import datetime

    from django.utils import timezone

    record = uploaded_file
    stale = pipeline.acquire_lease(record)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_lease_expires_at=timezone.now() - datetime.timedelta(seconds=1)
    )
    fresh = pipeline.acquire_lease(record)
    try:
        with pytest.raises(pipeline.LeaseLost):
            pipeline._guarded_status(record, stale, ParseStatus.RUNNING)
        # 新 owner 自己写是成功的
        pipeline._guarded_status(record, fresh, ParseStatus.RUNNING)
    finally:
        pipeline.release_lease(record, fresh)


@override_settings(KB_UPLOAD_PROCESS_MODE="queue", KB_TEST_ADAPTER="offline")
def test_renew_lease_does_not_revive_expired_token(uploaded_file, library_setup_vector):
    """续租不能复活已过期的 token（否则被接管的租约会被抢回来）。"""
    import datetime

    from django.utils import timezone

    record = uploaded_file
    token = pipeline.acquire_lease(record)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_lease_expires_at=timezone.now() - datetime.timedelta(seconds=1)
    )
    assert pipeline.renew_lease(record, token) is False
    pipeline.release_lease(record, token)


# ------------------------------------------------------------ 真实 worker ----
#
# 真实 broker + 真实 worker 消费的往返用例在 ``tests/test_real_worker.py``。
# 这里刻意**不再**保留"delay 一次再看看 meta"的写法：那种用例即使没有 worker
# 也会通过（允许 PENDING），是假阳性（审查意见 22:50）。

