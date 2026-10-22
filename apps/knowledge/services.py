"""资料入库服务：内容指纹、去重、审计留痕。

关键点：
- 入库只做"追加"，不改任何既有行；
- `(library, content_sha256)` 唯一约束 + 服务层先查一次，双击上传不会产生重复数据；
- 每次成功入库都写审计事件（append-only）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework import serializers

from apps.audit.services import append_event

from .models import DocumentChunk, IngestedFile, KnowledgeLibrary, ParseStatus
from .storage import StoredBlob, store_bytes, store_stream

CHUNK_READ_BYTES = 1024 * 1024


@dataclass(frozen=True)
class IngestResult:
    file: IngestedFile
    created: bool
    reason: str = ""
    #: 原始字节落盘结果；重试/重复上传时 reused=True 表示复用了已有副本
    blob: StoredBlob | None = None


def allowed_file(name: str, *, allowed_ext: list[str] | None = None) -> bool:
    suffix = Path(name).suffix.lower()
    allowed = allowed_ext if allowed_ext is not None else list(settings.KB_ALLOWED_UPLOAD_EXT)
    return suffix in set(allowed)


def sha256_of_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(CHUNK_READ_BYTES), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def ingest_file(
    *,
    library: KnowledgeLibrary,
    original_name: str,
    content_sha256: str,
    size_bytes: int,
    actor=None,
    directory_path: str = "",
    classification: str = "",
    classification_source: str = "",
    customer: str = "",
    department: str = "",
    amount_wan: float | None = None,
    source: str = "manual",
    conversation_key: str = "",
    file_type: str = "",
    blob: StoredBlob | None = None,
) -> IngestResult:
    """创建一条入库记录；已存在同内容资料时幂等返回既有行。

    ``blob`` 是原始字节落盘结果（内容寻址，见 :mod:`apps.knowledge.storage`）。
    没有它就无法解析，因此正常上传路径必须传；测试里造纯元数据行时可不传。
    """
    existing = IngestedFile.objects.filter(
        library=library, content_sha256=content_sha256
    ).first()
    if existing is not None:
        # 去重命中不等于"什么都不做"：如果这条老记录当年没落盘（P1 阶段的行），
        # 这次上传的字节正好可以补齐事实来源；补齐走受控通道（不改内容字段）。
        if blob is not None and not existing.raw_path:
            _record_blob(existing, blob)
        append_event(
            action="file.ingest_skipped",
            actor_id=getattr(actor, "pk", None),
            actor_label=getattr(actor, "get_username", lambda: "")() if actor else "",
            resource_kind="ingested_file",
            resource_key=str(existing.pk),
            conversation_key=conversation_key,
            payload={
                "reason": "duplicate_content",
                "library": library.code,
                "original_name": original_name,
            },
        )
        return IngestResult(file=existing, created=False, reason="duplicate_content", blob=blob)

    try:
        with transaction.atomic():
            # **写冻结闸门**：换模型/重建窗口内，新上传必须被**显式拒绝**，
            # 而不是写进一个正在被替换的代次。同事务内锁闸门行 → 无 TOCTOU 窗口。
            from .writegate import assert_writable

            assert_writable(library.pk, action="新建入库记录")
            record = IngestedFile(
                library=library,
                original_name=original_name,
                content_sha256=content_sha256,
                size_bytes=size_bytes,
                file_type=file_type or Path(original_name).suffix.lower().lstrip("."),
                directory_path=directory_path,
                classification=classification,
                # 上传入口显式给的性质 = **人工设置**（自动识别不得覆盖它）；
                # 留空表示"待识别"，由流水线按正文自动判定。
                classification_source=classification_source,
                customer=customer or library.customer,
                department=department or library.department,
                # 金额（万元）：原型搜索页的"金额下限"筛选依赖它。
                # 为空表示未填，不参与金额筛选（而不是当成 0）。
                amount_wan=amount_wan,
                owner=actor if getattr(actor, "pk", None) else None,
                parse_status=ParseStatus.PENDING,
                source=source,
                vector_collection=library.vector_collection,
                # 原始字节的落盘位置必须**在创建时就写进去**（审查意见：
                # 首次上传记录缺 raw_path，后续 process 接口会直接拒绝）。
                # 这两个字段是受控可写列，所以创建时赋值不会被触发器拦。
                raw_path=blob.rel_path if blob is not None else "",
                raw_size_bytes=blob.size_bytes if blob is not None else 0,
            )
            record.save()
            append_event(
                action="file.ingested",
                actor_id=getattr(actor, "pk", None),
                actor_label=getattr(actor, "get_username", lambda: "")() if actor else "",
                resource_kind="ingested_file",
                resource_key=str(record.pk),
                conversation_key=conversation_key,
                payload={
                    "library": library.code,
                    "original_name": original_name,
                    "sha256": content_sha256,
                    "size_bytes": size_bytes,
                    "source": source,
                },
            )
    except IntegrityError:
        # 并发下另一路已插入：回读并返回既有行
        existing = IngestedFile.objects.filter(
            library=library, content_sha256=content_sha256
        ).first()
        if existing is None:
            raise
        return IngestResult(file=existing, created=False, reason="concurrent_duplicate", blob=blob)

    return IngestResult(file=record, created=True, blob=blob)


def _record_blob(record: IngestedFile, blob: StoredBlob) -> None:
    """把原始字节的落盘位置写进记录（受控字段，只写 raw_path/raw_size_bytes）。"""
    from governance.registry import controlled_save

    record.raw_path = blob.rel_path
    record.raw_size_bytes = blob.size_bytes
    controlled_save(record, fields=["raw_path", "raw_size_bytes"])


def store_upload(upload, *, expected_sha256: str = "") -> StoredBlob:
    """把上传流落成不可覆盖的只读副本，返回内容寻址结果。"""
    return store_stream(upload, expected_sha256=expected_sha256)


def add_chunks(file: IngestedFile, texts: list[str]) -> list[DocumentChunk]:
    """写入解析分段（派生只读数据）。返回新建的分段列表。"""
    created: list[DocumentChunk] = []
    base = file.chunks.count()
    with transaction.atomic():
        # **写冻结闸门**：解析产出也要进这道门（换模型窗口内不再追加分段）。
        from .writegate import assert_writable

        assert_writable(file.library_id, action="写入解析分段")
        for offset, text in enumerate(texts):
            chunk = DocumentChunk(
                file=file,
                ordinal=base + offset,
                text=text,
                permission_code=file.permission_code,
                token_estimate=max(1, len(text) // 2),
            )
            chunk.save()
            created.append(chunk)
    return created


def require_valid_upload(size_bytes: int, original_name: str) -> None:
    limit = int(settings.KB_UPLOAD_MAX_BYTES)
    if size_bytes <= 0:
        raise serializers.ValidationError({"file": "文件为空"})
    if size_bytes > limit:
        raise serializers.ValidationError(
            {"file": f"文件超过上限 {limit // (1024 * 1024)} MB"}
        )
    if not allowed_file(original_name):
        raise serializers.ValidationError(
            {"file": f"不支持的文件类型，允许：{', '.join(settings.KB_ALLOWED_UPLOAD_EXT)}"}
        )
