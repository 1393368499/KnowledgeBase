"""原始字节存储：内容寻址、写一次、可校验。

为什么不用 Django 的 ``FileSystemStorage``
------------------------------------------
需求要求"原始字节真实持久化 + 哈希 + 不可覆盖"。``FileSystemStorage`` 遇到同名文件
会自动改名（``a.pdf`` → ``a_XXXX.pdf``），这会让"同内容只落一份"变成"同内容落多份"，
而且改名本身就无法证明"没有覆盖"。所以这里自己实现一层薄的、**按内容寻址**的存储：

- 路径固定为 ``<项目根>/media/raw/<sha256 前两位>/<sha256>``，与文件名无关；
- 写入方式：先写 ``*.part`` 临时文件并**边写边算哈希**，全部写完再 ``os.replace``
  原子改名；哈希对不上就删掉临时文件并拒绝——绝不会留下半个文件；
- 已存在且校验通过就直接复用（幂等）；已存在但内容不符 → 抛 :class:`BlobCorrupt`
  （这是数据损坏，**不静默覆盖**）；
- 存储层不提供 ``overwrite`` / ``delete`` 接口。合法删除必须走 P4 的三次确认通道，
  由那条通道显式调用 :func:`quarantine_blob`（移入隔离目录，而不是抹掉）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable

from config.paths import PROJECT_ROOT, assert_inside_project_root

#: 流式读写块大小
CHUNK_BYTES = 1024 * 1024


class StorageError(RuntimeError):
    """存储层错误基类。"""


class BlobExists(StorageError):
    """目标 blob 已存在（正常路径不会走到这里，写入前都会先查）。"""


class BlobCorrupt(StorageError):
    """已存在的 blob 内容与它的内容地址不一致——数据损坏，拒绝覆盖。"""


class BlobNotFound(StorageError):
    """要读取的 blob 不存在。"""


@dataclass(frozen=True)
class StoredBlob:
    """一次落盘的结果。``reused=True`` 表示复用了已存在的同一份内容。"""

    sha256: str
    size_bytes: int
    path: Path
    reused: bool = False

    @property
    def rel_path(self) -> str:
        """相对原始字节根目录的路径，直接写进 ``IngestedFile.raw_path``。

        用相对路径入库的理由：项目目录整体搬走后记录仍然有效（绝对路径会立刻失效）。

        为什么用 ``raw_root()`` 而不是 ``PROJECT_ROOT`` 算相对：路径真源只有一个
        （``config.paths.raw_store_dir``）。测试或本地模式把它指向别处时这里跟着走，
        不会因为"两个根"算出跨盘相对路径（Windows 上跨盘 relpath 会直接抛异常）。
        """
        import os

        return os.path.relpath(self.path, raw_root()).replace("\\", "/")

    @property
    def read_only(self) -> bool:
        """磁盘上这份副本是否真的是只读的（验收时用来证明"不可覆盖"）。"""
        import os

        return self.path.is_file() and not os.access(self.path, os.W_OK)


def raw_root() -> Path:
    """原始字节根目录：``<项目根>/media/raw``。

    路径真源在 :func:`config.paths.raw_store_dir`（已做项目根内校验），
    这里不再自己拼路径，避免两处口径分叉。
    """
    from config.paths import raw_store_dir

    return assert_inside_project_root(raw_store_dir(), label="raw_store_dir")


def quarantine_root() -> Path:
    """受控删除的隔离目录：``<项目根>/media/quarantine``。"""
    return assert_inside_project_root(raw_root().parent / "quarantine", label="media/quarantine")


def blob_path(sha256: str) -> Path:
    """内容地址 → 绝对路径。校验摘要格式，避免路径穿越。"""
    digest = (sha256 or "").strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise StorageError(f"非法内容摘要：{sha256!r}")
    return raw_root() / digest[:2] / digest


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_blob(sha256: str) -> bool:
    """重算磁盘上的字节，确认它确实是这个内容地址对应的数据。"""
    path = blob_path(sha256)
    if not path.is_file():
        return False
    return sha256_of_file(path) == sha256.strip().lower()


def store_stream(stream: BinaryIO, *, expected_sha256: str = "") -> StoredBlob:
    """把流的原始字节写进内容地址；返回落盘结果。

    ``expected_sha256`` 由调用方（上传接口在接收字节时）算出并传入——这样
    "接收时的哈希"和"落盘时的哈希"是同一个值，任何环节被改动都会在这里暴露。
    """
    root = raw_root()
    root.mkdir(parents=True, exist_ok=True)
    # 临时文件必须落在同一文件系统内，os.replace 才是原子的
    tmp_dir = root / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{uuid.uuid4().hex}.part"

    digest = hashlib.sha256()
    size = 0
    try:
        with open(tmp_path, "wb") as handle:
            for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                if not chunk:
                    continue
                digest.update(chunk)
                size += len(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())

        actual = digest.hexdigest()
        expected = (expected_sha256 or "").strip().lower()
        if expected and expected != actual:
            raise StorageError(
                f"落盘哈希与接收哈希不一致：接收 {expected}，落盘 {actual}；已丢弃临时文件"
            )
        target = blob_path(actual)

        if target.is_file():
            # 同一份内容已经在库里：校验后复用，绝不覆盖
            if not verify_blob(actual):
                raise BlobCorrupt(
                    f"已存在的 {target} 内容与摘要不符，拒绝写入以免掩盖损坏：{actual}"
                )
            return StoredBlob(sha256=actual, size_bytes=size, path=target, reused=True)

        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_path, target)
        _mark_read_only(target)
        return StoredBlob(sha256=actual, size_bytes=size, path=target, reused=False)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:  # pragma: no cover - 清理失败不影响主流程结果
                pass


def store_bytes(raw: bytes, *, expected_sha256: str = "") -> StoredBlob:
    """字节串落盘（小文件与测试用；大文件走 :func:`store_stream`）。"""
    import io

    return store_stream(io.BytesIO(raw), expected_sha256=expected_sha256)


def open_blob(sha256: str) -> BinaryIO:
    """打开原始字节用于解析。打开前校验内容地址，避免把损坏数据喂给解析器。"""
    path = blob_path(sha256)
    if not path.is_file():
        raise BlobNotFound(f"原始文件不存在：{path}")
    if not verify_blob(sha256):
        raise BlobCorrupt(f"原始文件已损坏（内容与摘要不符）：{path}")
    return open(path, "rb")  # noqa: SIM115 - 由调用方负责关闭


def read_blob(sha256: str) -> bytes:
    with open_blob(sha256) as handle:
        return handle.read()


def quarantine_blob(sha256: str, *, reason: str = "") -> Path | None:
    """把原始字节移入隔离目录（受控删除通道专用）。

    **不是**删除：文件仍在项目根下、仍在审计里可追溯，只是从 ``media/raw`` 移走，
    让系统不再引用它。任何人都不应该直接从业务代码调用它——只有三次确认删除通道可以。
    """
    path = blob_path(sha256)
    if not path.is_file():
        return None
    target_dir = quarantine_root()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{sha256}.{uuid.uuid4().hex[:8]}.bin"
    shutil.move(str(path), str(target))
    if reason:
        (target_dir / f"{target.name}.reason.txt").write_text(reason, encoding="utf-8")
    return target


def iter_blobs() -> Iterable[StoredBlob]:
    """遍历所有已落盘 blob（校验脚本/巡检用）。"""
    root = raw_root()
    if not root.is_dir():
        return
    for parent in sorted(root.iterdir()):
        if not parent.is_dir() or parent.name == ".tmp":
            continue
        for item in sorted(parent.iterdir()):
            if item.is_file() and len(item.name) == 64:
                yield StoredBlob(sha256=item.name, size_bytes=item.stat().st_size, path=item)


def _mark_read_only(path: Path) -> None:
    """把文件标成只读：把"不可覆盖"从约定变成文件系统属性。"""
    try:
        mode = path.stat().st_mode
        path.chmod(mode & ~0o222)
    except OSError:  # pragma: no cover - Windows 上偶发失败不影响正确性
        pass
