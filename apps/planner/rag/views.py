"""Authenticated source links. Escaped HTML permits inspecting an exact cited passage."""
from hashlib import sha256
from django.http import HttpResponse
from django.utils.html import escape
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from apps.knowledge.models import DocumentChunk
from .reader import readable_file


class EvidenceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from apps.planner.services import PlannerError
        try:
            record = readable_file(request.user, pk)
        except PlannerError:
            return Response({"detail": "来源不存在或无权查看"}, status=404)
        try:
            ordinal = int(request.query_params.get("ordinal", ""))
        except (TypeError, ValueError):
            return Response({"detail": "请指定有效的段落位置"}, status=400)
        version = request.query_params.get("version")
        digest = request.query_params.get("text_hash")
        chunk = DocumentChunk.objects.filter(file=record, ordinal=ordinal).first()
        if (chunk is None or version != record.content_sha256 or
                digest != sha256(chunk.text.encode()).hexdigest()):
            return Response({"detail": "来源版本或段落已变化，请重新查证"}, status=409)
        location = f"第 {chunk.page} 页 · 第 {ordinal} 段" if chunk.page else f"第 {ordinal} 段"
        response = HttpResponse(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(record.original_name)} · 引用原文</title>'
            '<body style="max-width:900px;margin:40px auto;padding:0 20px;font:17px/1.8 sans-serif">'
            f'<h1>{escape(record.original_name)}</h1><p>{escape(record.library.name)} · {location}</p>'
            f'<pre style="white-space:pre-wrap;overflow-wrap:anywhere;font:inherit">{escape(chunk.text)}</pre>'
            '<p>此处为引用段落，不能代表整份文件内容。</p></body></html>', content_type="text/html; charset=utf-8")
        response["Cache-Control"] = "no-store, private"
        response["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class ResearchRecordView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        import json
        from apps.planner.models import OperationRecord
        from .tool_evidence import accessible, digest, facts
        record = OperationRecord.objects.filter(pk=pk).first()
        if record is None or not accessible(request.user, record):
            return Response({"detail": "查证记录不存在或无权查看"}, status=404)
        if request.query_params.get("digest") != digest(record.result):
            return Response({"detail": "查证记录已变化"}, status=409)
        content = json.dumps(facts(record), ensure_ascii=False, indent=2)
        response = HttpResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>查证记录</title><body><h1>查证记录</h1><p>以下为当次工具执行结果，仅代表所列范围。</p>'
            f'<pre style="white-space:pre-wrap">{escape(content)}</pre></body></html>', content_type="text/html; charset=utf-8")
        response["Cache-Control"] = "no-store, private"
        response["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
        return response
