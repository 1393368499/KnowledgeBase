"""Qdrant 向量索引：generation 化的 collection、真实 upsert、混合检索。

数据位置（硬约束）
------------------
所有向量数据都在项目根下：真源是 :func:`config.paths.qdrant_server_dir`（server 模式，
容器 bind mount 到这里）与 :func:`config.paths.qdrant_embedded_dir`（本地模式）。
本模块**不允许**出现任何自拼路径。

generation 与"切换不抖动"
--------------------------
每个信息库的索引有 generation 版本号。collection 名为
``<前缀>-<库id>-g<generation>``。因为维度写死在 collection 里，换模型**只能新建**：

1. ``prepare_generation``：库行 ``select_for_update`` 锁住 → generation+1 → 建新 collection
   → 落一条 ``VectorCollection`` 登记（模型/维度/版本）。
2. 全量重嵌入写进新 collection（期间检索仍读旧 generation，线上不受影响）。
3. ``activate_generation``：一个事务里更新库的 ``active_generation`` 与每个文件的
   ``vector_collection_name``；旧 collection 原样保留，可一条命令回退。

并发切换靠两件事兜底：库行的行锁，以及 ``VectorCollection`` 上
``uniq_active_collection_per_library`` 这个"每库只有一个 active"的部分唯一索引。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from django.conf import settings
from django.db import transaction

from config.paths import qdrant_embedded_dir, qdrant_server_dir

from .models import IngestedFile, KnowledgeLibrary, ParseStatus, VectorCollection

logger = logging.getLogger(__name__)

#: 稠密向量距离：中文检索用余弦
DISTANCE = "Cosine"


class VectorIndexError(RuntimeError):
    """向量索引层错误。"""


class CollectionNotFound(VectorIndexError):
    """collection 不存在（通常是 generation 还没建）。"""


class DimensionMismatch(VectorIndexError):
    """写入向量维度与 collection 不一致——必须先建新 collection，不能原地改。"""


class IncompleteIndex(VectorIndexError):
    """目标 collection 覆盖不完整（缺点或内容不符）——拒绝切换上线。"""


#: 覆盖核验时一次 retrieve 多少个点（避免超大单请求）
COVERAGE_BATCH = 256


# ------------------------------------------------------------------ 命名 ----


def collection_prefix() -> str:
    return str(getattr(settings, "KB_QDRANT_COLLECTION_PREFIX", "kb")).strip("-") or "kb"


def collection_name(library_id: int, generation: int) -> str:
    """库 + generation → collection 名。名字里带 generation 才能"新旧并存"。"""
    return f"{collection_prefix()}-{library_id}-g{generation}"


def active_collection_name(library_id: int, generation: int) -> str:
    """按当前运行口径取 collection 名。

    只有一个地方会不一样：**离线测试适配器打开时**，名字要带测试后缀
    （``__offline_test``）。适配器会拒绝写任何不带该后缀的 collection，
    所以测试必须走"带后缀"的名字，才能在跑真实代码路径的同时保证
    假向量永远不会进入正式索引。
    """
    name = collection_name(library_id, generation)
    try:
        from .testing_adapter import TEST_COLLECTION_SUFFIX, enabled as adapter_enabled

        if adapter_enabled() and not name.endswith(TEST_COLLECTION_SUFFIX):
            return f"{name}{TEST_COLLECTION_SUFFIX}"
    except Exception:  # noqa: BLE001 - 适配器不可用就当没开
        pass
    return name


def logical_collection_name(library: KnowledgeLibrary) -> str:
    """库级的逻辑名（写进 ``vector_collection`` 字段，人看的那一层）。"""
    return collection_name(library.pk, library.active_generation)


# ------------------------------------------------------------------ 连接 ----


def qdrant_mode() -> str:
    """``server`` 或 ``embedded``。默认 server（生产口径）。"""
    return str(getattr(settings, "QDRANT_MODE", "server")).lower()


def qdrant_url() -> str:
    return str(getattr(settings, "QDRANT_URL", "http://127.0.0.1:6333")).rstrip("/")


#: 每个进程只探测/记录一次服务端版本（F-07：版本差异要可见，但不得影响业务）。
_QDRANT_VERSION_LOGGED = False


def _log_qdrant_server_version() -> None:
    """尽力记录 Qdrant 服务端版本，便于发现客户端/服务端版本漂移。

    ``check_compatibility=False`` 是**刻意的取舍**：客户端固定 1.16.2、服务端固定
    1.16.3（见 docs/office-qdrant-selection-2026-11-09.md），若开启兼容检查，
    上游小版本一动就会在运行时抛错。代价是版本漂移不再自动报警——所以这里把
    服务端版本打进日志，并在版本与客户端主次版本不一致时用 warning 提示。
    """
    global _QDRANT_VERSION_LOGGED
    if _QDRANT_VERSION_LOGGED:
        return
    _QDRANT_VERSION_LOGGED = True
    try:
        import httpx
        from importlib.metadata import version as _package_version

        with httpx.Client(timeout=3.0, trust_env=False) as client:
            payload = client.get(f"{qdrant_url()}/").json()
        server_version = str(payload.get("version") or "unknown")
        client_version = _package_version("qdrant-client")
        same_minor = server_version.split(".")[:2] == client_version.split(".")[:2]
        log = logger.info if same_minor else logger.warning
        log(
            "Qdrant server version=%s client=%s（兼容检查已关闭，版本漂移不会自动报错）",
            server_version,
            client_version,
        )
    except Exception:  # noqa: BLE001 - 版本探测失败不影响业务
        logger.debug("无法探测 Qdrant 服务端版本", exc_info=True)


def build_client():
    """构造 qdrant 客户端。

    server 模式连 HTTP；embedded 模式用本地文件模式，路径固定在
    ``vector_data/embedded``，并且它由本地文件锁串行化——**只允许单进程**，
    多 worker 会直接冲突，所以生产必须用 server 模式（见 docs/phase-2-completion.md）。

    ``trust_env=False`` 直接作为关键字传给 qdrant-client：它把不认识的 kwargs
    透传给内部的 ``httpx.Client``（已实测：``ApiClient(host, **kwargs)`` →
    ``Client(**kwargs)``），所以**不需要**再去改进程级 ``os.environ``。
    这一点很重要——改全局环境在多线程下会互相污染快照（审查意见 8）。
    """
    from qdrant_client import QdrantClient

    if qdrant_mode() == "embedded":
        path = qdrant_embedded_dir()
        path.mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=str(path))
    _log_qdrant_server_version()
    return QdrantClient(
        url=qdrant_url(),
        api_key=getattr(settings, "QDRANT_API_KEY", "") or None,
        timeout=int(getattr(settings, "QDRANT_TIMEOUT_SECONDS", 10)),
        check_compatibility=False,
        # 宿主 NO_PROXY 里有畸形条目（带方括号无端口），httpx 会在**构造**阶段
        # 抛 InvalidURL。trust_env=False 让它彻底不读代理环境变量：
        # 内网服务本来也不该走代理。
        trust_env=False,
    )


def server_storage_bound_to_project() -> bool:
    """server 模式的落盘目录是否确实在项目根下（巡检用）。"""
    return qdrant_server_dir().is_dir()


# ------------------------------------------------------------------ collection ----


def collection_exists(client, name: str) -> bool:
    try:
        return bool(client.collection_exists(name))
    except AttributeError:  # pragma: no cover - 极旧版本兼容
        try:
            client.get_collection(name)
            return True
        except Exception:  # noqa: BLE001
            return False


def ensure_collection(client, name: str, dimension: int, *, recreate: bool = False):
    """建 collection（幂等）。**已存在且维度不符时抛错，绝不原地改维度。**"""
    from qdrant_client.models import Distance, VectorParams

    if collection_exists(client, name):
        info = client.get_collection(name)
        existing = _vector_size(info)
        if existing is not None and existing != dimension:
            if not recreate:
                raise DimensionMismatch(
                    f"collection {name} 现有维度 {existing}，与请求的 {dimension} 不一致。"
                    "换模型必须新建 collection（generation +1），不能原地改维度。"
                )
            client.delete_collection(name)
        else:
            return info
    return client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
    )


def _vector_size(info: Any) -> int | None:
    try:
        params = info.config.params.vectors
    except AttributeError:
        return None
    size = getattr(params, "size", None)
    if size is None and isinstance(params, dict):
        size = params.get("size")
    return int(size) if size else None


def ensure_text_index(client, name: str, field_name: str = "text") -> None:
    """给 payload 的 text 字段建全文索引，供稀疏检索用。"""
    from qdrant_client.models import PayloadSchemaType, TextIndexParams, TokenizerType

    try:
        client.create_payload_index(
            collection_name=name,
            field_name=field_name,
            field_schema=TextIndexParams(
                type=PayloadSchemaType.TEXT, tokenizer=TokenizerType.MULTILINGUAL, lowercase=True
            ),
        )
    except Exception:  # noqa: BLE001 - 已存在索引时 qdrant 会报错，视为成功
        pass


# ------------------------------------------------------------------ 写入 ----


@dataclass(frozen=True)
class VectorPoint:
    point_id: str
    vector: list[float]
    payload: dict[str, Any]


def point_id_for(file: IngestedFile, chunk_ordinal: int) -> str:
    """稳定的点 id：同一份资料的同一段，重试时覆盖同一个点，不会产生重复向量。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kb:{file.pk}:{chunk_ordinal}"))


def upsert_points(client, name: str, points: Sequence[VectorPoint], *, dimension: int) -> int:
    """真实写入 Qdrant。维度不符立即拒绝，绝不"补零凑数"。"""
    from qdrant_client.models import PointStruct

    if not points:
        return 0
    for point in points:
        if len(point.vector) != dimension:
            raise DimensionMismatch(
                f"点 {point.point_id} 的向量是 {len(point.vector)} 维，collection "
                f"{name} 是 {dimension} 维；拒绝写入（不补零、不截断）。"
            )
    client.upsert(
        collection_name=name,
        points=[
            PointStruct(id=point.point_id, vector=point.vector, payload=point.payload)
            for point in points
        ],
        wait=True,
    )
    return len(points)


def count_points(client, name: str) -> int:
    result = client.count(collection_name=name, exact=True)
    return int(getattr(result, "count", 0))


def delete_points(client, name: str, point_ids: Sequence[str]) -> None:
    """删除指定点。只允许受控删除通道调用（资料级联清理向量）。"""
    if not point_ids:
        return
    from qdrant_client.models import PointIdsList

    client.delete(
        collection_name=name, points_selector=PointIdsList(points=list(point_ids)), wait=True
    )


# ------------------------------------------------------------------ generation ----


@dataclass
class GenerationPlan:
    library_id: int
    generation: int
    collection: str
    dimension: int
    model: str
    model_version: str
    previous_generation: int
    previous_collection: str
    created: bool


def _highest_registered_generation(library: KnowledgeLibrary) -> int:
    """该库历史上登记过的最大 generation（从 collection 名里解析）。

    为什么需要它：``next_generation`` 是新增字段，老库可能是 0；
    而"已经建过 g3、g4"这个事实只有登记表知道。取两者最大值才不会重用编号。
    """
    prefix = f"{collection_prefix()}-{library.pk}-g"
    highest = 0
    for name in VectorCollection.objects.filter(library=library).values_list("name", flat=True):
        if not name.startswith(prefix):
            continue
        tail = name[len(prefix) :].split("__", 1)[0]
        try:
            highest = max(highest, int(tail))
        except ValueError:
            continue
    return highest


def _collection_has_files(library: KnowledgeLibrary, collection: str) -> bool:
    """这个 collection 是否已经被任何资料引用（=已经开始写了）。"""
    return IngestedFile._base_manager.filter(
        library=library, vector_collection_name=collection
    ).exists()


def prepare_generation(
    library: KnowledgeLibrary,
    *,
    profile,
    client=None,
) -> GenerationPlan:
    """为某库准备一个**从未用过**的 generation（锁库行，单调分配）。

    审查意见 5 修的就是这里。两条被否掉的老做法：

    1. ``generation = active_generation + 1``：还没激活时连续两次 prepare 会算出同一个
       编号，两个并发构建写进同一个 collection；回退到 g1 后再 prepare 又算出 g2——
       而 g2 是**已经建好、装着旧数据**的索引，等于把新数据写进历史索引；
    2. "复用未激活且没有文件引用的空壳代次"：我们**没有** build_id / lease / 任务表，
       根本无法判断那份"空 collection"是不是正被另一个 worker 写。
       没有证据就不要假设它空闲。

    所以最终口径是**严格单调、每次 prepare 都分配新编号**：

        generation = max(next_generation 计数器, 登记表里的最大编号, active_generation) + 1

    并在同一个事务里把计数器推到该值（锁库行）。计数器只增不减：
    失败、回退、取消都不会让编号被重用。将来若要"同一次构建重复调用只分配一次"，
    正确做法是引入显式 ``build_id`` 幂等键（只有同 build_id 才复用），
    而不是靠"看起来是空的"来判断——那是无证据的猜测。
    """
    own_client = client is None
    qdrant = client or build_client()
    # ---- 第一步：**先持久化占号**（独立事务，提交后才去动远端）----
    #
    # 审查意见（21:15 复测第 6 条）指出的真实风险：如果"推进计数器"和"在远端建
    # collection"放在同一个事务里，远端建好之后一旦超时/回滚，编号就被回滚了——
    # 下一次 prepare 会**又分配同一个编号**，于是新数据被写进那个已经存在、
    # 但没人登记过的孤立 collection 里。
    #
    # 占号与远端建集合是两个不同存储上的动作，本来就无法真正原子。
    # 正确做法是：先把编号**落库**（这一步成功即不可回退），再去建远端；
    # 远端失败就把这行标成 failed，编号作废、**永不复用**，下次分配新编号。
    with transaction.atomic():
        locked = KnowledgeLibrary.objects.select_for_update().get(pk=library.pk)
        previous_generation = locked.active_generation
        previous_collection = (
            collection_name(locked.pk, previous_generation) if previous_generation else ""
        )
        generation = (
            max(
                int(locked.next_generation or 0),
                _highest_registered_generation(locked),
                int(previous_generation or 0),
            )
            + 1
        )
        name = active_collection_name(locked.pk, generation)
        if VectorCollection.objects.filter(library=locked, name=name).exists():
            # 唯一约束也会兜住；这里提前报错，信息更清楚
            raise VectorIndexError(f"generation 分配冲突：{name} 已经被登记过，拒绝复用")

        # **孤立集合检测（编号复用的最后一道防线）**
        #
        # 场景：某次分配已经"占号 + 建好远端集合"，但那次占号所在的数据库事务
        # 被回滚了（例如调用方自己包了一层 outer atomic，把我们的占号变成
        # savepoint 后整体回滚）。于是数据库里查不到这条登记，编号也退回去了，
        # 远端却**实际存在**一个同名集合。
        #
        # 如果不检测，下次分配会**又拿到同一个编号**，把新数据写进那个没人登记
        # 的孤立集合——这正是本函数文档里要避免的事。
        #
        # 所以：远端已存在但未登记 → 认定它是孤立集合，**登记为 failed 留痕**，
        # 并跳到下一个编号。这样"编号绝不复用"在任何回滚情形下都成立。
        for _attempt in range(8):
            if not collection_exists(qdrant, name):
                break
            VectorCollection.objects.create(
                name=name,
                dimension=int(profile.dimension),
                embedding_model=profile.model,
                model_version=profile.model_version,
                profile=profile.name,
                base_url=getattr(profile, "base_url", "") or "",
                library=locked,
                storage_mode=qdrant_mode(),
                storage_path=str(
                    qdrant_server_dir() if qdrant_mode() == "server" else qdrant_embedded_dir()
                ),
                is_active=False,
                build_status="failed",
                build_error=(
                    "orphan_remote_collection：远端已存在同名集合但没有登记行"
                    "（上一次占号被回滚），本编号作废、不复用"
                )[:400],
            )
            logger.warning("检测到孤立集合 %s（未登记），编号作废并跳过", name)
            generation += 1
            name = active_collection_name(locked.pk, generation)
            if VectorCollection.objects.filter(library=locked, name=name).exists():
                raise VectorIndexError(f"generation 分配冲突：{name} 已经被登记过，拒绝复用")
        else:  # pragma: no cover - 连续 8 个孤立集合，说明环境异常
            raise VectorIndexError(
                f"连续 {8} 个候选 collection 都是孤立集合，拒绝继续分配（请人工检查远端）"
            )

        KnowledgeLibrary.objects.filter(pk=locked.pk).update(next_generation=generation)
        registry = VectorCollection.objects.create(
            name=name,
            dimension=int(profile.dimension),
            embedding_model=profile.model,
            model_version=profile.model_version,
            profile=profile.name,
            base_url=getattr(profile, "base_url", "") or "",
            library=locked,
            storage_mode=qdrant_mode(),
            storage_path=str(
                qdrant_server_dir() if qdrant_mode() == "server" else qdrant_embedded_dir()
            ),
            is_active=False,
            # 远端还没建好，先标 building：这个状态下**不允许被切换**
            build_status="building",
        )

    # ---- 第二步：在远端建 collection（失败只影响本次，编号已作废）----
    try:
        ensure_collection(qdrant, name, int(profile.dimension))
        ensure_text_index(qdrant, name)
    except Exception as exc:  # noqa: BLE001 - 任何远端失败都要留痕并作废编号
        VectorCollection.objects.filter(pk=registry.pk).update(
            build_status="failed", build_error=f"{type(exc).__name__}: {exc}"[:400]
        )
        logger.warning("为 %s 建远端 collection 失败，编号 %s 作废：%s", name, generation, exc)
        raise
    finally:
        if own_client:
            qdrant.close()

    VectorCollection.objects.filter(pk=registry.pk).update(build_status="ready")
    return GenerationPlan(
        library_id=locked.pk,
        generation=generation,
        collection=name,
        dimension=int(profile.dimension),
        model=profile.model,
        model_version=profile.model_version,
        previous_generation=previous_generation,
        previous_collection=previous_collection,
        created=True,
    )


def validate_activation(
    library: KnowledgeLibrary,
    plan: GenerationPlan,
    *,
    client=None,
    verify_target_coverage: bool = True,
) -> None:
    """激活前的完整校验（审查意见 5 的后半段）。**没有"跳过校验"的开关。**

    必须全部通过才允许把新索引切上线：

    1. plan 属于这个库；
    2. 该 generation 已登记，登记维度/模型与 plan 一致（不能漂移）；
    3. Qdrant 里 collection 真实存在，且它**自己声明的维度**与 plan 一致；
    4. ``verify_target_coverage``（前向切换）：库里每一份未归档资料都已经把向量
       写进**这个** generation，且 coverage 达标（``embedded_chunk_count >= chunk_count``）。
       只看 file 上的 ``indexed`` 布尔是不够的，那只是我们自己写的标记；
    5. 反向回退时（``verify_target_coverage=False``）：不要求"指回目标代次"——
       回退不重嵌入，资料的向量本来就在目标 collection 里。这一路仍然严格要求
       **每一份资料都指着一个已登记、且与它自己 coverage 相符的 collection**，
       而不是放宽成什么都不查。

    注意这两种口径都**不是**"关掉校验"：第 1/2/3/5 条在两条路径上都生效。
    """
    if plan.library_id != library.pk:
        raise VectorIndexError(
            f"plan 属于库 {plan.library_id}，不能用在库 {library.pk} 上"
        )
    registered = VectorCollection.objects.filter(
        library=library, name=plan.collection
    ).first()
    if registered is None:
        raise VectorIndexError(
            f"collection {plan.collection} 没有登记记录，拒绝激活（不能把没登记的索引切上线）"
        )
    # **退役待删**的代次绝不能切上线：它已经被判定要删（只是远端还没删掉/删失败），
    # 手里可能正在被别人 drop，或者是上一个失败清理的残留。允许激活它就会把
    # "正在删除的索引"变成线上索引（独立审查第 2 项：退役状态必须被 activate/
    # rollback 明确拒绝）。
    if registered.retiring_at is not None:
        raise VectorIndexError(
            f"collection {plan.collection} 已标记退役待删"
            f"（{registered.retiring_at.isoformat()}），拒绝激活；"
            "请等待清理完成或重新执行重建"
        )
    # 只认构建完成（ready）的代次：building 表示"占号了但远端未必建好"，
    # failed 表示"这次构建已经作废、编号不再复用"。把这两种切上线会把
    # 一个空/半成品索引变成线上索引，用户看到的就是"什么都搜不到"。
    if registered.build_status != "ready":
        raise VectorIndexError(
            f"collection {plan.collection} 的构建状态是 {registered.build_status}"
            f"（{registered.build_error or '无详情'}），拒绝激活；请重新执行重建"
        )
    if int(registered.dimension) != int(plan.dimension):
        raise DimensionMismatch(
            f"登记维度 {registered.dimension} ≠ plan 维度 {plan.dimension}，拒绝激活"
        )
    if registered.embedding_model and plan.model and registered.embedding_model != plan.model:
        raise VectorIndexError(
            f"登记模型 {registered.embedding_model} ≠ plan 模型 {plan.model}，拒绝激活"
        )
    owns_client = client is None
    qdrant = client or build_client()
    try:
        if not collection_exists(qdrant, plan.collection):
            raise CollectionNotFound(
                f"Qdrant 里找不到 collection {plan.collection}，拒绝激活"
            )
        # 真实向量维度：collection 自己声明的维度必须与 plan 一致。
        # 只信登记行是不够的（登记行可能被人为改过，或者库被重建过）。
        info = qdrant.get_collection(plan.collection)
        actual_dimension = _vector_size(info)
        if actual_dimension is not None and int(actual_dimension) != int(plan.dimension):
            raise DimensionMismatch(
                f"Qdrant 里 {plan.collection} 实际是 {actual_dimension} 维，"
                f"plan 声明 {plan.dimension} 维；拒绝激活"
            )

        pending = IngestedFile._base_manager.filter(library=library).exclude(
            parse_status=ParseStatus.ARCHIVED
        )
        # **空库不准激活**：一个资料都没有的索引上线毫无意义，而且会把
        # "这个库还没建索引"和"建好了但是空的"混成同一个状态，后面排查会很痛苦。
        if not pending:
            raise IncompleteIndex(
                f"库 {library.code} 里没有任何未归档资料，空索引不准激活；"
                "请先上传资料再重建（首上传前不需要建索引）"
            )
        # **以目标 collection 里的真实 points 验证 coverage**（审查方要求）。
        #
        # 为什么不用 file 上的指针/布尔：同一份资料可以同时存在于 g1 与 g2，
        # 而 file 只有一个"当前指针"字段，它**无法证明某一代是否完整**；
        # 回退时更会被"当前指向 g2"直接挡死。
        # 真正的事实只有一处：目标 collection 里是不是有每一段的向量点。
        # 点 id 是稳定派生的（``point_id_for(file, ordinal)``），所以可以直接核对。
        # 前向重建与旧代回退用的是**同一套**核验。
        _verify_collection_coverage(
            qdrant,
            plan.collection,
            list(pending),
            expected_model=registered.embedding_model,
            expected_model_version=registered.model_version,
        )
        # 回退路径额外确认：资料的向量没有指向未登记的 collection
        # （否则检索会静默空结果）。两条路径都做，不放松任何一条。
        registered_names = set(
            VectorCollection.objects.filter(library=library).values_list("name", flat=True)
        )
        orphans = [
            record.original_name
            for record in pending
            if record.vector_collection_name and record.vector_collection_name not in registered_names
        ]
        if orphans:
            raise VectorIndexError(
                f"有 {len(orphans)} 份资料的向量指向未登记的 collection"
                f"（例如 {orphans[:3]}），拒绝切换"
            )
    finally:
        if owns_client:
            qdrant.close()


def _verify_collection_coverage(
    qdrant,
    collection: str,
    records: list[IngestedFile],
    *,
    expected_model: str = "",
    expected_model_version: str = "",
) -> int:
    """逐段核对 collection 里的点：id 存在、且 payload 与库里分段**逐字段一致**。

    核对内容（审查方要求，缺一不可）：

    - ``file_id`` / ``ordinal``：点确实属于这条资料的这一段；
    - ``text``：正文没有被换过；
    - ``content_sha256``：原始字节没有被换过（防止"内容变了但点没重建"）；
    - ``library_id``：跨库串点会直接导致越权召回；
    - ``model`` / ``model_version``：防止**旧模型**的点混进新一代索引
      （换模型时最容易发生，而且不会报错，只会静默降低检索质量）。

    取点方式是稳定 id 直接 retrieve（batch），不依赖 file 当前指针，
    所以**前向重建与旧代回退用的是同一套核验**。
    """
    missing: list[str] = []
    mismatched: list[str] = []
    unchecked: list[str] = []
    checked = 0
    for record in records:
        chunks = list(record.chunks.order_by("ordinal"))
        if not chunks:
            # 库里有一份**没有分段**的未归档资料：不能当成"这一代自然没有它"跳过。
            # 跳过等于"含失败资料的库可以被空激活"——审查方明确要求不允许。
            unchecked.append(f"{record.original_name}(未解析出分段)")
            continue
        ids = [point_id_for(record, chunk.ordinal) for chunk in chunks]
        found: dict[str, Any] = {}
        for start in range(0, len(ids), COVERAGE_BATCH):
            batch = ids[start : start + COVERAGE_BATCH]
            points = qdrant.retrieve(
                collection_name=collection, ids=batch, with_payload=True, with_vectors=False
            )
            for point in points or []:
                found[str(point.id)] = point
        for chunk in chunks:
            point_id = point_id_for(record, chunk.ordinal)
            point = found.get(point_id)
            if point is None:
                missing.append(f"{record.original_name}#{chunk.ordinal}")
                continue
            payload = dict(getattr(point, "payload", None) or {})
            if payload.get("file_id") != record.pk or payload.get("ordinal") != chunk.ordinal:
                mismatched.append(f"{record.original_name}#{chunk.ordinal}(归属/序号不符)")
                continue
            if payload.get("text") != chunk.text:
                mismatched.append(f"{record.original_name}#{chunk.ordinal}(正文不符)")
                continue
            if payload.get("library_id") != record.library_id:
                mismatched.append(f"{record.original_name}#{chunk.ordinal}(库归属不符)")
                continue
            if payload.get("content_sha256") != record.content_sha256:
                mismatched.append(f"{record.original_name}#{chunk.ordinal}(原始字节指纹不符)")
                continue
            if expected_model and payload.get("model") != expected_model:
                mismatched.append(
                    f"{record.original_name}#{chunk.ordinal}"
                    f"(模型不符：{payload.get('model')!r} ≠ {expected_model!r})"
                )
                continue
            if expected_model_version and payload.get("model_version") != expected_model_version:
                mismatched.append(
                    f"{record.original_name}#{chunk.ordinal}"
                    f"(模型版本不符：{payload.get('model_version')!r} "
                    f"≠ {expected_model_version!r})"
                )
                continue
            checked += 1
    if missing or mismatched or unchecked:
        raise IncompleteIndex(
            f"{collection} 覆盖不完整：缺 {len(missing)} 段"
            f"（例如 {missing[:3]}），内容不符 {len(mismatched)} 段"
            f"（例如 {mismatched[:3]}），未解析 {len(unchecked)} 份"
            f"（例如 {unchecked[:3]}）；拒绝切换"
        )
    return checked


def activate_generation(
    library: KnowledgeLibrary,
    plan: GenerationPlan,
    *,
    client=None,
    freeze_token: str | None = None,
) -> KnowledgeLibrary:
    """把 plan 指到线上（原子切换指针）。

    ## 闸门纪律（审查 P1：外部激活/回退能闯进别人的重建窗口）

    ``freeze_token`` 是**仅供内部使用**的参数，语义只有两种：

    * ``None``（**默认**：HTTP 受控激活、planner 受控意图、``rollback_generation``
      等一切外部调用）→ 本库必须**可写**。冻结中直接抛
      :class:`~apps.knowledge.writegate.WriteFrozen`（上层映射成 409），
      绝不接受调用方自报的令牌。
    * 给了字符串 → 只允许"**正在重建的那个 job**"在**自己持有的冻结窗口**内发布：
      必须在**同一个事务**里校验"仍处于冻结 **且** 令牌与当前 owner 一致"。

    **检查与发布必须持同一把 gate 行锁**（不能只在事务前检查一次）：
    两个助手（``assert_writable_locked`` / ``assert_freeze_owner``）都在**当前外层
    事务**里对闸门行做 ``select_for_update``，锁一直持有到本事务提交，因此
    ``validate_activation`` 与指针写入全程都在闸门保护之下；外部激活也无法
    "趁校验的间隙"闯进来。

    锁序固定 **gate → library**（与所有写路径一致），避免交叉死锁。
    """
    with transaction.atomic():
        # **先把闸门锁住并校验所有权**，再锁库行、再校验、最后切换（顺序很重要）：
        # 如果在事务外校验，两个并发切换可能都通过校验，然后一个接一个把指针
        # 切到不同的 collection，第二个就把第一个刚切好的状态覆盖掉。
        # 校验放在锁里，才能保证"我看到的那份资料状态"在整个切换期间不会变。
        from .writegate import assert_freeze_owner, assert_writable_locked

        if freeze_token is None:
            # 普通（外部）激活：冻结期一律拒绝 —— 重建窗口内只有重建自己能动指针
            assert_writable_locked(
                library.pk, action=f"激活/切换代次 {plan.collection}"
            )
        else:
            # 重建内部发布：必须仍是当前冻结的所有者（令牌未被旋转、冻结未被解除）
            assert_freeze_owner(
                library.pk, freeze_token, action=f"发布代次切换 {plan.collection}"
            )
        locked = KnowledgeLibrary.objects.select_for_update().get(pk=library.pk)
        validate_activation(locked, plan, client=client)
        # 幂等保护：同一事务里两次切换同一个 plan 不应该把已完成资料"退回未索引"
        VectorCollection.objects.filter(library=locked).update(is_active=False)
        VectorCollection.objects.filter(library=locked, name=plan.collection).update(is_active=True)
        locked.active_generation = plan.generation
        locked.vector_collection = plan.collection
        # 库表不是 append-only 模型，但这里只改"当前生效指针"，不改任何已入库内容
        KnowledgeLibrary.objects.filter(pk=locked.pk).update(
            active_generation=plan.generation,
            vector_collection=plan.collection,
            next_generation=max(int(locked.next_generation or 0), int(plan.generation)),
        )
        # 只把**还没指向目标 collection** 的资料改过去，避免把
        # "这份资料的向量到底在哪个 collection" 这个事实抹平。
        # 抹平的后果很实际：回退后所有资料都记成目标 collection，
        # 再想切回原来的新 generation 就没人知道向量其实在那里了。
        IngestedFile._base_manager.filter(library=locked).exclude(
            vector_collection_name=plan.collection
        ).update(
            vector_collection_name=plan.collection,
            vector_generation=plan.generation,
            vector_collection=plan.collection,
        )
        # 指针切换时把"已完成计数"对齐到该资料的分段数：
        # 校验刚刚已经在目标 collection 里逐段确认过点存在，
        # 所以这里写的是**被验证过的事实**，不是乐观值。
        for record in IngestedFile._base_manager.filter(library=locked):
            total = record.chunks.count()
            if total:
                IngestedFile._base_manager.filter(pk=record.pk).update(
                    chunk_count=total, embedded_chunk_count=total, vector_status="indexed"
                )
    locked.refresh_from_db()
    # 调用方手里那个实例也要跟上：否则紧接着的 rollback 会拿旧指针算"另一个版本"，
    # 回退目标算错（甚至回退到自己）。
    library.active_generation = locked.active_generation
    library.vector_collection = locked.vector_collection
    return locked


def rollback_generation(
    library: KnowledgeLibrary,
    *,
    to_generation: int | None = None,
    client=None,
) -> KnowledgeLibrary:
    """回退到指定 generation（默认回退到"登记表里最近的一个非当前版本"）。

    旧 collection 与旧登记行都还在，所以回退只是把指针切回去——**不需要重嵌入**，
    这也是"旧索引保留可回退"这条要求的实际兑现方式。

    **回退同样不许闯进重建窗口**（审查 P1）：它走 :func:`activate_generation` 的
    默认路径（``freeze_token=None``），因此冻结中会抛
    :class:`~apps.knowledge.writegate.WriteFrozen`（HTTP 409）。
    刻意**不提供**"传令牌强行回退"的参数：能自己签发令牌就等于没有闸门。

    维度/模型**从登记行读真实值**，不再写 ``dimension=0``（审查方指出）：
    一旦写 0，后续的 ``validate_activation`` 就会拿 0 去跟真实 collection 比，
    把一次正常的回退判成"维度不符"，或者更糟——让一个假维度进了库。
    """
    if to_generation is not None:
        target_name = active_collection_name(library.pk, to_generation)
        target_generation = to_generation
        # 指定回退目标时也要确认它真的存在：否则就是把一个空 collection
        # 当成"旧版本"切上去，检索会静默返回空结果——比报错危险得多。
        target_row = VectorCollection.objects.filter(
            library=library, name=target_name
        ).first()
        if target_row is None:
            raise VectorIndexError(
                f"找不到可回退的 generation {to_generation}（collection {target_name} "
                "没有登记记录）"
            )
        # 退役待删的代次不能被回退选中（它正是要被删掉的那一个）
        if target_row.retiring_at is not None:
            raise VectorIndexError(
                f"collection {target_name} 已标记退役待删"
                f"（{target_row.retiring_at.isoformat()}），拒绝回退到它"
            )
    else:
        candidates = (
            VectorCollection.objects.filter(library=library, retiring_at__isnull=True)
            .exclude(name=library.vector_collection)
            .order_by("-id")
            .first()
        )
        if candidates is None:
            raise VectorIndexError("没有可回退的旧 generation")
        target_name = candidates.name
        target_generation = _generation_of(library, target_name)
    if target_name == library.vector_collection:
        raise VectorIndexError(f"目标 collection {target_name} 就是当前生效版本，无需回退")
    registered = VectorCollection.objects.filter(library=library, name=target_name).first()
    if registered is None:  # pragma: no cover - 上面已查过
        raise VectorIndexError(f"collection {target_name} 没有登记记录，不能回退过去")
    plan = GenerationPlan(
        library_id=library.pk,
        generation=target_generation,
        collection=target_name,
        # 用登记行里的**真实**维度与模型，不写 0/空串
        dimension=int(registered.dimension),
        model=registered.embedding_model,
        model_version=registered.model_version,
        previous_generation=library.active_generation,
        previous_collection=library.vector_collection,
        created=False,
    )
    return activate_generation(library, plan, client=client)


def _generation_of(library: KnowledgeLibrary, name: str) -> int:
    prefix = f"{collection_prefix()}-{library.pk}-g"
    if name.startswith(prefix):
        try:
            return int(name[len(prefix) :])
        except ValueError:
            pass
    return 0


def indexed_collections(library: KnowledgeLibrary) -> list[str]:
    """该库历史上所有 collection（含旧版本），用于巡检与回退。"""
    return list(
        VectorCollection.objects.filter(library=library).order_by("id").values_list("name", flat=True)
    )


# ------------------------------------------------------------------ 检索 ----


@dataclass
class SearchCandidate:
    """一路召回命中的一条候选。"""

    point_id: str
    score: float
    collection: str
    payload: dict[str, Any] = field(default_factory=dict)


def search_dense(
    client,
    *,
    collections: Sequence[str],
    vector: list[float],
    limit: int,
    permission_codes: Sequence[str] | None = None,
    file_ids: Sequence[int] | None = None,
) -> list[SearchCandidate]:
    """稠密向量检索。

    ``permission_codes=None`` 表示不受限（系统管理员）。
    ``file_ids`` 是结构化条件收敛出的**候选文件集合**：在向量查询阶段就限制，
    而不是把不合条件的点取回来再过滤（那样会挤占 top_k，等于把名额浪费掉）。
    ``None`` 表示不限制。
    """
    from qdrant_client.models import FieldCondition, Filter, MatchAny

    must: list[Any] = []
    if permission_codes is not None:
        if not permission_codes:
            return []
        must.append(
            FieldCondition(key="permission_code", match=MatchAny(any=list(permission_codes)))
        )
    if file_ids is not None:
        if not file_ids:
            return []
        must.append(FieldCondition(key="file_id", match=MatchAny(any=list(file_ids))))
    query_filter = Filter(must=must) if must else None
    hits: list[SearchCandidate] = []
    for name in collections:
        if not collection_exists(client, name):
            continue
        response = client.query_points(
            collection_name=name,
            query=vector,
            limit=limit,
            query_filter=query_filter,
            with_payload=True,
        )
        for point in getattr(response, "points", []) or []:
            hits.append(
                SearchCandidate(
                    point_id=str(point.id),
                    score=float(point.score or 0.0),
                    collection=name,
                    payload=dict(point.payload or {}),
                )
            )
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:limit]


def search_sparse(
    client,
    *,
    collections: Sequence[str],
    text: str,
    limit: int,
    permission_codes: Sequence[str] | None = None,
    file_ids: Sequence[int] | None = None,
) -> list[SearchCandidate]:
    """关键词检索（Qdrant 全文索引）。

    ``file_ids`` 同样在查询阶段限制候选集合（``None`` = 不限制）。
    """
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchText

    if not text.strip():
        return []
    must: list[Any] = [FieldCondition(key="text", match=MatchText(text=text))]
    if file_ids is not None:
        if not file_ids:
            return []
        must.append(FieldCondition(key="file_id", match=MatchAny(any=list(file_ids))))
    if permission_codes is not None:
        if not permission_codes:
            return []
        must.append(
            FieldCondition(key="permission_code", match=MatchAny(any=list(permission_codes)))
        )
    query_filter = Filter(must=must)
    hits: list[SearchCandidate] = []
    for name in collections:
        if not collection_exists(client, name):
            continue

        def _scroll():
            return client.scroll(
                collection_name=name,
                scroll_filter=query_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )

        try:
            points, _ = _scroll()
        except Exception:
            # 老 collection 可能没建 ``text`` 全文索引（ensure_text_index 是后加的、
            # 只在 prepare_generation 里调用），此时 MatchText 会直接报错，稀疏这一路
            # 就永远是 0 条（实测基线：sparse_hits 恒为 0）。补建索引后重试一次；
            # 仍失败就把异常抛给 pipeline，由它记"该库稀疏不可用"并退回 DB 关键词兜底。
            ensure_text_index(client, name)
            points, _ = _scroll()
        for point in points:
            hits.append(
                SearchCandidate(
                    point_id=str(point.id),
                    score=0.0,
                    collection=name,
                    payload=dict(point.payload or {}),
                )
            )
    return hits[:limit]


#: 融合对齐所必需的"来源身份"字段。**缺任一字段就不合并** ——
#: 宁可不去重，也绝不能把不同库/不同文件/不同分段错误合并（那会丢结果或算错分）。
_FUSION_IDENTITY_FIELDS = ("file_id", "ordinal", "library_id", "content_sha256")


def fusion_identity(candidate: SearchCandidate) -> str:
    """多路召回对齐用的**可信来源身份**。

    为什么不能直接用 `collection:point_id` 当 key（真实缺陷）：
      同一条资料分段会被两路召回各命中一次 ——
        * Qdrant 向量召回：`point_id = <collection>:<chunk>`
        * DB 关键词兜底：`point_id = "db:<chunk_pk>"`
      两者 key 不同，于是**同一来源被算成两条**，在结果里重复出现
      （浏览器实测："沧海716" 同一片段在第 1/2 条重复，score 还都是 0.992）。

    身份定义：`library_id + file_id + ordinal + content_sha256`（+ generation 若有）。
      * 含 `library_id`：**不同库的同名/同分段内容不会被合并**；
      * 含 `file_id`：**不同文件不会被合并**；
      * 含 `ordinal`：**同一文件的不同分段不会被合并**；
      * 含 `content_sha256`：内容不同即身份不同（换代次重建后仍能正确对齐）。
    缺字段 → 回退到原 `collection:point_id`（保守，不做激进合并）。
    """
    payload = candidate.payload or {}
    if any(payload.get(field) is None for field in _FUSION_IDENTITY_FIELDS):
        return f"{candidate.collection}:{candidate.point_id}"
    if not str(payload.get("content_sha256")).strip():
        return f"{candidate.collection}:{candidate.point_id}"
    generation = payload.get("generation")
    generation_part = "-" if generation is None else str(generation)
    # **不能用 generation 参与身份**（独立复现的真实重复）：
    # 稠密路的 payload 带 `generation=2`，而 DB 关键词兜底路的 payload 里根本没有
    # 这个字段 → 同一条分段算成两个身份（`…:<sha>:2` 与 `…:<sha>:-`），
    # 于是同一份资料的同一段在结果里出现两次（浏览器实测：3 份资料各出现两次、
    # 分数还相近 0.223/0.218）。
    # 为什么去掉它是安全的：一次检索只查**当前生效的**那一个 collection，稠密/稀疏
    # 与 DB 兜底命中的必然是同一代次；跨代次去重本来就不该在这里做
    # （`_drop_tombstoned` 与库指针负责那件事）。因此身份 = 库+文件+段号+内容哈希。
    del generation_part  # 保留变量只为兼容旧调试输出，不参与身份
    return (
        f"src:{payload['library_id']}:{payload['file_id']}:{payload['ordinal']}:"
        f"{payload['content_sha256']}"
    )


def rrf_fuse(
    ranked_lists: Iterable[Sequence[SearchCandidate]], *, k: int = 60, limit: int = 30
) -> list[SearchCandidate]:
    """Reciprocal Rank Fusion：把多路召回合成一路。

    为什么用 RRF 而不是加权求和：稠密分数（余弦）与稀疏召回没有可比刻度，
    权重只能靠调参硬凑；RRF 只用名次，跨路融合稳定且无需调参。

    两处必须的去重（都由真实缺陷驱动）：
      1. **路内去重**：同一召回列表里若出现同一来源的重复项，
         它们不得各自加一次分（否则该来源被凭空抬高名次）；
      2. **跨路对齐**：按 `fusion_identity` 对齐 —— 向量路与 DB 关键词路命中
         同一分段时只算一条，并在结果里只出现一次。
    """
    scores: dict[str, float] = {}
    best: dict[str, SearchCandidate] = {}
    for ranked in ranked_lists:
        seen_in_list: set[str] = set()
        rank = 0
        for candidate in ranked:
            key = fusion_identity(candidate)
            if key in seen_in_list:
                # 路内重复：**既不重复加分，也不占用名次**
                continue
            seen_in_list.add(key)
            rank += 1
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            best.setdefault(key, candidate)
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
    fused: list[SearchCandidate] = []
    for key, score in ordered:
        candidate = best[key]
        fused.append(
            SearchCandidate(
                point_id=candidate.point_id,
                score=score,
                collection=candidate.collection,
                payload=candidate.payload,
            )
        )
    return fused


__all__ = [
    "CollectionNotFound",
    "DimensionMismatch",
    "GenerationPlan",
    "SearchCandidate",
    "VectorIndexError",
    "VectorPoint",
    "activate_generation",
    "build_client",
    "collection_exists",
    "collection_name",
    "count_points",
    "delete_points",
    "ensure_collection",
    "ensure_text_index",
    "indexed_collections",
    "logical_collection_name",
    "point_id_for",
    "prepare_generation",
    "qdrant_mode",
    "qdrant_url",
    "rollback_generation",
    "rrf_fuse",
    "search_dense",
    "search_sparse",
    "upsert_points",
]
