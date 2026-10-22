"""跨 web/worker 的只读进度快照；资料和操作的数据库状态始终是终态依据。"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import logging
import time
import uuid

from django.conf import settings

logger = logging.getLogger(__name__)
_file = ContextVar('file_progress', default=None)
_operation = ContextVar('operation_progress', default=None)
_connection = None
_connection_url = None
_retry_after = 0.0
_WRITE = """
local old = redis.call('GET', KEYS[1])
if old then
  local value = cjson.decode(old)
  if ARGV[4] == 'start' then
    if tonumber(value.started_at) > tonumber(ARGV[3]) then return 0 end
  elseif value.run ~= ARGV[2] then return 0 end
elseif ARGV[4] ~= 'start' then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[5])
return 1
"""


def _client():
    global _connection, _connection_url
    if not getattr(settings, 'KB_PROGRESS_ENABLED', True) or time.monotonic() < _retry_after:
        return None
    url = getattr(settings, 'KB_PROGRESS_REDIS_URL', '') or getattr(settings, 'CELERY_BROKER_URL', '')
    if not str(url).startswith(('redis://', 'rediss://')):
        return None
    if _connection is None or _connection_url != url:
        import redis
        _connection = redis.Redis.from_url(url, socket_connect_timeout=.2, socket_timeout=.2)
        _connection_url = url
    return _connection


def _unavailable():
    global _retry_after
    _retry_after = time.monotonic() + 15
    logger.warning('进度缓存暂不可用，继续处理并显示数据库状态')


def _key(kind, pk):
    return f"{getattr(settings, 'KB_PROGRESS_PREFIX', 'kb:progress')}:{kind}:{pk}"


def _write(kind, context, *, start=False):
    try:
        client = _client()
        if client is not None:
            client.eval(_WRITE, 1, _key(kind, context['id']), json.dumps(context, ensure_ascii=False),
                        context['run'], context['started_at'], 'start' if start else 'update', 86400)
    except Exception:  # 进度显示故障不能中断实际执行。
        _unavailable()


def read_many(kind, ids):
    try:
        client = _client()
        if client is not None and ids:
            values = client.mget([_key(kind, pk) for pk in ids])
            return {pk: json.loads(value) for pk, value in zip(ids, values) if value}
    except Exception:
        _unavailable()
    return {}


def _update(kind, context, stage, label, current=None, total=None, unit='', detail='', **extra):
    start = not context.get('published')
    context.update(stage=stage, label=label, current=current, total=total, unit=unit,
                   detail=detail, updated_at=time.time(), state='running', published=True, **extra)
    _write(kind, context, start=start)


def report(stage, label, current=None, total=None, unit='', detail=''):
    """只报告已完成的工作量；无总量时保持不定进度。"""
    file_context, operation_context = _file.get(), _operation.get()
    if file_context is not None:
        _update('file', file_context, stage, label, current, total, unit, detail)
    if operation_context is not None:
        _update('operation', operation_context, stage, label, current, total, unit, detail,
                file_id=file_context['id'] if file_context else None)


def _finish(kind, context, failed=False):
    if not context.get('published'):
        return
    context.update(state='failed' if failed else 'done', updated_at=time.time())
    if not failed:
        context.update(stage='complete', label='已完成', current=1, total=1, unit='', detail='')
    _write(kind, context)


@contextmanager
def track_file(record, lease_token):
    context = {'id': record.pk, 'run': lease_token,
               'started_at': record.parse_lease_started_at.timestamp() if record.parse_lease_started_at else time.time()}
    token = _file.set(context)
    try:
        report('preparing', '正在校验原件')
        yield
    except BaseException:
        _finish('file', context, failed=True)
        raise
    else:
        _finish('file', context)
    finally:
        _file.reset(token)


def track_operation(function):
    @wraps(function)
    def wrapped(user, operation, *args, **kwargs):
        context = {'id': operation.pk, 'run': uuid.uuid4().hex, 'started_at': time.time()}
        token = _operation.set(context)
        try:
            result = function(user, operation, *args, **kwargs)
        except BaseException:
            _finish('operation', context, failed=True)
            raise
        else:
            _finish('operation', context)
            return result
        finally:
            _operation.reset(token)
    return wrapped


def _public(value, state, fallback):
    if state == 'failed' and value.get('state') == 'done':
        value = {}
    current, total = value.get('current'), value.get('total')
    percent = None
    if isinstance(current, (int, float)) and isinstance(total, (int, float)) and total > 0:
        current = max(0, min(current, total))
        percent = min(99, round(current / total * 100)) if current < total else 100
    if state == 'done':
        return {'state': state, 'stage': 'complete', 'label': '已完成', 'percent': 100,
                'current': None, 'total': None, 'unit': '', 'detail': ''}
    label = value.get('label') or fallback
    if state == 'failed' and value.get('label'):
        label = '处理失败 · ' + label
    return {'state': state, 'stage': value.get('stage', ''), 'label': label,
            'percent': percent, 'current': current, 'total': total,
            'unit': value.get('unit', ''), 'detail': value.get('detail', ''),
            'started_at': value.get('started_at'), 'updated_at': value.get('updated_at')}


def file_snapshot(record, cached=None):
    value = (read_many('file', [record.pk]).get(record.pk, {}) if record.parse_status in {'running', 'failed'} else {}) if cached is None else cached
    if record.parse_status == 'running' and value.get('run') != record.parse_lease_token:
        value = {}
    states = {'pending': ('queued', '等待解析'), 'running': ('running', '正在处理'),
              'done': ('done', '已完成'), 'failed': ('failed', '处理失败'),
              'archived': ('cancelled', '历史归档资料')}
    state, fallback = states.get(record.parse_status, ('queued', '等待处理'))
    if state in {'queued', 'cancelled'}:
        value = {}
    return _public(value, state, fallback)


def operation_snapshot(operation, cached=None):
    value = (read_many('operation', [operation.pk]).get(operation.pk, {}) if operation.status in {'executing', 'failed'} else {}) if cached is None else cached
    state = {'executing': 'running', 'executed': 'done', 'failed': 'failed',
             'rejected': 'cancelled', 'expired': 'cancelled'}.get(operation.status, 'queued')
    if state in {'queued', 'cancelled'}:
        value = {}
    return _public(value, state, {'running': '正在执行', 'queued': '等待确认或执行',
                                'failed': '执行失败', 'cancelled': '操作已结束', 'done': '已完成'}[state])


def request_ids(raw):
    from rest_framework.exceptions import ValidationError
    parts = str(raw or '').split(',')
    if not 1 <= len(parts) <= 100 or any(not part.isascii() or not part.isdigit() or
                                      len(part) > 18 or int(part) <= 0 for part in parts):
        raise ValidationError({'ids': '请提供 1–100 个正整数编号，以逗号分隔'})
    return list(dict.fromkeys(int(part) for part in parts))
