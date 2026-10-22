"""``manage.py kbprobe``：**只读**探测外部模型服务配置与就绪状态。

设计口径（需求 4 + 用户 2026-10-13 指示）
-----------------------------------------
- 只做只读探测：绝不启停、绝不修改 AS711 上的任何服务；
- **默认真的不发探测请求**（AS711 断开时发探测只会白等超时），
  只打印配置；要真探测必须显式 ``--probe``；
- 探测失败就如实报失败，并给出熔断器状态，不把"连不上"说成"就绪"。

用法：

    ./.venv/Scripts/python.exe manage.py kbprobe
    ./.venv/Scripts/python.exe manage.py kbprobe --probe --profile bge1024
    ./.venv/Scripts/python.exe manage.py kbprobe --probe --service rerank --service ocr
    ./.venv/Scripts/python.exe manage.py kbprobe --json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.knowledge import ocr_selfcheck
from apps.knowledge.embedding import active_profile_name, get_profile, probe_dimension, profiles
from apps.knowledge.httpclient import environment_proxy_defect
from apps.knowledge.ocr import OcrClient
from apps.knowledge.rerank import RerankClient
from apps.knowledge.resilience import breaker_snapshots

#: OCR 探测样本里写的文字（= 固定样图的三行原文）。
#:
#: 为什么要写**明确文字**：探测必须能区分"服务挂了"与"发了张没法识别的图"。
#: 用可读文字做样本，才能把 HTTP 200 + 识别成功当作真的就绪。
#:
#: **样图来源已改为仓库里固定的、被真实模型验证过的 PNG**（独立复现的事故：
#: 旧实现在镜像里找不到字体 → 退回 PIL 内置位图字体 → 整行文字只有约 138×9 像素
#: → OCR 输出重复幻觉，看起来像"服务坏了"，其实是样本坏了）。判定关键词与
#: 渲染路径都在 ``apps/knowledge/ocr_selfcheck.py``。
OCR_PROBE_LINES = list(ocr_selfcheck.EXPECTED_LINES)


def _build_ocr_probe_png() -> bytes:
    """返回**固定自检样图**（1200×420、48px 中文，已对 AS711 实测识别正确）。

    不再"现场渲染"当唯一来源：渲染路径需要真正的中文字体，找不到就明确失败
    （``NoUsableFontError``），绝不退回不可读的内置位图字体。
    """
    return ocr_selfcheck.sample_bytes()


def _ocr_missing_needles(text: str) -> list[str]:
    """样图关键词里没识别出来的那些（空 = 全部识别正确）。"""
    return ocr_selfcheck.missing_needles(text)



class Command(BaseCommand):
    help = "只读探测嵌入/重排/OCR 服务；默认只打印配置，--probe 才真实调用"

    def add_arguments(self, parser):
        parser.add_argument(
            "--probe",
            action="store_true",
            help="真实调用一次（会受超时/重试/熔断约束）；不加则只看配置",
        )
        parser.add_argument(
            "--profile",
            action="append",
            default=[],
            help="要探测的嵌入 profile（可重复；默认只探测 active profile）",
        )
        parser.add_argument(
            "--service",
            action="append",
            default=[],
            choices=["embedding", "rerank", "ocr"],
            help="要探测的服务类别（可重复；默认全部）",
        )
        parser.add_argument("--timeout", type=float, default=20.0, help="单次探测超时（秒）")
        parser.add_argument("--json", action="store_true", help="以 JSON 输出")

    def handle(self, *args, **options):
        probe = bool(options["probe"])
        services = set(options["service"] or ["embedding", "rerank", "ocr"])
        wanted = options["profile"] or [active_profile_name()]

        report: dict = {
            "probe": probe,
            "active_profile": active_profile_name(),
            "config": {},
            "results": [],
            "breakers": breaker_snapshots(),
            "environment_warnings": [],
        }

        defect = environment_proxy_defect()
        if defect:
            report["environment_warnings"].append(defect)

        for name in sorted(profiles()):
            profile = profiles()[name]
            report["config"][name] = {
                "model": profile.model,
                "model_version": profile.model_version,
                "base_url": profile.base_url,
                "dimension": profile.dimension,
                "configured": profile.configured,
                "active": name == active_profile_name(),
                "note": profile.note,
            }

        if "embedding" in services:
            for name in wanted:
                profile = get_profile(name)
                entry = {
                    "service": "embedding",
                    "profile": name,
                    "model": profile.model,
                    "endpoint": profile.base_url,
                    "declared_dimension": profile.dimension,
                }
                if probe:
                    try:
                        result = probe_dimension(profile=profile, timeout=options["timeout"])
                        entry.update(
                            {
                                "ready": result["ready"],
                                "actual_dimension": result["actual_dimension"],
                                "error": result["error"],
                            }
                        )
                    except Exception as exc:  # noqa: BLE001 - 探测失败如实报
                        entry.update({"ready": False, "error": str(exc)})
                else:
                    entry["ready"] = None
                    entry["note"] = "未探测（加 --probe 才真实调用）"
                report["results"].append(entry)

        if "rerank" in services:
            client = RerankClient()
            entry = {
                "service": "rerank",
                "model": client.profile.model,
                "endpoint": client.profile.base_url,
                "configured": client.configured,
            }
            if probe and client.configured:
                try:
                    hits = client.rerank("质保期", ["质保期 12 个月", "无关内容"], top_n=2)
                    entry.update(
                        {
                            "ready": True,
                            "top_index": hits[0].index if hits else None,
                            "top_score": hits[0].score if hits else None,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    entry.update({"ready": False, "error": str(exc)})
            elif probe:
                entry.update({"ready": False, "error": "未配置端点"})
            else:
                entry["ready"] = None
                entry["note"] = "未探测（加 --probe 才真实调用）"
            report["results"].append(entry)

        if "ocr" in services:
            client = OcrClient()
            entry = {
                "service": "ocr",
                "model": client.profile.model,
                "endpoint": client.profile.base_url,
                "configured": client.configured,
            }
            if probe and client.configured:
                try:
                    png = _build_ocr_probe_png()
                    text = client.recognize_image(png, hint="探测")
                    # **断言确实识别出了文本**，而不是"HTTP 通了就算就绪"。
                    #
                    # 为什么要有这一步（实测踩到的假阴性）：早期探测发的是一张
                    # **1x1 白图**，PaddleOCR-VL 的图像处理器直接回 HTTP 400
                    # （输入太小/没有可识别内容），于是 kbprobe 报"OCR 不可用"——
                    # 而模型其实是好的（用 1100x360 的文本图能正确识别全部 3 行）。
                    # 所以探测样本必须**尺寸合理且含明确文字**，并把
                    # "有没有识别到预期文字"作为就绪判据。
                    # 判据：固定样图的**关键词**是否都被识别出来（OCR 可能在空格/
                    # 标点上与原文有差异，所以用关键词而不是整句）。
                    missing = _ocr_missing_needles(text)
                    expected = [
                        token
                        for token in ocr_selfcheck.EXPECTED_NEEDLES
                        if token not in missing
                    ]
                    entry.update(
                        {
                            "ready": not missing,
                            "sample_length": len(text),
                            "sample_size": len(png),
                            "expected_tokens_matched": expected,
                            "expected_tokens_missing": missing,
                            "sample_text": text[:200],
                        }
                    )
                    if missing:
                        entry["error"] = (
                            "模型有响应但没识别出这些关键词："
                            + "、".join(missing)
                            + "（固定样图已在真实服务上验证过可识别；"
                            "若持续缺失请检查端点是否真的是 OCR 服务）"
                        )
                except Exception as exc:  # noqa: BLE001
                    entry.update({"ready": False, "error": str(exc)})
            elif probe:
                entry.update({"ready": False, "error": "未配置端点"})
            else:
                entry["ready"] = None
                entry["note"] = "未探测（加 --probe 才真实调用）"
            report["results"].append(entry)

        if options["json"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            self.stdout.write(f"active profile：{report['active_profile']}")
            self.stdout.write(f"探测模式：{'真实调用' if probe else '仅看配置（加 --probe 才调用）'}")
            for warning in report["environment_warnings"]:
                self.stdout.write(self.style.WARNING(f"环境提示：{warning}"))
            self.stdout.write("")
            for name, info in report["config"].items():
                mark = "*" if info["active"] else " "
                self.stdout.write(
                    f"{mark} {name}: {info['model'] or '(未配置)'} @ "
                    f"{info['base_url'] or '(未配置端点)'} → {info['dimension']} 维"
                    f"（configured={info['configured']}）"
                )
            self.stdout.write("")
            for entry in report["results"]:
                if entry.get("ready") is True:
                    style = self.style.SUCCESS
                    verdict = "就绪"
                elif entry.get("ready") is False:
                    style = self.style.ERROR
                    verdict = "不可用"
                else:
                    style = self.style.WARNING
                    verdict = "未探测"
                extra = entry.get("error") or entry.get("note") or ""
                if entry["service"] == "ocr" and entry.get("ready") is True:
                    # 就绪时把"识别到的文字"也显示出来：这样"就绪"是有证据的，
                    # 不是"HTTP 通了"。
                    matched = entry.get("expected_tokens_matched") or []
                    extra = (
                        f"识别到预期文字 {matched}（样本 {entry.get('sample_size')} 字节）"
                    )
                self.stdout.write(
                    style(
                        f"[{verdict}] {entry['service']}"
                        f"{':' + entry.get('profile', '') if entry.get('profile') else ''} "
                        f"{entry.get('model') or ''} — {extra}"
                    )
                )
            if report["breakers"]:
                self.stdout.write("")
                self.stdout.write("熔断器状态：")
                for snapshot in report["breakers"]:
                    self.stdout.write(
                        f"  {snapshot['service']}: {snapshot['state']}"
                        f"（连续失败 {snapshot['consecutive_failures']}）"
                    )
            self.stdout.write("")
            self.stdout.write(
                "只读探测：不启停、不修改 AS711 上的任何服务（需求 4）。"
            )

        if probe and any(entry.get("ready") is False for entry in report["results"]):
            raise CommandError("有服务探测失败（见上面的不可用项）。这不会中断开发流程。")


__all__ = ["Command"]
