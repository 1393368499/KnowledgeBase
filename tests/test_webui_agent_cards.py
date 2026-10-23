"""Agent 控制台：三张"暂无状态"卡与三个"—"指标必须接上**真实来源**（源码级断言）。

口径来自 `docs/function-operation-matrix.md`：**「每项给出真实判定来源；没有就保持
灰点，不得伪造」**。所以这里锁死两件事：

  1. 有真实来源的（看门狗 / 结果校验 / 双阶段补扫 / 停滞 / 重启次数 / 待反馈）
     必须从后端字段读，而且**读不到就回到"暂无状态 / —"**（不是编一个数）；
  2. 「立即巡检」必须接真实巡检意图（此前错配到 `list_audit`，属假绿灯）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

PAGES = Path("apps/webui/static/webui/kb-pages.js")


@pytest.fixture(scope="module")
def source() -> str:
    return PAGES.read_text(encoding="utf-8")


def test_three_cards_no_longer_hardcode_no_status(source):
    """三张卡不能再在 `dogs` 数组里写死"暂无状态"（值必须来自派生变量）。"""
    dogs_block = source.split("const dogs = [")[1].split("];")[0]
    assert "暂无状态" not in dogs_block, "dogs 数组里仍有写死的暂无状态"
    for label in ("解析看门狗", "双阶段补扫", "结果校验"):
        # 允许在**卡片计算块**里作为"后端没给数"的兜底，但不允许直接进数组
        assert f'["{label}", null, "暂无状态"]' not in dogs_block
    # 看门狗/结果校验的兜底文案是"还没跑过巡检"，而不是含糊的"暂无状态"
    assert source.count("patrolHint") >= 3


def test_cards_read_the_real_backend_snapshots(source):
    """卡片读数必须来自运行指标里的巡检留痕（watchdog / verification / resweep）。"""
    assert 'const watchdog = (rm && rm.watchdog) || null;' in source
    assert 'const verification = (rm && rm.verification) || null;' in source
    assert 'const resweep = (rm && rm.resweep) || null;' in source
    # 六张卡的数组里必须用这三个派生结果，而不是内联常量
    assert "watchdogCard," in source
    assert "verificationCard," in source
    assert "resweepCard," in source
    # 没跑过巡检时必须**回退到灰点**，并说明可以点「立即巡检」
    assert '"还没跑过巡检（可点右侧「立即巡检」）"' in source
    assert "watchdog.available" in source
    assert "verification.available" in source


def test_metrics_placeholders_are_gone(source):
    """三个"—"指标必须读真实字段（停滞任务 / 重启次数 / 待处理反馈）。"""
    for placeholder in (
        '["停滞任务", "—"]',
        '["重启次数", "—"]',
        '["待处理反馈", "—"]',
    ):
        assert placeholder not in source, f"仍是占位：{placeholder}"
    assert 'rm.stalled_tasks === "number"' in source or 'typeof rm.stalled_tasks === "number"' in source
    assert "operations.body.summary.restart_stalled_executed" in source
    assert 'typeof rm.feedback_pending === "number"' in source


def test_scan_action_runs_a_real_patrol(source):
    """「立即巡检」必须接真实巡检意图；不能再错配到 list_audit（看审计列表）。"""
    assert 'scan: "run_patrol"' in source, "「立即巡检」没接真实巡检"
    assert 'scan: "list_audit"' not in source, "「立即巡检」仍错配到审计列表"
    # 「重扫未处理反馈」现在也是真实意图
    assert 'rescan_annotations: "reparse_failed"' in source


def test_learn_action_is_a_real_readonly_report(source):
    """「触发一次学习」没有真实动作可做（本系统**不做训练**）→ 接**只读**规则快照。

    之前这个按钮被归到"暂不可执行"；现在按钮文字改成实话（查看学习规则），
    并映射到真实的只读意图 —— 既不是假成功，也不是一个点了没反应的按钮。
    """
    block = source.split("const ACTION_INTENT = {")[1].split("};")[0]
    assert 'learn: "feedback_learning_report"' in block, "「查看学习规则」没接真实意图"
    assert '"触发一次学习"' not in source, "仍保留「训练」语义的按钮文字"
    assert "查看学习规则" in source


def test_unmapped_actions_still_say_so(source):
    """**以后新增**的未映射动作仍必须如实说明，不能假装成功。"""
    assert "这个动作暂时还不能执行" in source


def test_findings_labels_match_backend_keys(source):
    """界面用的四项中文名必须与巡检返回的 counts 键一一对应（拼错就会显示空）。"""
    for key in ("failed_exhausted", "stalled", "vector_incomplete", "blob_missing"):
        assert f"{key}:" in source, f"缺少分项键：{key}"


def test_feedback_table_is_wired_to_the_real_endpoint(source):
    """「检索反馈（批注）」表不得再写死空态：必须真的去取自己的反馈。

    端点两个方向（POST/GET）一直存在，界面却长期显示"还没有检索反馈"——
    这正是"假占位"，所以这里锁死"必须请求真实地址 + 必须渲染真实字段"。
    """
    assert 'KB.api("/api/v1/knowledge/search-feedback/")' in source
    # 表体来自派生结果 feedbackBody，而不是内联的写死行
    assert "const feedback = (feedbackList.ok" in source
    assert "const feedbackBody = feedback.length" in source
    assert "feedbackBody +" in source
    # 三个真实字段：资料名 / 判定中文名 / 净分口径
    for field in ("item.file_name", "item.verdict_label", "item.net"):
        assert field in source, f"表格没读真实字段：{field}"
    # 空态只描述"怎么产生反馈"，不再是含糊的绝对空态
    assert "还没有检索反馈：在「搜索」里对结果点" in source
    assert "会被「重扫未处理反馈」覆盖" in source
    # 有备注时"问题"列只显示备注 → 查询词仍要能查到（title 提示，不是丢掉）
    assert "title='检索「" in source
