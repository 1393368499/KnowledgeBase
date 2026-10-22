"""「待识别」性质筛选在**列表与检索两端**必须同一语义（用户 2026-10-16 要求）。

背景：前端曾用"筛出 0 条 + 库里有已识别资料"来**猜**后端不支持哨兵 —— 这个推断
不成立（全部识别成功时，正确结果恰好就是 0 条）。正确做法是后端显式支持同一哨兵：

* ``GET /api/v1/knowledge/files/?nature=__unidentified__`` → ``classification == ""``
* ``POST /api/v1/knowledge/search/``（结构化条件 ``nature``）→ 同一语义，不得 400
* 哨兵是**只读筛选值**，写入路径（上传）必须拒绝它

断言全部**以数据库为准**（不假设 fixture 的默认性质），这样"真实 0 条"与
"筛不出来"能被区分开：前者等于库里空性质的行集合，后者会与库不一致。
"""

from __future__ import annotations

import pytest

from apps.knowledge.models import IngestedFile
from apps.knowledge.nature import UNIDENTIFIED_SENTINEL

pytestmark = pytest.mark.django_db

FILES_URL = "/api/v1/knowledge/files/"
SEARCH_URL = "/api/v1/knowledge/search/"


def _blank_ids(library) -> set[int]:
    return set(
        IngestedFile._base_manager.filter(library=library, classification="")
        .values_list("pk", flat=True)
    )


def _list(api, user, query) -> set[int]:
    api.force_authenticate(user=user)
    response = api.get(f"{FILES_URL}?{query}")
    assert response.status_code == 200, response.content[:200]
    body = response.json()
    rows = body.get("results", body)
    return {row["id"] for row in rows}


def test_sentinel_matches_the_blank_rows_exactly(api, admin_user, library, make_file):
    """哨兵结果必须恰好等于"库里性质为空"的那批（不是 0 条，也不是全部）。

    用系统管理员断言：普通账号只能看到自己可读库里的行，那会把"权限过滤"
    混进"性质过滤"的对照里（权限本身另有用例覆盖）。
    """
    make_file(library, name="待识别-甲.txt", status="done")
    make_file(library, name="待识别-乙.txt", status="done")
    ids = _list(api, admin_user, f"nature={UNIDENTIFIED_SENTINEL}")
    expected = _blank_ids(library)
    assert expected, "前置条件：这个库里应当有性质为空的资料"
    assert ids == expected, f"哨兵结果与库中空性质集合不一致：{ids} != {expected}"


def test_a_library_without_blank_rows_returns_a_real_zero(api, user, library, make_file):
    """库里没有空性质资料时，正确结果就是**真实 0 条**（不能报错、也不能返回全部）。

    "已识别行"的构造走受控写入路径（性质的建/改受治理触发器与受控服务约束，
    测试里不绕过），因此这里只在**库里本来就没有空性质行**时验证 0 条语义；
    带已识别行的对照场景由分类套件
    (`tests/test_auto_classification.py::test_list_filter_and_display_agree`) 覆盖。
    """
    make_file(library, name="已识别-0.txt", status="done")
    if _blank_ids(library):
        pytest.skip("本库存在空性质行，无法在此验证全部已识别后的 0 条")
    api.force_authenticate(user=user)
    response = api.get(f"{FILES_URL}?nature={UNIDENTIFIED_SENTINEL}")
    assert response.status_code == 200, response.content[:200]
    body = response.json()
    rows = body.get("results", body)
    assert rows == [], "没有空性质资料时必须返回真实的 0 条"


def test_exact_nature_filter_excludes_blank_rows(api, user, library, make_file):
    """精确筛选只按值匹配：库里没有该值的资料时，结果里不能混进空性质行。"""
    make_file(library, name="混合-待识别.txt", status="done")
    blanks = _blank_ids(library)
    if not blanks:
        pytest.skip("本库没有空性质行，无法对照")
    report_ids = _list(api, user, "nature=报表")
    assert not (report_ids & blanks), "精确筛选把空性质也算进来了"


def test_search_endpoint_accepts_the_same_sentinel(api, user):
    """检索端不能因为哨兵报 400（结构化筛选走列表端点，两端口径必须一致）。"""
    api.force_authenticate(user=user)
    response = api.post(SEARCH_URL, {"q": "一致", "nature": UNIDENTIFIED_SENTINEL}, format="json")
    assert response.status_code == 200, response.content[:300]


def test_search_endpoint_still_rejects_a_bogus_nature(api, user):
    api.force_authenticate(user=user)
    response = api.post(SEARCH_URL, {"q": "合同", "nature": "技术文档"}, format="json")
    assert response.status_code == 400, response.content[:200]


def test_sentinel_is_rejected_by_the_upload_path(api, admin_user, library):
    """哨兵只是只读筛选值：上传时把它当性质必须 400（不能写进库）。"""
    from django.core.files.uploadedfile import SimpleUploadedFile

    api.force_authenticate(user=admin_user)
    response = api.post(
        FILES_URL + "upload/",
        {
            "file": SimpleUploadedFile("哨兵上传.txt", b"hello", content_type="text/plain"),
            "library": str(library.pk),
            "classification": UNIDENTIFIED_SENTINEL,
        },
        format="multipart",
    )
    assert response.status_code == 400, response.content[:200]
    assert not IngestedFile.objects.filter(original_name="哨兵上传.txt").exists()
