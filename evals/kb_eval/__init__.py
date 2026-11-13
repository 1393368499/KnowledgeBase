"""KnowledgeBase 检索评测包（offline / online 两种模式共用）。

模块职责：

- ``metrics``  —— 纯函数指标：Recall@k / recall_all@k / MRR / 要点命中率 /
                 P50·P95 延迟 / token 启发式估算 / 成本估算。
- ``report``   —— 把逐条 case 聚合成 ``{"meta","summary","cases","not_measured"}``。
- ``harness``  —— 离线跑真实 Django 检索链路的夹具（由 harness-engineer 提供）。
- ``client_http`` —— 在线模式 HTTP 检索客户端（可选）。

本文件只放包说明，不写业务逻辑；避免与其它模块循环导入。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import metrics, report

__all__ = ["metrics", "report"]
