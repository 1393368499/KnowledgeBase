"""智能检索中的文件名召回与精确命中优先级。"""
from dataclasses import replace
import heapq
import re
import unicodedata

from django.db.models import F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Replace

from .models import DocumentChunk, IngestedFile, ParseStatus
from .vectorindex import SearchCandidate

DASHES = "‐‑‒–—−－"
EXTENSION = re.compile(r"\.(?:pdf|docx?|dotx|pptx?|potx|xlsx?|csv|txt|md)$", re.I)
REASONS = {4: "exact_filename", 3: "exact_stem", 2: "filename_token", 1: "filename_contains"}


def normalize(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return " ".join(value.translate(str.maketrans({dash: "-" for dash in DASHES})).split())


def match_rank(name, query):
    name, query = normalize(name), normalize(query)
    if not query:
        return 0
    if name == query:
        return 4
    stem, term = EXTENSION.sub("", name), EXTENSION.sub("", query)
    if not term:
        return 0
    if stem == term:
        return 3
    start = stem.find(term)
    if start < 0:
        return 0
    # 编号 -8 必须在数字边界结束，不能把 -80/-81 当成完整编号。
    for match in re.finditer(re.escape(term), stem):
        before = stem[match.start() - 1:match.start()] if match.start() else ""
        after = stem[match.end():match.end() + 1]
        left_ok = not (re.match(r"[a-z0-9]", term[0]) and re.match(r"[a-z0-9]", before))
        right_ok = not (re.match(r"[a-z0-9]", term[-1]) and re.match(r"[a-z0-9]", after))
        if left_ok and right_ok:
            return 2
    return 1


def recall(*, query, library_ids, limit, eligible_file_ids=None):
    term = normalize(query)
    stem_term = EXTENSION.sub("", term)
    if len(term) < 2 or not stem_term or not library_ids or eligible_file_ids == set():
        return []
    lookup = F("original_name")
    for dash in DASHES:
        lookup = Replace(lookup, Value(dash), Value("-"))
    lookup = Replace(lookup, Value("\u3000"), Value(" "))
    files = IngestedFile.objects.filter(
        library_id__in=library_ids, library__is_active=True,
    ).exclude(parse_status=ParseStatus.ARCHIVED)
    if eligible_file_ids is not None:
        files = files.filter(pk__in=eligible_file_ids)
    files = files.annotate(_lookup=lookup).filter(
        Q(_lookup__icontains=term) | Q(_lookup__icontains=stem_term)
    )
    # 检查全部名称命中，只保留最优的 limit 份，不能让低 ID 的相近名称截掉精确项。
    chosen = heapq.nsmallest(
        limit, files.values_list("id", "original_name").iterator(chunk_size=500),
        key=lambda row: (-match_rank(row[1], query), row[0]),
    )
    ids = [row[0] for row in chosen]
    first_chunk = DocumentChunk.objects.filter(file_id=OuterRef("pk")).order_by("ordinal", "pk")
    records = {row.pk: row for row in IngestedFile.objects.filter(pk__in=ids)
               .select_related("library").annotate(_chunk_id=Subquery(first_chunk.values("pk")[:1]))}
    chunks = DocumentChunk.objects.in_bulk([row._chunk_id for row in records.values() if row._chunk_id])
    output = []
    for pk in ids:
        record = records.get(pk)
        if record is None:
            continue
        chunk = chunks.get(record._chunk_id)
        output.append(SearchCandidate(
            point_id=f"db:{chunk.pk}" if chunk else f"filename:{record.pk}",
            score=0.0, collection="db-filename", payload={
                "file_id": record.pk, "library_id": record.library_id,
                "library_code": record.library.code, "original_name": record.original_name,
                "content_sha256": record.content_sha256, "permission_code": record.permission_code,
                "directory_path": record.directory_path, "ordinal": chunk.ordinal if chunk else None,
                "page": chunk.page if chunk else None, "text": chunk.text if chunk else "",
                "classification": record.classification, "source_layer": "filename",
            },
        ))
    return output


def promote(candidates, query):
    """每个名称命中文件只提升一个最佳片段，其他真实片段继续参与正文排序。"""
    names = dict(IngestedFile.objects.filter(
        pk__in=[item.payload.get("file_id") for item in candidates]
    ).values_list("id", "original_name"))
    promoted, rest, seen = [], [], set()
    for index, candidate in enumerate(candidates):
        pk = candidate.payload.get("file_id")
        rank = match_rank(names.get(pk, ""), query)
        if rank and pk not in seen:
            seen.add(pk)
            payload = dict(candidate.payload, filename_match_rank=rank, filename_match=REASONS[rank])
            promoted.append((rank, index, replace(candidate, payload=payload)))
        else:
            rest.append(candidate)
    promoted.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in promoted] + rest
