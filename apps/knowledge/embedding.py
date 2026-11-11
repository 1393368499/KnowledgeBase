"""嵌入模型 profile：维度、模型名、端点全部可配置，并禁止"伪造维度"。

用户要求（2026-10 新增）
-----------------------
1. 以后会换成 2048 维的模型，**先兼容当前的 1024 维模型**——所以现在 active
   profile 必须是 ``bge1024``，2048 的 profile **预配置但不激活**。
2. 维度 / 模型名 / 端点必须配置化，不能写死在代码里。
3. collection 要带模型版本与维度。
4. 换模型必须：新建 collection → 全量重嵌入 → 原子切换；旧索引保留可回退。
5. **绝不能把 1024 维补零、截断，或者对外声称它是 2048 维。**
6. 模型返回的维度不匹配必须显式拒绝，且不能污染索引。

本模块只负责 1/2/5/6：profile 定义、维度裁决、真实维度探测。真正的 collection
重建与切换在 :mod:`apps.knowledge.vectorindex` 里。

关于"就绪"的口径
-----------------
``ProfileInfo.ready`` **不是**手填的声明，而是由 :func:`probe_dimension` 真实调用
嵌入服务逐条判定：只有实测维度 == 声明维度才算就绪。2048 的 profile 在服务部署
并实测通过之前恒为未就绪，任何嵌入请求都会被显式拒绝——不会静默降级。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

import httpx
from django.conf import settings

from .httpclient import build_http_client
from .resilience import (
    CircuitBreaker,
    breaker_for,
    call_with_resilience,
)

#: 当前可用的默认 profile 名（1024 维，已实测）
DEFAULT_PROFILE = "bge1024"

#: 允许的最大嵌入维度：超过它的配置一律显式拒绝（防止非法值被写进 collection 维度）
MAX_EMBED_DIMENSION = 8192


def coerce_dimension(value: Any, *, where: str) -> int:
    """把配置/登记里的维度解析成合法正整数；**非法值显式报错，不静默兜底**。"""
    if isinstance(value, bool):  # bool 是 int 子类，必须先挡掉
        raise EmbeddingError(f"{where} 的维度不能是布尔值：{value!r}")
    if isinstance(value, int):
        dim = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise EmbeddingError(f"{where} 的维度是空字符串")
        try:
            dim = int(text)
        except ValueError as exc:
            raise EmbeddingError(f"{where} 的维度不是整数：{value!r}") from exc
    else:
        raise EmbeddingError(f"{where} 的维度类型非法：{type(value).__name__}")
    if dim < 1:
        raise EmbeddingError(f"{where} 的维度必须 ≥ 1，收到 {dim}")
    if dim > MAX_EMBED_DIMENSION:
        raise EmbeddingError(
            f"{where} 的维度 {dim} 超过允许上限 {MAX_EMBED_DIMENSION}；"
            "确需更大维度请先在白名单里显式放开"
        )
    return dim


def coerce_text(value: Any, *, where: str) -> str:
    """把配置字段解析成字符串；``None`` 视为空串，非字符串显式报错。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EmbeddingError(f"{where} 必须是字符串，收到 {type(value).__name__}")
    return value


class EmbeddingError(RuntimeError):
    """嵌入相关的显式失败（维度不匹配、profile 未就绪、服务不可达）。"""


class _RetryableHttpError(EmbeddingError):
    """网络层失败：可以重试（连接被拒、超时、读失败）。"""

    retryable = True


class _ServiceHttpError(EmbeddingError):
    """服务端返回了错误状态码；是否重试看状态码。"""

    def __init__(self, message: str, *, http_status: int) -> None:
        super().__init__(message)
        self.http_status = http_status


def _post_json_with_resilience(
    *,
    http: httpx.Client,
    url: str,
    payload: dict[str, Any],
    service: str,
    breaker: CircuitBreaker,
    max_attempts: int,
) -> dict[str, Any]:
    """POST 一个 OpenAI 兼容接口，带熔断与退避重试。"""

    def once() -> dict[str, Any]:
        try:
            response = http.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise _RetryableHttpError(f"请求超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise _RetryableHttpError(f"网络错误：{exc}") from exc
        if response.status_code != 200:
            raise _ServiceHttpError(
                f"服务返回 {response.status_code}：{response.text[:300]}",
                http_status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise EmbeddingError(f"响应不是合法 JSON：{response.text[:200]}") from exc

    return call_with_resilience(
        service=service,
        operation="post",
        func=once,
        breaker=breaker,
        max_attempts=max_attempts,
    )


class DimensionMismatch(EmbeddingError):
    """服务返回的向量维度与 profile 声明不一致。

    单独成类是为了让上层能精确捕获并回复"拒绝"而不是"500"，也方便测试断言。
    """

    def __init__(self, message: str, *, expected: int, actual: int) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class ProfileInfo:
    """一个嵌入模型 profile 的完整描述。"""

    name: str
    model: str
    model_version: str
    base_url: str
    dimension: int
    note: str = ""

    @property
    def configured(self) -> bool:
        """是否已配置（有模型名与端点）。

        注意这与"就绪"是两件事：configured 只说"配置齐了"，**就绪必须实测**
        （见 :func:`probe_dimension` / :func:`assert_profile_ready`）。写索引前的
        硬闸门是后者——把一个实测 1024 维的模型声明成 2048 维，会在这里被挡下。
        """
        return bool(self.model.strip()) and bool(self.base_url.strip())

    @property
    def collection_suffix(self) -> str:
        """collection 名里的模型标识段：模型名 + 版本 + 维度。"""
        slug = "".join(ch if ch.isalnum() else "_" for ch in self.model).strip("_")
        version = "".join(ch if ch.isalnum() else "_" for ch in self.model_version).strip("_")
        return f"{slug}_{version}_{self.dimension}"

    def to_public(self, *, ready: bool) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "model_version": self.model_version,
            "base_url": self.base_url,
            "dimension": self.dimension,
            "ready": ready,
            "note": self.note,
        }


#: 内置 profile。**所有字段都能被环境变量覆盖**，这里只是默认值。
#: 注意 ``bge2048`` 的 ``note`` 写明了"未部署"，不要把它当成可用 profile 宣传。
BUILTIN_PROFILES: dict[str, ProfileInfo] = {
    "bge1024": ProfileInfo(
        name="bge1024",
        model="bge-large-zh-v1.5",
        model_version="v1.5",
        base_url="http://172.16.100.114:8000/v1",
        dimension=1024,
        note="当前线上模型，实测 1024 维；兼容保留。",
    ),
    "bge2048": ProfileInfo(
        name="bge2048",
        model="",  # 新模型尚未部署，模型名/端点留空，由环境变量注入
        model_version="target",
        base_url="",
        dimension=2048,
        note="目标 2048 维模型，尚未部署；未就绪时任何嵌入请求都会被拒绝。",
    ),
}


def _env_profiles_raw() -> dict[str, Any]:
    """解析 ``KB_EMBED_PROFILES``（对象：profile 名 → 字段覆盖）。"""
    raw = os.environ.get("KB_EMBED_PROFILES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - 配置错误
        raise EmbeddingError(f"KB_EMBED_PROFILES 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise EmbeddingError("KB_EMBED_PROFILES 必须是对象（profile 名 → 覆盖字段）")
    return parsed


def _env_profile_overrides(name: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """取某个 profile 的覆盖值；**未知 profile 名由 :func:`profiles` 显式拒绝**。

    为什么要拒绝未知名字：在 ``KB_EMBED_PROFILES`` 里写一个新名字（例如 ``bge3072``）
    却毫无效果时，运维会以为"已经支持了"。宁可报错，也不能静默忽略——静默忽略就是配置漂移。
    """
    parsed = _env_profiles_raw() if raw is None else raw
    if name not in parsed:
        return {}
    entry = parsed[name]
    if not isinstance(entry, dict):
        raise EmbeddingError(f"KB_EMBED_PROFILES.{name} 必须是对象")
    return entry


@lru_cache(maxsize=1)
def profiles() -> dict[str, ProfileInfo]:
    """合并内置 profile、环境变量覆盖与 settings，返回全部 profile。

    **严格性**（对应"模型配置白名单"要求）：未知 profile 名、未知字段、非法维度/类型
    一律**显式报错**；"未配置"（model/base_url 为空）是合法状态，但**不等于就绪**。
    """
    merged: dict[str, ProfileInfo] = dict(BUILTIN_PROFILES)
    overrides = _env_profiles_raw()
    unknown_profiles = sorted(set(overrides) - set(merged))
    if unknown_profiles:
        raise EmbeddingError(
            f"KB_EMBED_PROFILES 含未登记的 profile 名：{unknown_profiles}；可用：{sorted(merged)}。"
            "新增模型必须先在内置 profile 白名单里登记，再覆盖字段（否则配置会被静默忽略）"
        )

    # settings.MODEL_SERVICES["embedding"] 是历史入口，继续兼容
    embed_conf = getattr(settings, "MODEL_SERVICES", {}).get("embedding", {}) or {}
    active_defaults = {
        "model": coerce_text(
            embed_conf.get("model", ""), where="MODEL_SERVICES.embedding.model"
        ),
        "base_url": coerce_text(
            embed_conf.get("base_url", ""), where="MODEL_SERVICES.embedding.base_url"
        ),
        "dimension": coerce_dimension(
            embed_conf.get("dimension", 1024), where="MODEL_SERVICES.embedding.dimension"
        ),
    }

    result: dict[str, ProfileInfo] = {}
    for name, info in merged.items():
        data: dict[str, Any] = {
            "name": name,
            "model": info.model,
            "model_version": info.model_version,
            "base_url": info.base_url,
            "dimension": info.dimension,
            "note": info.note,
        }
        if name == DEFAULT_PROFILE:
            # 默认 profile 允许被旧的单项环境变量覆盖
            data["model"] = (
                os.environ.get("AS711_EMBED_MODEL") or active_defaults["model"] or info.model
            )
            data["base_url"] = (
                os.environ.get("AS711_EMBED_BASE_URL") or active_defaults["base_url"] or info.base_url
            )
            data["dimension"] = coerce_dimension(
                os.environ.get("AS711_EMBED_DIM") or active_defaults["dimension"] or info.dimension,
                where="AS711_EMBED_DIM / MODEL_SERVICES.embedding.dimension",
            )
        override = _env_profile_overrides(name, overrides)
        unknown = set(override) - set(ProfileInfo.__slots__)
        if unknown:
            raise EmbeddingError(
                f"KB_EMBED_PROFILES.{name} 含未知字段：{sorted(unknown)}"
                f"（可用：{sorted(ProfileInfo.__slots__)}）"
            )
        if "name" in override:
            # profile 名必须与配置键一致：否则会与 collection 登记、查询缓存键、
            # 探测结果全部错位（而且错得很安静）。
            raise EmbeddingError(
                f"KB_EMBED_PROFILES.{name} 不允许覆盖 name 字段（收到 {override['name']!r}）："
                "profile 名必须与配置键一致"
            )
        data.update(override)
        # 逐字段强校验：类型/维度非法在这里就报错，不留给后面某次比较"意外通过"
        data["model"] = coerce_text(data["model"], where=f"profile {name}.model")
        data["model_version"] = coerce_text(
            data["model_version"], where=f"profile {name}.model_version"
        )
        data["base_url"] = coerce_text(data["base_url"], where=f"profile {name}.base_url")
        data["note"] = coerce_text(data["note"], where=f"profile {name}.note")
        data["dimension"] = coerce_dimension(
            data["dimension"], where=f"profile {name}.dimension"
        )
        result[name] = ProfileInfo(**{k: data[k] for k in ProfileInfo.__slots__})
    return result


def active_profile_name() -> str:
    """当前激活的 profile 名。默认 ``bge1024``——除非显式改，否则不动。"""
    return (
        os.environ.get("KB_EMBED_ACTIVE_PROFILE")
        or getattr(settings, "KB_EMBED_ACTIVE_PROFILE", DEFAULT_PROFILE)
        or DEFAULT_PROFILE
    )


def active_profile() -> ProfileInfo:
    name = active_profile_name()
    table = profiles()
    if name not in table:
        raise EmbeddingError(f"KB_EMBED_ACTIVE_PROFILE={name!r} 不在已配置 profile 里：{sorted(table)}")
    return table[name]


def get_profile(name: str) -> ProfileInfo:
    table = profiles()
    if name not in table:
        raise EmbeddingError(f"未知嵌入 profile：{name!r}（可用：{sorted(table)}）")
    return table[name]


def profile_fingerprint(profile: "ProfileInfo") -> str:
    """profile 的**完整快照指纹**（名 + 模型 + 版本 + 维度 + 端点）。

    用途：查询向量的缓存键、熔断/服务身份。只按 profile 名做键，会让"同名 profile
    改了版本/端点"的两代共用同一个 query vector，从而静默用错模型 —— 这正是换模型
    期间最危险的错。

    **编码必须是规范且无歧义的**（审查修正）：早先用 ``"|".join([...])``，
    于是一个字段里含 ``|`` 就能伪造出另一组的编码 —— 例如
    ``model="a|b"`` + ``version="c"`` 与 ``model="a"`` + ``version="b|c"``
    拼出来是同一个字符串。命中同一个缓存键 = 用错模型查索引，而且是**静默**的。
    现在改成**规范 JSON 数组**（定长顺序、``ensure_ascii=False``、紧凑分隔符）：
    字段内容里的引号/竖线/换行都会被 JSON 转义，不同组合不可能拼出同一串。
    """
    return json.dumps(
        [
            profile.name,
            profile.model,
            profile.model_version,
            int(profile.dimension),
            (profile.base_url or "").rstrip("/"),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def embedding_service_name(profile: "ProfileInfo | str") -> str:
    """嵌入服务的**无歧义**服务名（熔断键、审计、指标共用同一把尺子）。

    为什么不能只用 ``embed:<profile 名>``（审查建议）：profile 名是**配置项的名字**，
    不是模型身份。同名 profile 换了端点或模型版本之后，两套服务会共用同一个熔断器：

    * 旧端点挂了 → 把新端点那套一起熔断（健康组被拖垮）；
    * 旧端点"健康" → 新端点持续的故障被它的成功计数掩盖。

    换模型/换端点期间两代索引同时在线上，这是实打实的串扰。所以键里必须带上
    **完整快照指纹**的密码学摘要：

        ``embed:<profile 名>#<SHA256(规范 JSON 快照指纹) 全量十六进制>``

    * 摘要取 **SHA256 全量**（不是截断）——截断摘要只能叫"碰撞概率很低"，
      不能叫"无歧义"；键长多几十字节毫无代价（Redis 键、日志都放得下），
      而"两套不同配置被当成同一个服务"是要命的错。
    * 指纹本身是规范 JSON（见 :func:`profile_fingerprint`），字段内容无法拼出歧义。

    只在拿不到完整快照（连 profile 对象都没有）时才退化成裸名字——那种情况下
    本来也无法证明两套配置是不是同一个模型。
    """
    if isinstance(profile, str):
        name = profile
        fingerprint = profile
    else:
        name = profile.name
        fingerprint = profile_fingerprint(profile)
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return f"embed:{name}#{digest}"


def profile_for_registration(
    *,
    profile: str = "",
    model: str = "",
    model_version: str = "",
    dimension: int = 0,
    base_url: str = "",
    where: str = "collection 登记",
) -> ProfileInfo:
    """把"某一代 collection 的**登记快照**"解析成一个可用的 :class:`ProfileInfo`。

    **优先级（本轮修正的核心）**：**完整快照固定优先**。

    * 快照完整（model + dimension + base_url 都有）→ **一律使用快照**的
      model/version/dim/base_url。即使同名 profile 的配置后来改了端点或版本，
      这一代也必须继续用它建立时的服务；配置漂移只写进 ``note`` 供排查，
      **不允许静默转向新服务**（否则旧代会悄悄查错模型）。
    * 快照不完整（历史行缺端点等）→ 只有在**配置与快照严格相符**
      （model/version/dimension 全部一致）时才用配置补齐端点；否则显式拒绝。
    * profile 名不在配置表里 → 只能用快照；不完整就拒绝。
    * **任何情况下都不回落到全局 active profile**，也不补零/截断/改维度。
    * 离线测试适配器启用时端点不参与（不需要真实 HTTP），此时允许缺端点。
    """
    from .testing_adapter import enabled as _adapter_enabled

    need_endpoint = not _adapter_enabled()
    dim = coerce_dimension(dimension, where=f"{where}.dimension") if dimension else 0
    name = (profile or "").strip()
    table = profiles()
    configured = table.get(name) if name else None
    has_model, has_dim, has_base = bool(model), dim > 0, bool(base_url)

    if has_model and has_dim and (has_base or not need_endpoint):
        note = "由 collection 登记快照固定（该代模型/端点不随全局配置变化）"
        if configured is not None:
            diffs: list[str] = []
            if configured.model != model:
                diffs.append(f"model 配置={configured.model!r}")
            if configured.model_version != model_version:
                diffs.append(f"model_version 配置={configured.model_version!r}")
            if configured.dimension != dim:
                diffs.append(f"dimension 配置={configured.dimension}")
            if (configured.base_url or "").rstrip("/") != (base_url or "").rstrip("/"):
                diffs.append(f"base_url 配置={configured.base_url!r}")
            if diffs:
                note += (
                    "；**检测到配置漂移**（" + "；".join(diffs) + "）：本代仍固定使用快照，"
                    "如需改用新配置必须重建该代并按受控流程切换"
                )
        return ProfileInfo(
            name=name or f"snapshot@{dim}",
            model=model,
            model_version=model_version,
            base_url=base_url,
            dimension=dim,
            note=note,
        )

    if configured is None:
        raise EmbeddingError(
            f"{where} 的快照不完整（model={model!r}, dimension={dim}, base_url={base_url!r}），"
            f"且 profile {name!r} 不在已配置表里：拒绝用其它模型顶替这一代索引。"
        )
    problems: list[str] = []
    if not has_model:
        problems.append("快照缺 model")
    elif configured.model != model:
        problems.append(f"model 配置={configured.model!r} vs 快照={model!r}")
    if not has_dim:
        problems.append("快照缺 dimension")
    elif configured.dimension != dim:
        problems.append(f"dimension 配置={configured.dimension} vs 快照={dim}")
    if model_version and configured.model_version and configured.model_version != model_version:
        problems.append(
            f"model_version 配置={configured.model_version!r} vs 快照={model_version!r}"
        )
    # 版本口径（本轮加严）：**不允许"双方都非空才比较"这条绕过路径**。
    if not configured.model_version:
        problems.append("配置的 model_version 为空：无法证明是同一版本，拒绝用配置补齐")
    elif not model_version:
        problems.append(
            "快照的 model_version 为空：旧行版本未冻结，拒绝随配置取版本"
            "（请执行迁移补齐或重建该代）"
        )
    elif configured.model_version != model_version:
        problems.append(
            f"model_version 配置={configured.model_version!r} vs 快照={model_version!r}"
        )
    if problems:
        raise EmbeddingError(
            f"{where} 的快照不完整且与当前配置不严格相符：{'；'.join(problems)}。"
            "拒绝用配置补齐后查询/写入这一代索引（避免串模型/串维度）；请重建该代。"
        )
    if need_endpoint and not configured.base_url:
        raise EmbeddingError(
            f"{where} 只能由 profile {name!r} 的配置补齐端点，但该配置没有 base_url："
            "拒绝回落到全局 active profile。"
        )
    return replace(
        configured,
        model_version=model_version or configured.model_version,
        note=(configured.note or "")
        + "；端点由**严格相符**的配置补齐（该代历史登记缺端点）",
    )


def _endpoint(base_url: str, path: str) -> str:
    if not base_url:
        raise EmbeddingError("该 profile 尚未配置 base_url（模型未部署）")
    return base_url.rstrip("/") + path


def embed_texts(
    texts: list[str],
    *,
    profile: ProfileInfo | None = None,
    timeout: float | None = None,
    client: httpx.Client | None = None,
    max_attempts: int | None = None,
    breaker: CircuitBreaker | None = None,
) -> list[list[float]]:
    """调用嵌入服务，**并逐条校验维度**。

    维度不匹配立刻抛 :class:`DimensionMismatch`：不补零、不截断、不返回半截结果，
    调用方拿不到任何可用于写索引的向量。

    韧性：超时 + 重试上限 + 熔断（见 :mod:`apps.knowledge.resilience`）。
    服务不可用时抛 :class:`~apps.knowledge.resilience.ServiceUnavailable`，
    调用方据此如实报"模型服务不可用"，绝不返回空向量凑数。

    **离线测试适配器不在这里生效**：``embed_texts`` 永远是真实 HTTP 调用。
    想用假向量请走 :func:`embed_for_indexing`——它带 collection 校验，
    保证假向量进不去正式索引。
    """
    prof = profile or active_profile()
    if not texts:
        return []
    # 一个 Unicode 字符最坏会被拆为多个 token；按 UTF-8 字节做保守窗口。
    # 保留完整原文，将真实窗口向量按长度加权合并，不截断原文或修改只读分段。
    if "bge-large-zh" in prof.model.lower():
        windows = []
        groups = []
        for text in texts:
            pieces, current, size = [], [], 0
            for character in text:
                width = len(character.encode("utf-8"))
                if current and size + width > 480:
                    pieces.append("".join(current))
                    current, size = [], 0
                current.append(character)
                size += width
            pieces.append("".join(current))
            groups.append((len(windows), len(pieces)))
            windows.extend(pieces)
        if len(windows) > len(texts):
            vectors = []
            for offset in range(0, len(windows), 16):
                vectors.extend(embed_texts(windows[offset:offset + 16], profile=prof, timeout=timeout,
                                          client=client, max_attempts=max_attempts, breaker=breaker))
            result = []
            for start, count in groups:
                if count == 1:
                    result.append(vectors[start])
                    continue
                weights = [max(1, len(window)) for window in windows[start:start + count]]
                total = sum(weights)
                pooled = [sum(vector[i] * weight for vector, weight in
                              zip(vectors[start:start + count], weights)) / total
                          for i in range(prof.dimension)]
                norm = math.sqrt(sum(value * value for value in pooled))
                result.append([value / norm for value in pooled] if norm else pooled)
            return result
    # 超时来自配置（默认 10 秒）。**不要用 30 秒这种大默认值**：
    # 模型服务断开时，检索要等"超时 × 重试上限"，界面会长时间停在"检索中"，
    # 而关键字那一路本可以立刻返回（实测过：默认 20 秒超时 × 2 次 = 40 秒）。
    timeout = float(
        timeout if timeout is not None else getattr(settings, "KB_EMBED_TIMEOUT_SECONDS", 10)
    )
    payload = {"model": prof.model, "input": texts}
    owns_client = client is None
    # **离线模式在构造客户端之前就返回**：连 http 客户端都不造，
    # 报错直指真实原因（"服务未启用"），而不是某个构造期的网络错误。
    from .resilience import disabled_service, model_calls_disabled

    if model_calls_disabled():
        raise disabled_service(embedding_service_name(prof))
    # 必须 trust_env=False：宿主 NO_PROXY 里的畸形条目会让 httpx 构造就失败
    # （见 apps/knowledge/httpclient.py 的说明）
    http = client or build_http_client(timeout=timeout)
    try:
        body = _post_json_with_resilience(
            http=http,
            url=_endpoint(prof.base_url, "/embeddings"),
            payload=payload,
            # **服务名与熔断键必须是完整快照身份**（同名不同端点/版本不能互相拖垮）
            service=embedding_service_name(prof),
            breaker=breaker
            or breaker_for(
                embedding_service_name(prof),
                failure_threshold=int(getattr(settings, "KB_SERVICE_FAILURE_THRESHOLD", 3)),
                cooldown_seconds=float(getattr(settings, "KB_SERVICE_COOLDOWN_SECONDS", 60)),
            ),
            max_attempts=max_attempts
            if max_attempts is not None
            else int(getattr(settings, "KB_EMBED_RETRIES", 2)) + 1,
        )
    finally:
        if owns_client:
            http.close()

    data = body.get("data")
    if not isinstance(data, list) or len(data) != len(texts):
        raise EmbeddingError(
            f"嵌入服务返回条数异常：期望 {len(texts)}，实际 "
            f"{len(data) if isinstance(data, list) else type(data).__name__}"
        )

    # **按 index 排序 + 拒绝重复/越界**：顺序错位会把 A 的向量写到 B 上（静默串数据），
    # 比直接报错危险得多。OpenAI 兼容服务"通常"保证顺序，但"通常"不能当契约。
    indexed: list[tuple[int, Any]] = []
    seen_index: set[int] = set()
    for position, item in enumerate(data):
        raw_index = item.get("index") if isinstance(item, dict) else None
        if raw_index is None:
            idx = position  # 没有 index 字段：按返回顺序解释
        else:
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                raise EmbeddingError(f"第 {position} 条响应的 index 非法：{raw_index!r}")
            idx = raw_index
        if idx in seen_index:
            raise EmbeddingError(f"嵌入服务返回了重复的 index={idx}，拒绝猜测顺序")
        if not (0 <= idx < len(texts)):
            raise EmbeddingError(f"嵌入服务返回的 index={idx} 越界（本批共 {len(texts)} 条）")
        seen_index.add(idx)
        indexed.append((idx, item))
    indexed.sort(key=lambda pair: pair[0])

    vectors: list[list[float]] = []
    for idx, item in indexed:
        vector = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(vector, list) or not vector:
            raise EmbeddingError(f"index={idx} 的响应缺少 embedding")
        if len(vector) != prof.dimension:
            raise DimensionMismatch(
                f"维度不匹配：profile {prof.name} 声明 {prof.dimension} 维，"
                f"模型 {prof.model or '(未命名)'} 实际返回 {len(vector)} 维。"
                "已拒绝写入，不会补零/截断，也不会污染索引。",
                expected=prof.dimension,
                actual=len(vector),
            )
        values: list[float] = []
        for component, raw in enumerate(vector):
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise EmbeddingError(
                    f"index={idx} 的第 {component} 个分量类型非法：{type(raw).__name__}"
                )
            value = float(raw)
            if not math.isfinite(value):
                raise EmbeddingError(
                    f"index={idx} 的第 {component} 个分量不是有限数（{raw!r}）："
                    "拒绝把 NaN/Inf 写进索引"
                )
            values.append(value)
        vectors.append(values)
    return vectors


def probe_dimension(
    *,
    profile: ProfileInfo,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """真实调用一次，回报该 profile 是否就绪以及实测维度。

    这就是"就绪"的唯一来源：**不猜、不声明**。2048 的 profile 在目标模型部署前
    会稳定返回 ``ready=False``。
    """
    report: dict[str, Any] = {
        "name": profile.name,
        "model": profile.model,
        "base_url": profile.base_url,
        "declared_dimension": profile.dimension,
        "actual_dimension": None,
        "ready": False,
        "error": "",
    }
    if not profile.model or not profile.base_url:
        report["error"] = "profile 未配置 model/base_url（目标模型尚未部署）"
        return report
    try:
        vectors = embed_texts(["维度探测"], profile=profile, timeout=timeout, client=client)
    except DimensionMismatch as exc:
        # 维度不匹配是"最有价值"的探测结果：把实测维度带回去，方便定位模型版本
        report["actual_dimension"] = exc.actual
        report["error"] = f"实测 {exc.actual} 维 ≠ 声明 {exc.expected} 维，判定为未就绪：{exc}"
        return report
    except EmbeddingError as exc:
        report["error"] = str(exc)
        return report
    actual = len(vectors[0])
    report["actual_dimension"] = actual
    report["ready"] = actual == profile.dimension
    if not report["ready"]:
        report["error"] = (
            f"实测 {actual} 维 ≠ 声明 {profile.dimension} 维，判定为未就绪"
        )
    return report


def assert_profile_ready(profile: ProfileInfo, *, timeout: float = 20.0) -> dict[str, Any]:
    """写索引前的硬闸门：profile 未就绪就不允许开始嵌入。"""
    report = probe_dimension(profile=profile, timeout=timeout)
    if not report["ready"]:
        raise EmbeddingError(
            f"嵌入 profile {profile.name} 未就绪，拒绝写入索引：{report['error']}"
        )
    return report


def embed_for_indexing(
    texts: list[str],
    *,
    collection: str,
    profile: ProfileInfo | None = None,
    client: httpx.Client | None = None,
    max_attempts: int | None = None,
    breaker: CircuitBreaker | None = None,
    timeout: float | None = None,
) -> tuple[list[list[float]], bool]:
    """给"要写进某个 collection"的场景取向量。

    返回 ``(vectors, used_adapter)``。当且仅当显式打开了离线测试适配器
    （``KB_TEST_ADAPTER=offline``）时，才返回确定性假向量；此时 ``collection``
    **必须**以 ``__offline_test`` 结尾，否则抛
    :class:`~apps.knowledge.testing_adapter.TestAdapterForbidden`。

    这样"假向量污染正式索引"在代码层面就不可能发生：正式 collection 名不带后缀。
    """
    from .testing_adapter import OfflineAdapter, enabled as adapter_enabled

    if adapter_enabled():
        adapter = OfflineAdapter()
        return adapter.embed(texts, collection=collection), True
    return (
        embed_texts(
            texts,
            profile=profile,
            client=client,
            max_attempts=max_attempts,
            breaker=breaker,
            timeout=timeout,
        ),
        False,
    )
