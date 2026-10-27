from unittest.mock import patch

import pytest

from apps.knowledge.models import KnowledgeLibrary
from apps.planner import services
from apps.planner.llm import LlmReply, ToolCall
from apps.planner.models import ActionTicket, Conversation, OperationRecord

pytestmark = pytest.mark.django_db


def test_create_library_executes_with_zero_confirmations_and_cannot_replay(admin_user):
    result = services.propose_operation(admin_user, intent="create_library", params={"code": "direct-lib", "name": "直接创建"})
    row = OperationRecord.objects.get(pk=result['operation_id'])
    assert row.status == 'executed' and row.required_confirmations == row.confirmations == 0
    assert not ActionTicket.objects.filter(operation=row).exists()
    assert KnowledgeLibrary.objects.filter(code='direct-lib').count() == 1
    with pytest.raises(services.PlannerError):
        services.execute_operation(admin_user, row)
    assert KnowledgeLibrary.objects.filter(code='direct-lib').count() == 1


def test_new_policy_keeps_permissions_and_delete_confirmation(user, admin_user, library, make_file):
    with pytest.raises(services.PlannerError):
        services.propose_operation(user, intent='create_library', params={'code': 'denied', 'name': '越权'})
    record = make_file(library, '不能自动删除.md')
    result = services.propose_operation(admin_user, intent='delete_file', params={'file_id': record.pk})
    assert result['status'] == 'proposed' and result['required_confirmations'] == 3
    assert result['confirm_token'] and not result.get('auto_executed')
    assert type(record)._base_manager.filter(pk=record.pk).exists()
    with pytest.raises(services.PlannerError):
        services.execute_operation(admin_user, OperationRecord.objects.get(pk=result['operation_id']))


def test_missing_sources_asks_for_parameters_without_leaving_confirmation_card(user):
    with pytest.raises(services.PlannerError, match='参考文件'):
        services.propose_operation(user, intent='compose_document', params={'title': '报告', 'topic': '主题'})
    assert not OperationRecord.objects.exists()
    assert not ActionTicket.objects.exists()


def test_chat_repeated_write_tool_calls_apply_effect_once(admin_user):
    params = {'code': 'one-effect', 'name': '只创建一次'}
    replies = [LlmReply(tool_calls=[ToolCall('a', 'create_library', params), ToolCall('b', 'create_library', params)]),
               LlmReply(tool_calls=[ToolCall('c', 'create_library', params)]), LlmReply(content='已创建。')]
    conversation = Conversation.objects.create(user=admin_user)
    with patch.object(services, 'build_context_report', return_value={'context': '', 'degraded': False, 'degraded_reason': '', 'searched_libraries': [], 'failed_collections': []}), \
         patch.object(services.LlmClient, 'chat', side_effect=replies):
        result = services.reply_to_message(admin_user, conversation, '创建这个信息库')
    assert KnowledgeLibrary.objects.filter(code='one-effect').count() == 1
    assert OperationRecord.objects.filter(intent='create_library').count() == 1
    assert len(result['proposals']) == 1


def test_conversation_shows_only_pending_deletions(api, admin_user):
    conversation = Conversation.objects.create(user=admin_user)
    OperationRecord.objects.create(user=admin_user, conversation=conversation, intent='create_library', status='proposed')
    deletion = OperationRecord.objects.create(user=admin_user, conversation=conversation, intent='delete_conversation', status='proposed')
    api.force_authenticate(admin_user)
    response = api.get(f'/api/v1/planner/conversations/{conversation.pk}/')
    assert response.status_code == 200
    assert [row['id'] for row in response.json()['operations']] == [deletion.pk]
