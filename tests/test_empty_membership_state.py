"""空权限态（"在任何信息库都没有成员权限"）必须**正确显示并真正禁用**。

Codex 独立角色复验在 `786a` 镜像上实测到两条真实缺陷（不是夹具误报）：

1. 无成员权限的说明被自己清空：`loadFiles` 先写"没有成员权限…"，随后
   `renderRows → updatePermissionNote()` 在被置灰动作计数全 0 时用空串把它抹掉；
2. 无库时控件仍可用：`bulkParseAllowed()` 在 `libraryRoles.size === 0` 时直接放行，
   `updateUploadGate()` 在筛选为「全部信息库」时也会 clearGate。

关键区分：`libraryRoles === null`（权限**未知**：请求失败）**不能**禁用任何东西
（否则一次请求失败就把有权限的用户锁死）；`size === 0`（**确定**没有任何库成员）
才禁用并说明"联系库管理员"。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES_JS = ROOT / "apps/webui/static/webui/kb-files.js"


def _js() -> str:
    return FILES_JS.read_text(encoding="utf-8")


def test_unknown_permissions_are_not_treated_as_no_membership():
    """权限未知（null）与确定无成员（size 0）必须分开，不能互相顶替。"""
    js = _js()
    assert "let noLibraryMembership = false;" in js
    # 只有"成功取回且为空、且非系统管理员"才置位
    assert "noLibraryMembership = !isSystemAdmin() && map.size === 0;" in js
    # 请求失败分支显式复位，不能沿用上一次的"无成员"结论
    assert "libraryRoles = null;" in js
    failed_branch = js.index("if (!result.ok) {")
    reset = js.index("noLibraryMembership = false;", failed_branch)
    assert reset > failed_branch, "请求失败时没有复位 noLibraryMembership"


def test_no_membership_note_is_not_wiped_by_the_denied_count_branch():
    """无成员说明必须在"没有被置灰动作就清空"之前短路掉（否则会被自己清空）。"""
    js = _js()
    body = js[js.index("function updatePermissionNote()"):js.index("async function loadCapabilities()")]
    no_member_at = body.index("if (noLibraryMembership) {")
    clear_at = body.index('if (!reasons.length) { showPermissionNote(""); return; }')
    assert no_member_at < clear_at, "无成员说明仍在清空分支之后，会被抹掉"
    # 列表拿到结果时的旧写法（自己写权限条）已移除
    assert 'showPermissionNote("你当前在**任何信息库**里都没有成员权限，所以看不到资料。"' not in js
    # 事实（有没有筛选）交给唯一出口决定措辞
    assert "emptyListUnfiltered" in js


def test_empty_library_list_disables_bulk_parse_and_upload():
    """确定没有任何库成员权限时：「全部解析」「上传/扫描目录」都要禁用并说明原因。"""
    js = _js()
    bulk = js[js.index("function bulkParseAllowed()"):js.index("function updateBulkActionsGate()")]
    assert "if (!libraryRoles.size) {" in bulk
    assert "return false;" in bulk, "size===0 时仍在放行「全部解析」"
    assert "if (!libraryRoles) return true;" in bulk, "权限未知时应当放行（交后端判定）"

    gate = js[js.index("function updateUploadGate()"):js.index("function clearUploadGate()")]
    no_member_at = gate.index("if (noLibraryMembership) {")
    clear_at = gate.index('if (state.library === "all" || !libraryRoles) {')
    assert no_member_at < clear_at, "无成员时仍会走 clearGate（上传按钮保持可用）"
    assert "写入权限才能上传" in gate


def test_no_membership_copy_tells_the_user_the_next_step():
    """文案要给**下一步**（联系库管理员），而不是术语（403/权限码/API）。"""
    js = _js()
    for text in ("联系", "库管理员"):
        assert text in js
    for jargon in ("403", "API", "接口", "参数"):
        # 只检查这两段用户可见文案里没有术语
        note = js[js.index("if (noLibraryMembership) {"):js.index("const reasons = [];")]
        assert jargon not in note, f"无成员提示里出现术语：{jargon}"
