"""项目根目录与运行期目录的唯一真源。

本模块是"向量数据必须落在项目根目录"这条硬约束的唯一实现点：
任何需要向量目录的代码都必须调用 :func:`vector_data_dir`，不允许自行拼接路径。
"""

from __future__ import annotations

import os
from pathlib import Path

# knowledgebase/paths.py -> knowledgebase/ -> 项目根
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


class StoragePathError(RuntimeError):
    """运行期目录配置不合法。"""


def _from_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


def vector_data_dir() -> Path:
    """向量数据根目录，默认 ``<项目根>/data/vector_data``。"""
    return _from_env("VECTOR_DATA_DIR", PROJECT_ROOT / "data" / "vector_data")


def qdrant_server_dir() -> Path:
    """Qdrant server 的 storage 挂载点（容器 bind mount 到这里）。"""
    return vector_data_dir() / "qdrant"


def qdrant_embedded_dir() -> Path:
    """qdrant-client 本地嵌入模式的落盘目录。

    注意：嵌入模式由本地文件锁串行化，同一时刻只允许一个进程持有。
    """
    return vector_data_dir() / "embedded"


def raw_store_dir() -> Path:
    """已入库资料的**原始字节**存放根目录，默认 ``<项目根>/data/media/raw``。

    为什么放在项目根而不是外部对象存储：
    - 需求 2 的同类约束（数据可自证位置）；原始字节是本系统的"事实来源"，
      绝不能出现"数据库里有记录、但文件在别处"的状态。
    - 内容寻址（按 sha256 分片）让"重试不重复写"是天然的：同一份内容
      重复落盘就是同一个路径，写之前先校验，存在且哈希一致就直接复用。
    """
    return _from_env("RAW_STORE_DIR", PROJECT_ROOT / "data" / "media" / "raw")


def assert_inside_project_root(path: Path, *, label: str) -> Path:
    """断言 ``path`` 位于项目根目录内，否则抛错。

    这是需求 2 的强制点：向量库不允许落到项目外的任意位置。
    """
    resolved_root = PROJECT_ROOT.resolve()
    candidate = path if path.is_absolute() else (PROJECT_ROOT / path)
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise StoragePathError(
            f"{label} 必须位于项目根目录 {resolved_root} 内，当前解析为 {resolved}"
        )
    return resolved


def ensure_runtime_dirs() -> dict[str, Path]:
    """创建并校验运行期目录，返回各自路径。启动与单测都会调用。"""
    created: dict[str, Path] = {}
    for label, path in (
        ("vector_data", vector_data_dir()),
        ("qdrant", qdrant_server_dir()),
        ("embedded", qdrant_embedded_dir()),
        ("db", _from_env("DB_DIR", PROJECT_ROOT / "data" / "db")),
        ("media", _from_env("MEDIA_ROOT_DIR", PROJECT_ROOT / "data" / "media")),
        ("raw_store", raw_store_dir()),
    ):
        safe = assert_inside_project_root(path, label=label)
        safe.mkdir(parents=True, exist_ok=True)
        created[label] = safe
    return created
