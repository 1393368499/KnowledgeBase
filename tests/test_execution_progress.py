"""真实执行边界、页/段计数、授权和失败降级；不调用线上模型或业务 Redis。"""
import io
import json
from types import SimpleNamespace

import pytest
from django.utils import timezone
from pypdf import PdfWriter

from apps.knowledge import parsers, progress
from apps.knowledge.models import MemberRole, ParseStatus
from apps.knowledge.ocr import OcrError
from apps.planner import services
from apps.planner.models import OperationRecord


@pytest.fixture
def cache(monkeypatch):
    class Cache:
        def __init__(self):
            self.data = {}

        def eval(self, script, count, key, payload, run, started, mode, ttl):
            assert count == 1 and ttl == 86400
            old = json.loads(self.data[key]) if key in self.data else None
            if old and ((mode == 'start' and old['started_at'] > started) or
                        (mode != 'start' and old['run'] != run)):
                return 0
            if not old and mode != 'start':
                return 0
            self.data[key] = payload
            return 1

        def mget(self, keys):
            return [self.data.get(key) for key in keys]

    instance = Cache()
    monkeypatch.setattr(progress, '_client', lambda: instance)
    return instance


def record(pk=17, status='running', token='current'):
    return SimpleNamespace(pk=pk, parse_status=status, parse_lease_token=token,
                           parse_lease_started_at=timezone.now())


def test_file_counts_survive_refresh_without_exposing_internal_tokens(cache):
    file = record()
    with progress.track_file(file, 'current'):
        progress.report('ocr', '正在识别扫描页', 2, 5, '页', '原件第 3 页')
        snap = progress.file_snapshot(file)
        assert (snap['state'], snap['percent'], snap['current'], snap['total']) == ('running', 40, 2, 5)
        assert snap['detail'] == '原件第 3 页'
        assert 'run' not in snap and 'id' not in snap and 'published' not in snap
    file.parse_status = 'done'
    assert progress.file_snapshot(file)['percent'] == 100
    assert progress._file.get() is None


def test_failure_keeps_last_completed_work_and_does_not_claim_success(cache):
    file = record()
    with pytest.raises(RuntimeError):
        with progress.track_file(file, 'current'):
            progress.report('ocr', '正在识别扫描页', 2, 5, '页')
            raise RuntimeError('internal-secret-error')
    file.parse_status = 'failed'
    snapshot = progress.file_snapshot(file)
    assert snapshot['state'] == 'failed' and snapshot['percent'] == 40
    assert 'internal-secret-error' not in json.dumps(snapshot)


def test_old_lease_cannot_publish_over_new_run(cache):
    file = record()
    with progress.track_file(file, 'current'):
        old = progress._file.get().copy()
        newer = dict(old, run='next', started_at=old['started_at']+1, label='new')
        progress._write('file', newer, start=True)
        progress._write('file', dict(old, label='stale'))
        progress._write('file', dict(old, label='stale-start'), start=True)
        assert progress.read_many('file', [file.pk])[file.pk]['label'] == 'new'
        # DB lease 与缓存不匹配时不显示旧一轮进度。
        assert progress.file_snapshot(file)['percent'] is None


@pytest.mark.parametrize('state', ['pending', 'archived'])
def test_non_running_file_ignores_stale_success(state):
    snapshot = progress.file_snapshot(record(status=state), {'current': 1, 'total': 1, 'label': 'stale'})
    assert snapshot['percent'] is None and snapshot['label'] != 'stale'


def test_percentage_cannot_round_unfinished_work_up_to_100():
    snapshot = progress.file_snapshot(record(), {'run': 'current', 'current': 199, 'total': 200})
    assert snapshot['percent'] == 99


def test_progress_cache_outage_never_interrupts_actual_work(monkeypatch):
    class Broken:
        def eval(self, *args):
            raise ConnectionError('unavailable')
        def mget(self, *args):
            raise ConnectionError('unavailable')
    monkeypatch.setattr(progress, '_client', lambda: Broken())
    monkeypatch.setattr(progress, '_retry_after', 0)
    with progress.track_file(record(), 'current'):
        progress.report('ocr', '正在识别扫描页', 1, 3)
    assert progress.read_many('file', [17]) == {}


def test_nested_file_progress_is_visible_to_parent_operation(cache):
    operation = SimpleNamespace(pk=11, status='executing')
    @progress.track_operation
    def execute(user, operation):
        with progress.track_file(record(), 'current'):
            progress.report('embedding', '正在建立向量索引', 8, 20, '段')
            snap = progress.operation_snapshot(operation)
            assert snap['percent'] == 40 and snap['unit'] == '段'
        return 'ok'
    assert execute(None, operation) == 'ok'
    assert progress._operation.get() is None


@pytest.mark.parametrize('value', ['', '0', '-1', 'a', '²', '１２', '1,,2', ','.join(['1']*101), '9'*30])
def test_progress_ids_reject_invalid_or_unbounded_input(value):
    from rest_framework.exceptions import ValidationError
    with pytest.raises(ValidationError):
        progress.request_ids(value)


def test_pdf_reports_only_completed_pages_and_stops_before_failed_page(cache, monkeypatch):
    pdf = PdfWriter()
    for _ in range(3):
        pdf.add_blank_page(width=100, height=100)
    source = io.BytesIO()
    pdf.write(source)
    monkeypatch.setattr(parsers, 'render_pdf_pages', lambda raw, pages: [(p, str(p).encode()) for p in pages])
    file = record()
    seen = []
    def recognize(data, **kwargs):
        seen.append(progress.file_snapshot(file))
        if data == b'3':
            raise OcrError('识别失败')
        return '完整正文'
    with pytest.raises(parsers.ParseError):
        with progress.track_file(file, 'current'):
            parsers.parse_bytes(source.getvalue(), '三页.pdf', ocr=SimpleNamespace(available=True, recognize_image=recognize))
    assert [s['current'] for s in seen] == [0, 1, 2]
    assert all(s['total'] == 3 and s['stage'] == 'ocr' for s in seen)
    file.parse_status = 'failed'
    assert progress.file_snapshot(file)['percent'] == 67


@pytest.mark.django_db
def test_progress_api_authorizes_before_cache_lookup(auth_api, user, other_user, make_library, make_file, grant_member, monkeypatch):
    mine = make_library('mine')
    theirs = make_library('theirs')
    grant_member(mine, user, role=MemberRole.READER)
    grant_member(theirs, other_user, role=MemberRole.READER)
    visible = make_file(mine, status=ParseStatus.RUNNING, parse_lease_token='current')
    secret = make_file(theirs, status=ParseStatus.RUNNING, parse_error='secret')
    own = OperationRecord.objects.create(user=user, intent='list_files', status='executing')
    private = OperationRecord.objects.create(user=other_user, intent='list_files', status='executing', error='secret')
    lookups = []
    monkeypatch.setattr(progress, 'read_many', lambda kind, ids: lookups.append((kind, ids)) or {})
    client = auth_api(user)
    files = client.get(f'/api/v1/knowledge/files/progress/?ids={visible.pk},{secret.pk}')
    assert files.status_code == 200, files.data
    assert [r['id'] for r in files.data['results']] == [visible.pk]
    operations = client.get(f'/api/v1/planner/operations/progress/?ids={own.pk},{private.pk}')
    assert operations.status_code == 200
    assert [r['id'] for r in operations.data['results']] == [own.pk]
    assert lookups == [('file', [visible.pk]), ('operation', [own.pk])]
    assert 'secret' not in json.dumps([files.data, operations.data])
    assert client.get('/api/v1/knowledge/files/progress/?ids=²').status_code == 400
    assert client.get('/api/v1/planner/operations/progress/?ids=0').status_code == 400


@pytest.mark.django_db
def test_progress_endpoints_require_login(api):
    for url in ('knowledge/files', 'planner/operations'):
        assert api.get(f'/api/v1/{url}/progress/?ids=1').status_code in (401, 403)


@pytest.mark.django_db
@pytest.mark.parametrize('fail', [False, True])
def test_real_execution_reports_after_claim_and_terminal_database_state(cache, user, monkeypatch, fail):
    """提案即执行：进度只在真正的执行窗口里可见，终态与重复执行都如实落库。

    现行策略下 ``propose_operation`` 内部就调用 ``execute_operation``，
    所以 ``_dispatch`` 必须在提案**之前**替换，才能观察到 EXECUTING 中间态。
    """
    def dispatch(actor, op, spec):
        op.refresh_from_db()
        assert op.status == 'executing'
        progress.report('reading', '正在读取资料', 1, 3, '份')
        assert progress.operation_snapshot(op)['percent'] == 33
        if fail:
            raise services.PlannerError('test execution failed')
        return {'count': 3}
    monkeypatch.setattr(services, '_dispatch', dispatch)
    if fail:
        with pytest.raises(services.PlannerError):
            services.propose_operation(user, intent='list_files', params={})
    else:
        proposal = services.propose_operation(user, intent='list_files', params={})
        assert proposal['auto_executed'] is True
        assert proposal['result']['count'] == 3
    operation = OperationRecord.objects.get(intent='list_files')
    operation.refresh_from_db()
    assert progress.operation_snapshot(operation)['state'] == ('failed' if fail else 'done')
    before = cache.data.copy()
    with pytest.raises(services.PlannerError):
        services.execute_operation(user, operation)
    assert cache.data == before, '重复执行不得覆盖已经完成/失败的进度'
