"""后台补处理和巡检必须推进后续资料，并持久记录本批检查范围。"""

import io
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.knowledge import integrity
from apps.knowledge.models import IngestedFile, LibraryWriteGate, PatrolRun
from apps.knowledge.writegate import WriteFrozen
from knowledgebase.celery import patrol

pytestmark = pytest.mark.django_db


def _finish(record):
    IngestedFile._base_manager.filter(pk=record.pk).update(parse_status="done")
    return SimpleNamespace(ok=True, detail="已完成")


def test_sweep_skips_exhausted_records_before_taking_batch(make_library, make_file, monkeypatch):
    lib = make_library()
    exhausted = [make_file(lib, status="failed", parse_attempts=5) for _ in range(20)]
    pending = make_file(lib, status="pending")
    calls = []

    def process(record):
        calls.append(record.pk)
        return _finish(record)

    monkeypatch.setattr("apps.knowledge.management.commands.kbprocess.process_file", process)
    for _ in range(2):
        call_command("kbprocess", limit=20, retry_failed=True, stdout=io.StringIO())
    assert calls == [pending.pk]
    assert all(IngestedFile._base_manager.get(pk=row.pk).parse_attempts == 5 for row in exhausted)


def test_sweep_frozen_library_does_not_fill_batch(make_library, make_file, monkeypatch):
    frozen_lib = make_library(code="frozen")
    LibraryWriteGate.objects.create(library=frozen_lib, frozen_at=timezone.now())
    frozen = make_file(frozen_lib, status="pending")
    pending = make_file(make_library(code="writable"), status="pending")
    calls = []

    def process(record):
        calls.append(record.pk)
        return _finish(record)

    monkeypatch.setattr("apps.knowledge.management.commands.kbprocess.process_file", process)
    call_command("kbprocess", limit=1, stdout=io.StringIO())
    assert calls == [pending.pk]
    frozen.refresh_from_db()
    assert frozen.parse_status == "pending"


def test_sweep_continues_other_libraries_if_freeze_happens_after_query(make_library, make_file, monkeypatch):
    frozen_lib = make_library(code="racing-freeze")
    first = make_file(frozen_lib, status="pending")
    make_file(frozen_lib, status="pending")
    pending = make_file(make_library(code="writable"), status="pending")
    calls = []

    def process(record):
        calls.append(record.pk)
        if record.library_id == frozen_lib.pk:
            raise WriteFrozen("重建中")
        return _finish(record)

    monkeypatch.setattr("apps.knowledge.management.commands.kbprocess.process_file", process)
    call_command("kbprocess", stdout=io.StringIO())
    assert calls == [first.pk, pending.pk]


@pytest.mark.parametrize("status", ["pending", "running"])
def test_patrol_saves_history_with_unfinished_files(make_library, make_file, status):
    make_file(make_library(), status=status)
    result = patrol.run(source="beat")
    run = PatrolRun.objects.get()
    assert result["ok"]
    assert run.checked == 1
    assert isinstance(run.duration_ms, int)
    assert run.coverage == result["coverage"]
    assert integrity.patrol_snapshot()["coverage"] == run.coverage
    assert integrity.verification_snapshot()["coverage"] == run.coverage


def test_patrol_rotates_across_entire_snapshot_despite_new_uploads(make_library, make_file):
    lib = make_library()
    old = make_file(lib, chunk_count=2, embedded_chunk_count=0)
    for _ in range(200):
        make_file(lib, chunk_count=2, embedded_chunk_count=2)
    first = patrol.run(limit=200, source="beat")
    assert first["checked"] == 200
    assert not first["coverage"]["cycle_complete"]
    assert first["findings"]["vector_incomplete"][0]["file_id"] == old.pk
    added = make_file(lib)
    # 从数据库保存的游标续做，不依赖调用方带回游标；新增资料留待下一轮。
    second = patrol.run(limit=200, source="beat")
    assert second["checked"] == 1
    assert second["coverage"]["after_id"] == first["coverage"]["last_id"]
    assert second["coverage"]["last_id"] < added.pk
    assert second["coverage"]["cycle_complete"]
    third = patrol.run(limit=500, source="beat")
    assert third["coverage"]["after_id"] == 0
    assert third["coverage"]["last_id"] == added.pk
    assert third["checked"] == 202


def test_manual_patrol_does_not_reset_periodic_cursor(make_library, make_file):
    lib = make_library()
    rows = [make_file(lib) for _ in range(3)]
    patrol.run(limit=1, source="beat")
    patrol.run(limit=3, source="manual")
    second = patrol.run(limit=1, source="beat")
    assert second["coverage"]["first_id"] == rows[1].pk


def test_failed_history_write_does_not_advance_cursor(make_library, make_file, monkeypatch):
    lib = make_library()
    rows = [make_file(lib) for _ in range(2)]
    with monkeypatch.context() as patch:
        patch.setattr(integrity, "record_patrol_run", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db unavailable")))
        first = patrol.run(limit=1, source="beat")
    second = patrol.run(limit=1, source="beat")
    assert first["coverage"]["first_id"] == second["coverage"]["first_id"] == rows[0].pk
    assert PatrolRun.objects.count() == 1
