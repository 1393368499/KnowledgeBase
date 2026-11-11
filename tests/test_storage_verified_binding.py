"""阻断 3 的拒绝回归：verified 绑定被破坏时必须拒绝迁移。

覆盖：归档被换、清单被改、源路径不一致、绑定字段缺失、条目数不符。
这些校验发生在**任何 distro 操作之前**，所以用 NullController 即可安全测试。
"""

from __future__ import annotations


def _backup(data, backup):
    import storage_migrate as sm

    rb = sm.Rollback("KnowledgeBase-Runtime", 56333, 56339, service_control=False)
    ctl = sm.NullController()
    rb.controller = ctl
    sm.do_backup(data, backup, execute=True, rb=rb, service_control=False, controller=ctl)
    return rb


def _migrate(data, backup, target, expect_abort=True):
    import pytest

    import storage_migrate as sm

    rb = sm.Rollback("KnowledgeBase-Runtime", 56333, 56339, service_control=False)
    if expect_abort:
        with pytest.raises(sm.Abort):
            sm.do_migrate(data, backup, target, rb, execute=True, service_control=False)
    else:
        sm.do_migrate(data, backup, target, rb, execute=True, service_control=False)


def test_intact_verified_passes_precondition(fixture_source, monkeypatch):
    """未篡改时应通过**绑定**前置校验。

    两点修正（不弱化校验，只是让夹具独立）：
      1. 执行器修好后，夹具模式下复制+校验能**真正跑完**，"必然 Abort"的旧前提不成立；
         因此断言改为：**要么完成、要么因非绑定原因失败**，绝不允许"未篡改却被
         verified 绑定校验拒绝"。
      2. distro 层**显式替换为夹具** —— 本用例只关心绑定校验，不该去碰真实 WSL
         （既慢又会在真实 distro 里留下目录）。
    """
    import storage_migrate as sm

    data, backup = fixture_source
    _backup(data, backup)

    def fake_wsl(script, *values, check=True):  # noqa: ANN001, ARG001
        if script is sm.S_TARGET_STATE:
            return "absent"
        if script is sm.S_FINDMNT:
            return "ext4 /dev/fake"
        if script is sm.S_BULK_COPY:
            return "COPY_OK:0"
        if script is sm.S_BULK_HASH:
            return "MATCH:0/0\nEXTRA_COUNT:0"
        return "OK"

    monkeypatch.setattr(sm, "wsl", fake_wsl)
    rb = sm.Rollback("KnowledgeBase-Runtime", 56333, 56339, service_control=False)
    rb.controller = sm.NullController()
    try:
        sm.do_migrate(
            data, backup, "/var/lib/qdrant-fixture-bind-ok", rb,
            execute=True, service_control=False,
        )
    except sm.Abort as exc:
        assert "verified" not in str(exc), f"未篡改却被绑定校验拒绝：{exc}"


def test_replaced_archive_is_rejected(fixture_source):

    data, backup = fixture_source
    _backup(data, backup)
    archive = next(backup.glob("*.zip"))
    archive.write_bytes(archive.read_bytes() + b"tampered")
    _migrate(data, backup, "/var/lib/qdrant-fixture-t1")


def test_modified_manifest_is_rejected(fixture_source):
    data, backup = fixture_source
    _backup(data, backup)
    mf = backup / "manifest.json"
    doc = mf.read_text(encoding="utf-8").replace('"count"', '"count2"', 1)
    mf.write_text(doc, encoding="utf-8")
    _migrate(data, backup, "/var/lib/qdrant-fixture-t2")


def test_source_path_mismatch_is_rejected(fixture_source, tmp_path):
    data, backup = fixture_source
    _backup(data, backup)
    ver = backup / "manifest.verified.json"
    import json

    doc = json.loads(ver.read_text(encoding="utf-8"))
    doc["source_realpath"] = str(tmp_path)      # 换成别的源
    ver.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    _migrate(data, backup, "/var/lib/qdrant-fixture-t3")


def test_missing_binding_field_is_rejected(fixture_source):
    data, backup = fixture_source
    _backup(data, backup)          # ← 之前漏了这一步，文件当然不存在
    import json

    ver = backup / "manifest.verified.json"
    doc = json.loads(ver.read_text(encoding="utf-8"))
    doc.pop("archive_sha256", None)
    ver.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    _migrate(data, backup, "/var/lib/qdrant-fixture-t4")


def test_entry_count_mismatch_is_rejected(fixture_source):
    data, backup = fixture_source
    _backup(data, backup)          # ← 同上
    import json

    ver = backup / "manifest.verified.json"
    doc = json.loads(ver.read_text(encoding="utf-8"))
    doc["count"] = doc["count"] + 1             # 少项/多项都要拒绝
    ver.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    _migrate(data, backup, "/var/lib/qdrant-fixture-t5")
