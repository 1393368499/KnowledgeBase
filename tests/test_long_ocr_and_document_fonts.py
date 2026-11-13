import io
import json
from types import SimpleNamespace

import httpx
import pytest
from docx import Document
from pypdf import PdfWriter

from apps.knowledge import embedding, parsers
from apps.knowledge.ocr import OcrClient, OcrProfile, OcrError, default_profile
from apps.planner.document_export import build_export
from apps.planner.models import DerivedDocument


def test_paddle_ocr_uses_official_task_prompt_and_rejects_truncation():
    def handler(request):
        payload = json.loads(request.content)
        content = payload["messages"][0]["content"]
        assert content[0]["type"] == "image_url"
        assert content[1] == {"type": "text", "text": "OCR:"}
        assert payload["repetition_penalty"] == 1.1
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "不完整内容"}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ocr = OcrClient(OcrProfile(base_url="http://ocr-test/v1", model="PaddleOCR-VL-1.5"), client=client)
        with pytest.raises(OcrError, match="不完整页"):
            ocr.recognize_image(b"image", hint="不应注入任务提示的文件名")


def test_ocr_output_budget_is_configurable(settings):
    settings.KB_OCR_MAX_TOKENS = 16384
    assert default_profile().max_tokens == 16384
    assert OcrProfile(base_url="http://ocr-test/v1", model="PaddleOCR-VL-1.5").max_tokens == 4096


def test_ocr_shared_concurrency_limit_is_separate_from_other_models(settings):
    from apps.knowledge.ratelimit import build_limiter
    settings.KB_MODEL_MAX_CONCURRENCY = 4
    settings.KB_MODEL_LIMIT_ENABLED = True
    settings.KB_OCR_MAX_CONCURRENCY = 1
    limiter = build_limiter()
    seen = []
    class Client:
        def eval(self, *args):
            seen.append(args)
            return 1
    limiter._client = Client()
    limiter.try_acquire("ocr:http://ocr-test/v1|PaddleOCR-VL-1.5")
    limiter.try_acquire("embedding:bge")
    assert [args[3] for args in seen] == [1, 4]


def test_truncated_page_is_retried_as_complete_regions():
    from PIL import Image
    source = io.BytesIO()
    Image.new("RGB", (600, 1000), "white").save(source, format="PNG")
    replies = iter([("length", "不完整内容"), ("stop", "上半页\n边界文字"), ("stop", "边界文字\n下半页")])
    dimensions = []
    def handler(request):
        import base64
        data = json.loads(request.content)["messages"][0]["content"][0]["image_url"]["url"].split(",", 1)[1]
        with Image.open(io.BytesIO(base64.b64decode(data))) as img:
            dimensions.append(img.size)
        reason, content = next(replies)
        return httpx.Response(200, json={"choices": [{"finish_reason": reason, "message": {"content": content}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ocr = OcrClient(OcrProfile(base_url="http://ocr-test/v1", model="PaddleOCR-VL-1.5"), client=client)
        assert ocr.recognize_image(source.getvalue()) == "上半页\n边界文字\n下半页"
    assert dimensions == [(600, 1000), (600, 548), (600, 548)]


def test_region_fallback_stays_bounded_and_never_accepts_partial_text():
    from PIL import Image
    source = io.BytesIO()
    Image.new("RGB", (600, 1000), "white").save(source, format="PNG")
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "不完整内容"}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ocr = OcrClient(OcrProfile(base_url="http://ocr-test/v1", model="PaddleOCR-VL-1.5"), client=client)
        with pytest.raises(OcrError, match="不完整页"):
            ocr.recognize_image(source.getvalue())
    assert len(calls) == 3


def test_fifty_page_scanned_pdf_keeps_all_pages_with_bounded_rendering(monkeypatch):
    pdf = PdfWriter()
    for _ in range(50):
        pdf.add_blank_page(width=100, height=100)
    source = io.BytesIO()
    pdf.write(source)
    rendered = []
    def render(raw, pages):
        rendered.append(list(pages))
        return [(page, str(page).encode()) for page in pages]
    monkeypatch.setattr(parsers, "render_pdf_pages", render)
    ocr = SimpleNamespace(available=True, recognize_image=lambda data, hint="": "第" + data.decode() + "页正文")
    result = parsers.parse_bytes(source.getvalue(), "50页合同.pdf", ocr=ocr)  # pyright: ignore[reportArgumentType]
    assert len(result.blocks) == 50
    assert [block.page for block in result.blocks] == list(range(1, 51))
    assert max(map(len, rendered)) == 1


def test_explicit_ocr_limit_still_fails_before_recognition(monkeypatch):
    pdf = PdfWriter()
    for _ in range(3):
        pdf.add_blank_page(width=100, height=100)
    source = io.BytesIO()
    pdf.write(source)
    with pytest.raises(parsers.ParseError, match="超过单份上限"):
        parsers.parse_bytes(source.getvalue(), "3页.pdf", ocr=SimpleNamespace(available=True), ocr_pages=2)  # pyright: ignore[reportArgumentType]


def test_long_embedding_input_uses_all_text_and_returns_one_vector_per_chunk():
    texts = ["项目合同金额与付款安排。" * 110, "短文本"]
    seen = []
    def handler(request):
        payload = json.loads(request.content)
        seen.extend(payload["input"])
        assert all(len(text.encode()) <= 480 for text in payload["input"])
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [1., 0.]}
                                                   for i, _ in enumerate(payload["input"])]})
    profile = embedding.ProfileInfo(name="bge1024", model="bge-large-zh-v1.5", model_version="v1",
                                    dimension=2, base_url="http://test/v1", note="")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        vectors = embedding.embed_texts(texts, profile=profile, client=client)
    assert len(vectors) == 2 and vectors == [[1., 0.], [1., 0.]]
    assert "".join(seen) == "".join(texts)


@pytest.mark.django_db
def test_document_font_and_color_options_persist_and_export(auth_api, user):
    api = auth_api(user)
    options = {"title_font": "xiaobiaosong", "body_font": "fangsong", "heading_font": "heiti",
               "subheading_font": "kaiti", "title_color": "red", "text_color": "black"}
    result = api.post("/api/v1/planner/documents/", {"title": "通知", "content_markdown": "## 工作安排\n\n公文正文。",
        "format_options": options}, format="json")
    assert result.status_code == 201, result.data
    pk = result.data["document_id"]
    assert api.get(f"/api/v1/planner/documents/{pk}/").data["format_options"] == options
    document = DerivedDocument.objects.get(pk=pk)
    word = Document(io.BytesIO(build_export(document, "docx").content))
    assert str(word.paragraphs[0].runs[0].font.color.rgb) == "FF0000"
    assert "方正小标宋简体" in word.paragraphs[0]._p.xml
    assert "仿宋" in word.paragraphs[-1]._p.xml
    assert "000000" in word.paragraphs[-1]._p.xml
    html = build_export(document, "html").content.decode()
    assert "方正小标宋简体" in html and "#FF0000" in html
    saved = api.put(f"/api/v1/planner/documents/{pk}/", {"content_markdown": "修改正文", "format_options": {"body_font": "kaiti"}}, format="json")
    assert saved.status_code == 200 and saved.data["format_options"] == {"body_font": "kaiti"}


@pytest.mark.django_db
def test_format_options_reject_css_injection_and_unknown_values(auth_api, user):
    response = auth_api(user).post("/api/v1/planner/documents/", {"title": "通知", "content_markdown": "正文",
        "format_options": {"title_font": 'evil";color:red'}}, format="json")
    assert response.status_code == 400
    assert not DerivedDocument.objects.exists()


@pytest.mark.django_db
def test_admin_can_retry_known_service_failure_without_resetting_history(admin_user, user, library, make_file, monkeypatch):
    from apps.knowledge import pipeline
    from apps.knowledge.models import ParseStatus
    record = make_file(library, status=ParseStatus.FAILED, parse_attempts=5,
                       parse_error="ParseError: 第 1 页 OCR 失败：OCR 服务不可用：请求超时")
    calls = []
    monkeypatch.setattr(pipeline, "process_file", lambda *args, **kwargs: calls.append(kwargs))
    pipeline.retry_file(record, actor=admin_user)
    assert calls[-1]["consume_budget"] is False
    pipeline.retry_file(record, actor=user)
    assert calls[-1]["consume_budget"] is True
    pipeline.retry_file(record)
    assert calls[-1]["consume_budget"] is True
    record.refresh_from_db()
    assert record.parse_attempts == 5
