"""项目统一的外呼 HTTP 客户端工厂。

为什么必须显式 ``trust_env=False``
----------------------------------
本机的环境变量里有一个畸形条目 ``NO_PROXY=...,[::1]``。httpx 会把 ``NO_PROXY`` 的
每一项当作 URL pattern 解析，``[::1]``（带方括号、无端口）会让 ``URLPattern.__init__``
直接抛 ``httpx.InvalidURL: Invalid port: ':1]'``——注意异常发生在**构造客户端**时，
连请求都发不出去。

这不是本项目的配置错误，而是宿主环境缺陷，但它会连带打挂所有外呼（嵌入/重排/OCR）
和 qdrant-client（它内部也用 httpx）。系统不依赖宿主环境变量来实现网络连通性：
AS711 在内网、Qdrant 在 127.0.0.1，本来就不该走代理。

因此：**所有外呼都必须用本模块的 :func:`build_http_client`**，不要直接
``httpx.Client()``。

为什么不"临时改 os.environ 再改回来"
------------------------------------
早先的实现用 ``clean_proxy_environment()`` 上下文管理器改进程级环境变量，因为当时
以为 qdrant-client 不让我们注入 httpx 参数。**那个做法已废弃**，原因：

1. 它改的是**进程全局**状态。多线程并发时，线程 A 恢复的快照可能是线程 B 改过之后
   的值，互相污染（审查意见 8 指出的问题）；
2. 它其实没必要——已核对 ``qdrant_client/http/api_client.py``：
   ``ApiClient.__init__(self, host, **kwargs)`` 会把 kwargs 直接交给
   ``httpx.Client(**kwargs)``。所以 ``trust_env=False`` 可以**直接当作关键字参数**
   传给 ``QdrantClient``，由它透传下去（``vectorindex.build_client()`` 就是这么做的，
   并且有一条专门的回归测试盯着）。

结论：只保留"每个客户端自己 trust_env=False"，不再有任何全局环境修改。
"""

from __future__ import annotations

import os

import httpx

#: 默认超时（秒）。单个客户端可按需覆盖。
DEFAULT_TIMEOUT = 60.0
#: 连接超时（秒）。**必须单独设小**，见 :func:`build_http_client` 的说明。
#:
#: 取值理由：熔断需要连续 3 次失败才打开，所以"第一次请求"的最坏等待 ≈
#: ``3 × connect_timeout + 退避``。设 3 秒 → 最坏约 10 秒（实测 16.8 秒 → 10 秒级）；
#: 之后熔断打开，请求直接走关键字在 1 秒内返回。内网可达时连接是毫秒级，
#: 3 秒不会误伤正常调用。
DEFAULT_CONNECT_TIMEOUT = 3.0


def build_http_client(
    *,
    timeout: float = DEFAULT_TIMEOUT,
    connect_timeout: float | None = None,
    verify: bool = True,
    **kwargs,
) -> httpx.Client:
    """构造忽略宿主代理环境变量的 httpx 客户端。

    ``trust_env=False`` 一次解决三件事：不读 ``HTTP_PROXY`` / ``NO_PROXY``
    （避开上面那个畸形条目）、不读 ``.netrc``、不读 ``SSL_CERT_FILE``。

    **连接超时与读取超时必须分开设**（实测教训）：只传一个 ``timeout=10`` 时，
    httpx 会把它当成"所有阶段的总超时"，但底层的 TCP 连接建立仍可能按操作系统的
    SYN 重传节奏耗很久。实测把 ``timeout=10`` 传给一个**不可达**的内网地址，
    单次调用要 **17 秒**才返回；显式设 ``connect=5`` 之后立刻按预期失败。
    服务断开时这个差别直接决定"检索要卡 35 秒还是 1 秒"。
    """
    kwargs.pop("trust_env", None)
    effective_connect = (
        DEFAULT_CONNECT_TIMEOUT if connect_timeout is None else float(connect_timeout)
    )
    return httpx.Client(
        timeout=httpx.Timeout(float(timeout), connect=effective_connect),
        verify=verify,
        trust_env=False,
        **kwargs,
    )


def environment_proxy_defect() -> str:
    """宿主 ``NO_PROXY`` 是否含那个会打挂 httpx 的畸形条目（巡检用，返回空串表示正常）。"""
    raw = os.environ.get("NO_PROXY", "") or os.environ.get("no_proxy", "")
    for item in raw.split(","):
        item = item.strip()
        if item.startswith("[") and item.endswith("]"):
            return (
                f"NO_PROXY 含畸形条目 {item!r}：未显式 trust_env=False 的 httpx 客户端"
                "会在构造阶段就抛 InvalidURL"
            )
    return ""
