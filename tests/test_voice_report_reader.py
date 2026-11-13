from unittest.mock import patch
import hashlib
import pytest

from apps.planner.models import DerivedDocument, Message
from apps.voice import services
from apps.voice.documents import attach_result, report_text, KEY
from apps.voice.models import VoiceTurn
from apps.voice.rendering import frame
from tests.test_voice_devices import added, upload

pytestmark = pytest.mark.django_db


def setup_report(user, text=None):
    d, api = added(user)
    response = upload(api, d)
    turn = VoiceTurn.objects.get(pk=response.data['task_id'])
    conversation = turn.conversation
    assert conversation is not None
    turn.user_message = Message.objects.create(conversation=conversation, role='user', content='生成统计报告')
    turn.state = 'answer_ready'
    turn.answer = '报告已生成，请到网页查看'
    turn.save()
    doc = DerivedDocument.objects.create(created_by=user, conversation=conversation,
        title='完整统计报告', content_markdown=text or '\n'.join(f'第{i}项：真实报告内容' for i in range(100)))
    result = {'intent': 'compose_document', 'status': 'executed', 'result': {'document_id': doc.pk}}
    return d, api, turn, doc, result


def test_complete_report_is_readable_and_first_page_is_fast(user):
    d, api, turn, doc, result = setup_report(user)
    attach_result(turn, result)
    services.render_turn(turn)
    assert turn.answer_pages > 8
    assert '第99项' in turn.answer and '请到网页查看' not in turn.answer
    url = f'/api/v1/voice/device/turns/{turn.client_turn_id}/pages/answer/batch/'
    first = api.get(url+'0/')
    assert first.status_code == 200 and first['X-Frame-Count'] == '1'
    assert first.content == frame(turn.answer, '报告正文', 0)[0]
    second = api.get(url+'1/')
    assert second['X-Frame-Count'] == '8'
    last = api.get(url+str(turn.answer_pages-1)+'/')
    assert last.content == frame(turn.answer, '报告正文', turn.answer_pages-1)[0]
    assert last['X-Content-SHA256'] == hashlib.sha256(last.content).hexdigest()
    assert api.get(url+str(turn.answer_pages)+'/').status_code == 404


def test_tables_preserve_every_header_and_value_for_small_screen(user):
    text = '# 统计\n\n| 年份 | 数量 |\n|---|---:|\n|2025|30|\n|2026|17|\n\n报告末尾'
    _, _, turn, _, result = setup_report(user, text)
    attach_result(turn, result)
    body = report_text(turn)
    assert body is not None
    for text in ['年份：2025','数量：30','年份：2026','数量：17','报告末尾']:
        assert text in body
    assert '|---' not in body


def test_edited_report_invalidates_revision_and_deleted_report_hides_cached_text(user):
    _, api, turn, doc, result = setup_report(user)
    attach_result(turn, result)
    services.render_turn(turn)
    before = api.get('/api/v1/voice/device/config/').data['latest_answer_revision']
    doc.content_markdown = '这是编辑后的最新报告正文'
    doc.save()
    after = api.get('/api/v1/voice/device/config/').data['latest_answer_revision']
    assert before != after
    assert '编辑后的最新' in services.screen_answer(turn)
    doc.delete()
    assert '报告已删除' in services.screen_answer(turn)
    assert '第99项' not in services.screen_answer(turn)


def test_doc_binding_is_scoped_to_exact_owner_conversation_and_turn(user, other_user):
    _, _, turn, doc, result = setup_report(user)
    assert report_text(turn) is None  # Other existing documents are never guessed.
    doc.created_by = other_user
    doc.save()
    attach_result(turn, result)
    assert report_text(turn) is None
    assert turn.user_message is not None
    turn.user_message.payload[KEY] = [doc.pk]
    turn.user_message.save()
    assert '报告已删除' in (report_text(turn) or "")
    doc.created_by = user
    doc.conversation = None
    doc.save()
    assert '报告已删除' in (report_text(turn) or "")


def test_final_summary_failure_does_not_replay_saved_report_or_hide_body(user):
    _, _, turn, doc, result = setup_report(user)
    def failed_reply(*args, **kwargs):
        attach_result(kwargs['voice_turn'], result)
        raise RuntimeError('final summary timed out')
    with patch('apps.planner.services.reply_to_message', side_effect=failed_reply) as reply:
        services.answer_turn(turn)
    reply.assert_called_once()
    turn.refresh_from_db()
    assert turn.state == 'answer_ready' and '第99项' in turn.answer
    assert DerivedDocument.objects.count() == 1
    assert not Message.objects.filter(role='assistant').exists()
    assert Message.objects.filter(role='system', payload__document_saved_reply_unavailable=True).exists()  # pyright: ignore[reportCallIssue]
