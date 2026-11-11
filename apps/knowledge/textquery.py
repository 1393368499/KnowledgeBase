"""检索用的中文友好切词与覆盖率打分。

为什么需要它（实测基线见 ``evals/BASELINE.md``）：

* 旧实现用 ``re.split(r"[\\s\\W_]+", query)`` 切词，对中文几乎无效——中文没有空格，
  一整句只能切出一个词，必须整句原样出现在 chunk 文本里才算命中；
* 把查询里的空格去掉（这正是真实用户的输入形态）后，离线词法 Recall@5 从 88%
  掉到 12%，8 条纯中文问句 0 命中；仅有的 6 条命中全都带 ASCII 编号
  （靠编号数字侥幸命中，不是中文起了作用）。

做法（可解释、不引入第三方分词依赖）：

* 拉丁/数字词按非字母数字边界切分，保留 ``-`` ``_`` ``.`` 连接（``GB-T-19001``）；
* 连续汉字串按 **二元组（bigram）** 切分：``预付款比例`` → 预付/付款/款比/比例；
  二元组对中文是接近最优的零依赖切分粒度（单字噪音大，三元组召回低）；
* 两类词一起去重、按长度降序、限量，交给 SQL/内存做 OR 匹配，
  再用"覆盖了几个查询词"打分——**命中越全的片段排得越前**，而不是靠 id 顺序。
"""

from __future__ import annotations

import re
import unicodedata

#: 连续汉字（含扩展 A 区与兼容区）。不把标点算进来，避免切出跨标点的假词。
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

#: 拉丁/数字词：允许 ``-`` ``_`` ``.`` 连接（型号、编号、版本号）。
_LATIN_TOKEN = re.compile(r"[0-9A-Za-z]+(?:[-_.][0-9A-Za-z]+)*")

#: 单字中文命中率极低但噪音极大，不作独立查询词。
MIN_CJK_RUN = 2

#: 拉丁词最短长度：与旧的 ``MIN_KEYWORD_LEN`` 保持一致。
MIN_LATIN_LEN = 2

#: 查询词上限：每个词都会变成一条 ``OR`` 子句，太多会让 SQL/全文查询变慢。
MAX_TERMS = 24


def terms(query: str, *, max_terms: int = MAX_TERMS) -> list[str]:
    """把查询串切成"可匹配的查询词"。返回小写、去重、长词在前的列表。"""
    text = unicodedata.normalize("NFKC", str(query or ""))
    ordered: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        term = term.strip().lower()
        if not term or term in seen:
            return
        seen.add(term)
        ordered.append(term)

    for match in _LATIN_TOKEN.finditer(text):
        token = match.group(0)
        if len(token) >= MIN_LATIN_LEN:
            add(token)

    for match in _CJK_RUN.finditer(text):
        run = match.group(0)
        if len(run) < MIN_CJK_RUN:
            continue
        for index in range(len(run) - 1):
            add(run[index:index + 2])
        # 短片段本身也是完整词（"质保期"），整串一起参与匹配，命中更精确。
        if len(run) <= 4:
            add(run)

    # 长词优先：更长的词区分度更高，SQL 命中范围更小，且排序稳定。
    ordered.sort(key=len, reverse=True)
    return ordered[:max_terms]


def coverage(text: str, query_terms: list[str]) -> int:
    """这段正文覆盖了几个查询词（0 表示完全没沾边）。"""
    if not text or not query_terms:
        return 0
    lowered = str(text).lower()
    return sum(1 for term in query_terms if term in lowered)


__all__ = ["terms", "coverage", "MAX_TERMS", "MIN_CJK_RUN", "MIN_LATIN_LEN"]
