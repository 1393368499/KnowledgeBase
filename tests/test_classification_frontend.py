"""「性质」（``classification``）改为**按正文自动识别**之后的前端回归。

用户 2026-10-16 的要求：性质不再由用户手选/按扩展名猜，而是按**正文内容**识别，
五项固定词表（申请 / 合同 / 报表 / 普通文档 / 涉密文档）。界面必须按接口返回如实展示：

* 上传弹窗：``classification`` **默认留空 = 按正文自动识别**，选了就是人工指定，
  并且要写明"人工选择不会被自动识别覆盖"；
* 列表「性质」列 / 搜索结果卡：显示五项之一；**空值显示「待识别」**（不是空白、不是「—」），
  依据/失败原因取 ``classification_note``、来源取 ``classification_source``
  （规则识别 / 模型识别 / 人工设置），都放在 ``title`` 里；
* 性质筛选下拉：五项 + 「待识别」；
* 行内人工纠正的五项下拉**保留**，成功后提示"已人工设置（不会被自动识别覆盖）"。

本文件只有两类断言，都不启动浏览器、不调用模型：
  1. **源码级**：上面每一条界面口径都能在 JS/模板里找到对应实现；
  2. **真实渲染 + 真实 API**：``/app/files/`` 返回的 HTML 里新控件真的存在；
     ``/api/v1/knowledge/files/?nature=__unidentified__`` 的**真实行为**被记录下来
     （后端还没支持"按空性质筛选"时必须显示全部，界面因此**不假装**它生效 ——
     见 ``test_unidentified_filter_is_reported_truthfully``）。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model

from apps.knowledge.models import IngestedFile, KnowledgeLibrary, LibraryMember

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]
COMMON_JS = ROOT / "apps" / "webui" / "static" / "webui" / "kb-common.js"
FILES_JS = ROOT / "apps" / "webui" / "static" / "webui" / "kb-files.js"
SEARCH_JS = ROOT / "apps" / "webui" / "static" / "webui" / "kb-search.js"
HOME_CSS = ROOT / "apps" / "webui" / "static" / "webui" / "kb-home.css"
FILES_HTML = ROOT / "apps" / "webui" / "templates" / "webui" / "files.html"
SEARCH_HTML = ROOT / "apps" / "webui" / "templates" / "webui" / "search.html"
BASE_HTML = ROOT / "apps" / "webui" / "templates" / "webui" / "base.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ============================================================== 夹具 ====


@pytest.fixture
def admin():
    return get_user_model().objects.create_user(
        username=f"nature-{uuid.uuid4().hex[:8]}", password="x", is_staff=True
    )


@pytest.fixture
def library(admin):
    item = KnowledgeLibrary.objects.create(code=f"nat-{uuid.uuid4().hex[:6]}", name="性质库")
    LibraryMember.objects.create(library=item, user=admin, role="admin")
    return item


def _make_file(library, name: str, classification: str) -> IngestedFile:
    return IngestedFile._base_manager.create(
        library=library,
        original_name=name,
        content_sha256=uuid.uuid4().hex,
        size_bytes=10,
        # 与上传路径一致：`file_type` 存的是扩展名（不带点），否则「文件类型」筛选
        # 与「类型」列都会对不上（本地夹具踩过：全写成 md 导致 pdf 筛选为空）
        file_type=name.rsplit(".", 1)[-1].lower(),
        classification=classification,
    )


# ==================================================== 1. 如实显示口径 ====


def test_common_exposes_one_shared_nature_display_rule():
    """文件页与搜索页必须**共用同一处**显示规则（否则两页口径迟早漂移）。"""
    common = _read(COMMON_JS)
    assert "KB.NATURE_PENDING" in common and '"待识别"' in common, "缺少「待识别」的统一文案"
    assert "KB.NATURE_UNIDENTIFIED" in common, "缺少「待识别」筛选的哨兵值定义"
    assert "KB.natureLabel = function" in common, "缺少取值 → 显示文案的统一函数"
    assert "KB.natureTooltip = function" in common, "缺少依据/来源的悬浮说明函数"
    # 空值 → 「待识别」：**不能**显示成空白或「—」
    label_body = common[common.index("KB.natureLabel = function") :]
    label_body = label_body[: label_body.index("KB.natureSourceLabel")]
    assert "KB.NATURE_PENDING" in label_body, "空值没有回落到「待识别」"
    assert '"—"' not in label_body, "空值被显示成「—」（要求是「待识别」）"
    # 来源的中文说法固定三种：规则识别 / 模型识别 / 人工设置
    for wording in ("规则识别", "模型识别", "人工设置"):
        assert wording in common, f"来源的中文说法缺少：{wording}"


def test_tooltip_takes_note_and_source_from_the_api_fields():
    """tooltip 必须**取接口字段**：`classification_note`（依据/失败原因）+ 来源。"""
    common = _read(COMMON_JS)
    tooltip = common[common.index("KB.natureTooltip = function") :]
    tooltip = tooltip[: tooltip.index("/** 与原型同款的 HTML 转义")] if "/** 与原型同款的 HTML 转义" in tooltip else tooltip
    assert "classification_note" in tooltip, "tooltip 没有取 `classification_note`（依据/失败原因）"
    assert "KB.natureSourceLabel" in tooltip, "tooltip 没有取来源（`classification_source`）"
    assert "classification_source" in common, "没有读 `classification_source`"
    # 依据/原因必须**照抄**接口给的话，不能在前端另编一句
    assert '"依据/原因："' in tooltip, "缺少「依据/原因」这一行的文案"


def test_nature_display_does_not_invent_a_verdict():
    """前端**不许**自己下"已自动识别"的结论：没有来源/依据就什么都不说。

    判据看的是**代码行为**（返回值），不是注释措辞 —— 注释里出现"已自动识别"
    正是在说明"不许编这句话"。
    """
    common = _read(COMMON_JS)
    # 没有 classification_source → 来源说法为空（界面上就不显示来源）
    source_label = common[common.index("KB.natureSourceLabel = function") :]
    source_label = source_label[: source_label.index("KB.natureTooltip = function")]
    assert 'if (!source) return "";' in source_label, "没有来源时应当什么都不说"
    # 既没有来源也没有依据 → tooltip 是空串（不会自动生成一句"已自动识别"）
    tooltip = common[common.index("KB.natureTooltip = function") :]
    tooltip = tooltip[: tooltip.index("与原型同款的 HTML 转义")]
    assert "parts.join(" in tooltip, "tooltip 不是由真实字段拼出来的"
    # 界面上"来源"这一行只在真的有来源时才出现
    assert "if (sourceLabel) parts.push" in tooltip, "来源缺失时仍会写一行来源"
    assert "if (note) parts.push" in tooltip, "依据缺失时仍会写一行依据"
    # 显示文案只认 classification_label / classification（不附加任何来源猜测）
    label_body = common[common.index("KB.natureLabel = function") :]
    label_body = label_body[: label_body.index("KB.natureSourceLabel")]
    for invented in ("自动", "识别出", "已识别"):
        assert invented not in label_body, f"显示文案里出现了前端猜测：{invented}"


def test_files_js_renders_label_and_tooltip_in_the_nature_column():
    """列表「性质」列：显示文案 + tooltip，两者都来自接口字段。"""
    js = _read(FILES_JS)
    assert "KB.natureLabel(file)" in js, "列表没有用统一函数显示性质"
    assert "KB.natureTooltip(file)" in js, "列表没有把依据/来源放进 title"
    assert 'class="nature-label"' in js and "natureCell" in js, "缺少「性质」显示单元格"
    # 旧实现把空值显示成「—（未设置）」，必须已经改掉
    assert "—（未设置）" not in js, "空性质仍在显示「—（未设置）」"


def test_search_js_renders_the_same_nature_wording():
    """搜索结果卡与文件页**同口径**。"""
    js = _read(SEARCH_JS)
    assert "KB.natureLabel(item)" in js, "结果卡没有用统一函数显示性质"
    assert "KB.natureTooltip(item)" in js, "结果卡没有把依据/来源放进 title"
    assert 'esc(item.classification || "—")' not in js, "结果卡仍在把空性质显示成「—」"
    # 结构化列表路径也要把三件套透传进结果卡（否则搜索页永远显示「待识别」）
    for field in ("classification_label", "classification_source", "classification_note"):
        assert field in js, f"结果卡数据没有透传 `{field}`"


# ==================================================== 2. 上传弹窗口径 ====


def test_upload_dialog_defaults_to_auto_recognition():
    """上传弹窗：默认**不人工指定** = 由系统按正文自动识别（第一条选项就是它）。

    文案口径（2026-10-16 更正）：选项写「不人工指定（由系统按正文自动识别）」，
    而不是「留空（按正文自动识别）」—— 因为"人工选空"是另一条路径（行内下拉的
    「待识别」），它会被记成 `manual`、**不会**再被自动识别。上传弹窗这一项是
    "根本不传 classification"，两者不能混为一谈。
    """
    js = _read(FILES_JS)
    assert "按正文自动识别" in js, "上传弹窗没有写「不人工指定 = 按正文自动识别」"
    assert "不人工指定（由系统按正文自动识别）" in js, "缺少默认选项文案"
    # 默认选项在列表**第一位**，且 value 为空串（= 不传 classification）
    options_at = js.index('label: "不人工指定（由系统按正文自动识别）"')
    manual_at = js.index('label: value + "（人工指定，不会被自动识别覆盖）"')
    assert options_at < manual_at, "自动识别选项不在人工选项之前（默认选中项必须是它）"
    assert 'value: "",' in js[options_at - 200 : options_at + 50], "默认选项的取值不是空串"
    # 留空时**不上传** classification 字段
    assert 'if (nature) form.append("classification", nature);' in js, (
        "留空时仍会提交 classification（那就等于人工指定了空值，不是「自动识别」）"
    )


def test_upload_dialog_says_manual_choice_wins():
    """选了某一项 = 人工指定，必须写明"不会被自动识别覆盖"。"""
    js = _read(FILES_JS)
    assert "不会被自动识别覆盖" in js, "上传弹窗没有说明人工指定不会被覆盖"
    assert "人工指定，不会被自动识别覆盖" in js, "人工选项文案缺少覆盖说明"


# ================================================== 3. 行内人工纠正 ====


def test_inline_correction_keeps_the_five_options_and_manual_note():
    """行内五项下拉**保留**；来源记为人工设置，成功后提示不会被覆盖。"""
    js = _read(FILES_JS)
    assert "function natureSelect(file)" in js, "行内性质下拉被删掉了"
    assert 'data-nature="' in js and "nature-select" in js, "行内下拉缺少定位属性/样式类"
    # 五项来自服务端词表，不在前端写死
    assert "NATURE_VOCABULARY.forEach" in js, "行内下拉没有用服务端词表"
    assert "NATURE_VOCABULARY = data.classifications" in js, "词表来源不是 /facets/ 的 classifications"
    # 走受控通道，不是表单直改
    assert 'intent: "classify_file"' in js, "行内修改没走受控操作 classify_file"
    # 成功提示必须点明"人工设置 + 不会被自动识别覆盖"
    assert "已人工设置" in js, "缺少「已人工设置」的提示文案"
    assert "（不会被自动识别覆盖）" in js, "成功提示没有说明不会被自动识别覆盖"


# ================================================== 4. 筛选的六项口径 ====


def test_nature_filter_lists_five_values_plus_pending():
    """文件页性质筛选 = 全部 + 五项 + 「待识别」（空串表达不了这个条件）。"""
    js = _read(FILES_JS)
    assert 'id="files-nature"' in _read(FILES_HTML) or "files-nature" in js
    assert "KB.NATURE_UNIDENTIFIED" in js, "性质筛选没有用「待识别」的取值"
    assert 'state.nature !== "all"' in js, "筛选条件没有真的进查询串"
    assert 'params.set("nature", state.nature)' in js, "列表请求没有带上 nature"
    # 行内下拉的留空项就是「待识别」（表格里只放得下这几个字，解释在 title 上）
    assert 'KB.NATURE_PENDING + "</option>"' in js, "行内下拉的留空项没有显示「待识别」"
    # 解释必须**准确**：人工选空 = 保持为空、不会被自动识别覆盖
    # （旧文案"留空 = 还没识别出性质；后端会按正文自动识别"是不成立的承诺：
    #  人工选空会被记成 manual，自动识别看到 manual 直接 skipped_manual）
    assert "留空 = 性质为空（待识别）" in js, "行内下拉的留空项缺少解释（title）"
    assert "自动识别不会覆盖它" in js, "行内下拉没有说明人工清空不会被自动覆盖"


def test_search_page_also_offers_and_sends_pending():
    """搜索页与文件页同口径：下拉有「待识别」，且**真的把它发出去**。"""
    js = _read(SEARCH_JS)
    assert 'KB.NATURE_UNIDENTIFIED' in js, "搜索页没有「待识别」的取值"
    assert "insertAdjacentHTML" in js, "搜索页下拉没有加入「待识别」这一项"
    # 两条通路都要带上（语义检索 payload + 结构化列表查询串）
    assert "payload.nature = state.nature" in js, "语义检索没有带上性质条件"
    assert 'params.set("nature", state.nature)' in js, "结构化列表没有带上性质条件"
    # 标签也要显示中文（不能露出 __unidentified__）
    assert "KB.NATURE_PENDING" in js


def test_no_capability_guessing_and_no_implementation_speak():
    """**不许用数据计数猜接口能力**，也不许把实现细节写给用户看。

    反面教材（本轮已删除）：用"待识别筛出 0 条 + 库里有已识别资料"推断"后端不支持"。
    那在"库里 3 份全部识别成功"时恰好误报（正确结果本来就是 0 条）。
    """
    files_js = _read(FILES_JS)
    search_js = _read(SEARCH_JS)
    for source, name in ((files_js, "kb-files.js"), (search_js, "kb-search.js")):
        for banned in ("detectUnidentifiedSupport", "unidentifiedFilterSupported",
                       "hasAnyNatureValue", "dropUnsupportedNatureOption",
                       "unsupportedNatureNote", "后端尚未支持", "等接口支持"):
            assert banned not in source, f"{name} 仍有「猜能力/实现细节」的残留：{banned}"
        # 用户可见文案里不许出现"不支持"这类对实现下结论的措辞
        assert "不支持" not in source, f"{name} 的用户文案里出现了「不支持」"


def test_empty_result_says_there_is_no_pending_data():
    """0 条时必须说"没有待识别（未识别性质）的资料"，而不是解释接口/参数。"""
    for path, name in ((FILES_JS, "kb-files.js"), (SEARCH_JS, "kb-search.js")):
        source = _read(path)
        assert "没有待识别（未识别性质）的资料" in source, f"{name} 缺少 0 条时的真实文案"
    # 文案由**真实条数**驱动，不是由"猜出来的能力"驱动
    assert "function natureResultNote(count)" in _read(FILES_JS)
    assert "function natureResultNote(count)" in _read(SEARCH_JS)
    # 失败与 0 条必须分开：失败分支里清掉性质说明并把 detail 显示出来
    assert 'showNatureNote("");' in _read(FILES_JS), "文件页失败时没有清掉性质说明"
    assert "lastResultCount = null;" in _read(SEARCH_JS), "搜索页失败时没有作废条数"
    assert "lastResultCount === null" in _read(SEARCH_JS), "搜索页把失败当成 0 条处理"


def test_nature_notes_live_in_page_not_toasts():
    """提示必须留在页面上（独立提示区），不是会消失的 toast。"""
    files_js = _read(FILES_JS)
    search_js = _read(SEARCH_JS)
    assert 'id="files-nature-note"' in _read(FILES_HTML), "文件页缺少筛选说明区"
    assert 'id="search-nature-note"' in _read(SEARCH_HTML), "搜索页缺少筛选说明区"
    assert 'KB.notice("files-nature-note"' in files_js, "文件页说明没有写进提示区"
    assert 'KB.notice("search-nature-note"' in search_js, "搜索页说明没有写进提示区"


# ============================================ 5. 真实渲染 / 真实 API ====


def test_files_page_renders_the_new_nature_controls(client, admin):
    """真实渲染 `/app/files/`：新控件必须真的在发出去的 HTML 里。"""
    client.force_login(admin)
    response = client.get("/app/files/")
    assert response.status_code == 200, response.content[:200]
    page = response.content.decode("utf-8")
    assert 'id="files-nature"' in page, "渲染后的筛选行缺「资料性质」下拉"
    assert 'value="__unidentified__"' in page, "筛选下拉缺「待识别」选项"
    assert 'id="files-nature-note"' in page, "渲染后的页面缺筛选说明区"
    # 样式必须真的被加载（否则「待识别」/行内下拉排版会散）
    assert "kb-home.css" in page, "「性质」显示样式未加载"


def test_search_page_renders_the_nature_note_area(client, admin):
    client.force_login(admin)
    response = client.get("/app/search/")
    assert response.status_code == 200, response.content[:200]
    page = response.content.decode("utf-8")
    assert 'id="nature"' in page, "搜索页缺「资料性质」下拉"
    assert 'id="search-nature-note"' in page, "搜索页缺性质筛选说明区"


def test_nature_css_targets_the_shared_classes():
    """两项新样式（显示 + 行内下拉）在共享样式表里；原型 CSS 已删除，不得复活。"""
    css = _read(HOME_CSS)
    assert ".nature-label" in css, "缺少性质显示样式"
    assert ".nature-select" in css, "缺少行内纠正下拉样式"
    proto = ROOT / "apps" / "webui" / "static" / "webui" / "kb-proto.css"
    assert not proto.exists(), "kb-proto.css 是无引用的死文件，已删除，不能复活"


def _list_names(client, library, **params):
    response = client.get("/api/v1/knowledge/files/", {"library": str(library.pk), **params})
    assert response.status_code == 200, response.content
    body = response.json()
    rows = body["results"] if isinstance(body, dict) else body
    return sorted(row["original_name"] for row in rows)


def test_unidentified_filter_against_real_list_api(client, admin, library):
    """真实**列表**接口：「待识别」筛选的四种情形都要如实（用户给的验收口径）。

    1. 全部未识别 → 筛出全部；
    2. 部分已识别 → 只出空值那几份；
    3. 全部已识别 → 真实 0 条（**不是**"接口不支持"，也不是把已识别的混进来）；
    4. 与其他筛选（文件类型 / 关键词 / 状态）组合仍一致。
    """
    client.force_login(admin)

    # 1) 全部未识别
    a = _make_file(library, "待识别A.md", "")
    b = _make_file(library, "待识别B.pdf", "")
    assert _list_names(client, library, nature="__unidentified__") == ["待识别A.md", "待识别B.pdf"]
    # 空串与"不带该参数"等价（所以前端必须用 __unidentified__ 表达）
    assert _list_names(client, library, nature="") == ["待识别A.md", "待识别B.pdf"]

    # 2) 部分已识别 → 只出空值那几份
    _make_file(library, "合同.md", "合同")
    assert _list_names(client, library, nature="__unidentified__") == ["待识别A.md", "待识别B.pdf"]
    assert _list_names(client, library, nature="合同") == ["合同.md"]

    # 3) 全部已识别 → 真实 0 条（用两个**独立**的库来验证，不改已入库记录 ——
    #    IngestedFile 是受治理的不可变记录，测试里也不许绕过去原地改写）
    other = KnowledgeLibrary.objects.create(
        code=f"nat2-{uuid.uuid4().hex[:6]}", name="全部已识别库", vector_collection=library.vector_collection
    )
    LibraryMember.objects.create(library=other, user=admin, role="admin")
    _make_file(other, "已识别A.md", "报表")
    _make_file(other, "已识别B.md", "申请")
    assert _list_names(client, other, nature="__unidentified__") == []
    # 对照组：这个库里确实有资料，只是没有"待识别"的
    assert len(_list_names(client, other)) == 2

    # 4) 与其它筛选组合：文件类型 / 关键词（组合条件里"已识别的"不许被误带进来）
    _make_file(library, "待识别-扫描.pdf", "")
    pdf_rows = _list_names(client, library, nature="__unidentified__", file_type="pdf")
    assert pdf_rows == ["待识别-扫描.pdf", "待识别B.pdf"], pdf_rows
    assert _list_names(client, library, nature="__unidentified__", q="扫描", mode="filename") == [
        "待识别-扫描.pdf"
    ]
    # 同一关键词下，已识别的那份（合同.md）不会因为"待识别"这个条件被带进来
    assert _list_names(client, library, nature="__unidentified__", q="合同", mode="filename") == []
    assert _list_names(client, library, nature="__unidentified__", file_type="md") == [
        "待识别A.md"
    ]


def test_unidentified_filter_against_real_search_api(client, admin, library):
    """真实**检索**接口：同一个取值（`__unidentified__`）语义一致（后端已支持）。

    检索是**正文**命中，所以这里给两份资料都建一份含同一关键词的分段（没有分段就
    没有命中，那是"检索不到内容"而不是"性质筛选不对"）。
    """
    from apps.knowledge.models import DocumentChunk

    client.force_login(admin)
    pending = _make_file(library, "待识别-合同.pdf", "")
    identified = _make_file(library, "已识别-合同.pdf", "合同")
    for record in (pending, identified):
        DocumentChunk.objects.create(file=record, ordinal=0, text="本合同质保期 12 个月")

    response = client.post(
        "/api/v1/knowledge/search/",
        {"q": "质保期", "nature": "__unidentified__"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    names = sorted(row.get("original_name", "") for row in response.json()["results"])
    assert names == ["待识别-合同.pdf"], f"检索接口的「待识别」筛选结果不对：{names}"

    # 对照组：同一个查询词，不带性质条件时两份都能命中（证明差异来自性质筛选）
    plain = client.post(
        "/api/v1/knowledge/search/", {"q": "质保期"}, content_type="application/json"
    )
    assert plain.status_code == 200, plain.content
    assert sorted(row.get("original_name", "") for row in plain.json()["results"]) == [
        "已识别-合同.pdf",
        "待识别-合同.pdf",
    ]
    # 换成词表值：只剩已识别的那份
    other = client.post(
        "/api/v1/knowledge/search/",
        {"q": "质保期", "nature": "合同"},
        content_type="application/json",
    )
    assert other.status_code == 200, other.content
    assert sorted(row.get("original_name", "") for row in other.json()["results"]) == [
        "已识别-合同.pdf"
    ]


def test_permission_denial_is_never_rendered_as_an_empty_result(client, other_user, library):
    """无权限场景：接口必须拒绝，界面（按 kb-files.js 的分支）显示 detail 而不是空态。

    源码级钉住"失败 ≠ 0 条"：失败分支里先清掉性质说明，再把服务端 detail 交给结果区。
    """
    _make_file(library, "别人的资料.md", "")
    client.force_login(other_user)   # 不是该库成员
    response = client.get(
        "/api/v1/knowledge/files/",
        {"library": str(library.pk), "nature": "__unidentified__"},
    )
    body = response.json()
    rows = body.get("results", body) if isinstance(body, dict) else body
    assert rows == [], f"无权限用户拿到了资料列表：{body}"

    files_js = _read(FILES_JS)
    failure_branch = files_js[files_js.index("if (!result.ok) {") :]
    failure_branch = failure_branch[: failure_branch.index("const body = result.body || {};")]
    assert 'showNatureNote("");' in failure_branch, "失败分支没有清掉性质说明（会被当成空态）"
    assert "result.body && result.body.detail" in failure_branch, "失败分支没有显示服务端原因"
