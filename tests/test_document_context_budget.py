import json
import re
from types import SimpleNamespace

import pytest

from apps.planner import document_context as dc, services
from apps.planner.llm import LLMError, LlmClient, LlmProfile, input_budget, message_size
from apps.planner.models import DerivedDocument
from apps.knowledge.models import DocumentChunk


def test_client_rejects_oversized_input_before_transport(settings):
    settings.KB_LLM_CONTEXT_TOKENS = 20000
    settings.KB_LLM_CONTEXT_RESERVE_TOKENS = 1024
    client = LlmClient(LlmProfile(base_url='http://test/v1', model='test', max_tokens=2048))
    class Never:
        def post(self, *args, **kwargs):
            pytest.fail('超长请求不得发送给模型')
    client._client = Never()
    with pytest.raises(LLMError, match='安全上下文预算'):
        client.chat([{'role':'user','content':'长资料'*20000}])


def test_batch_split_preserves_all_unicode_and_escaped_characters():
    text = ('中文🙂\x00"\\\n' * 4000) + '最后一段不能丢'
    rows = [{'file_id':1,'original_name':'原件.pdf','text':text}]
    batches = list(dc._batches(rows,4096))
    assert ''.join(row['text'] for batch in batches for row in batch) == text
    assert all(dc._size(dc.render_sources(batch)) <= 4096 for batch in batches)


@pytest.mark.parametrize('items', [[], [{'file_id':2,'facts':'wrong'}],
    [{'file_id':1,'facts':'a'},{'file_id':1,'facts':'duplicate'}], [{'file_id':1,'facts':''}]])
def test_extraction_cannot_silently_omit_or_mix_sources(items):
    client=SimpleNamespace(chat=lambda messages:SimpleNamespace(content=json.dumps({'sources':items})))
    with pytest.raises(LLMError):
        dc._extract(client,[{'file_id':1,'original_name':'参考.pdf','text':'原始资料'}],'报告')


def test_truncated_evidence_is_rejected():
    client=SimpleNamespace(chat=lambda messages:SimpleNamespace(content='{}',finish_reason='length'))
    with pytest.raises(LLMError,match='截断'):
        dc._extract(client,[{'file_id':1,'text':'资料'}],'报告')


@pytest.mark.django_db
def test_long_selected_files_cover_all_chunks_and_sources(settings,user,library,make_file,grant_member):
    settings.KB_LLM_DOC_SOURCE_BYTES = 4096
    grant_member(library,user)
    files=[make_file(library,f'合同{i}.pdf') for i in range(3)]
    originals=[]
    for file in files:
        for ordinal in range(55):
            text=f'唯一标记-F{file.pk}-C{ordinal}-尾部。' + '合同总价100万元。'*10
            DocumentChunk.objects.create(file=file,ordinal=ordinal,text=text,token_estimate=200,page=ordinal+1)
            originals.append(text)
    class Model:
        def __init__(self):
            self.extractions=[]
            self.finals=[]
        def chat(self,messages):
            assert message_size(messages) <= input_budget(services.document_profile())
            if messages[0]['content']==dc.EXTRACT_PROMPT:
                source=messages[1]['content']
                self.extractions.append(source)
                ids=list(dict.fromkeys(int(x) for x in re.findall(r'\[来源 file_id=(\d+)',source)))
                return SimpleNamespace(content=json.dumps({'sources':[{'file_id':pk,'facts':f'合同{pk}：合同总价100万元，见原文。'} for pk in ids]}))
            self.finals.append(messages)
            return SimpleNamespace(content='# 合同统计报告\n\n三份合同分别列示。',model='test')
    model=Model()
    result=services.compose_document(user,title='合同统计报告',topic='合同金额',source_file_ids=[f.pk for f in files],client=model)
    assert result['source_preparation']['summarized']
    assert result['source_preparation']['chunks']==165
    assert result['source_preparation']['files']==3
    assert all(text in '\n'.join(model.extractions) for text in originals)
    assert len(model.finals)==1
    document=DerivedDocument.objects.get(pk=result['document_id'])
    assert set(document.sources.values_list('pk',flat=True))=={f.pk for f in files}
    assert all(f'file_id={f.pk}' in model.finals[0][0]['content'] for f in files)


@pytest.mark.django_db
def test_missing_chunks_in_one_selected_file_cannot_be_ignored(user,library,make_file,grant_member):
    grant_member(library,user)
    good=make_file(library,'有正文.pdf')
    empty=make_file(library,'缺分段.pdf')
    DocumentChunk.objects.create(file=good,ordinal=0,text='可用正文',token_estimate=10)
    with pytest.raises(services.PlannerError,match='缺分段'):
        services.compose_document(user,title='报告',topic='统计',source_file_ids=[good.pk,empty.pk])
    assert not DerivedDocument.objects.exists()
