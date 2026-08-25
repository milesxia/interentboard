from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

BPLUS_HINTS = ("shanghai.gov.cn", "jfdaily.com")
B_HINTS = ("people.com.cn", "xinmin.cn", "wenhui.whb.cn")

MATERIAL_TERMS = (
    "项目建议书", "可行性研究", "可研批复", "批复", "征收决定", "征收范围", "补偿方案", "房屋调查",
    "测绘", "房地产价格评估", "施工许可证", "开工", "竣工", "完工", "通车", "开放", "投运", "贯通",
    "中标", "招标", "控规调整", "规划公示", "正式印发", "延期", "调整", "交通切换", "匝道启用",
)
WEAK_TERMS = ("群租", "消防检查", "垃圾分类", "普通巡查", "物业治理", "市容整治")

P_RANK = {f"P{i}": i for i in range(11)}
E_RANK = {f"E{i}": i for i in range(7)}
T_RANK = {f"T{i}": i for i in range(6)}


def source_grade(url: str, official_domains: list[str] | None = None) -> str:
    host = (urlparse(url).hostname or "").lower()
    if official_domains and any(host == d or host.endswith("." + d) for d in official_domains):
        return "A"
    if host.endswith(".gov.cn"):
        return "A"
    if any(h in host for h in BPLUS_HINTS):
        return "B+"
    if any(h in host for h in B_HINTS):
        return "B"
    return "C"


def material_score(text: str) -> int:
    score = sum(2 for term in MATERIAL_TERMS if term in text)
    score -= sum(2 for term in WEAK_TERMS if term in text)
    return score


def conservative_stage_hint(text: str) -> dict:
    result: dict[str, str] = {}
    # Engineering stage. Higher rules intentionally overwrite lower ones.
    if "项目建议书" in text and any(x in text for x in ("批复", "批准", "同意")):
        result["P"] = "P3"
    if ("可行性研究" in text or "可研" in text) and any(x in text for x in ("批复", "批准", "同意")):
        result["P"] = "P4"
    if any(x in text for x in ("设计方案公示", "建设工程规划许可证", "规划许可")):
        result["P"] = "P5"
    if any(x in text for x in ("中标", "施工合同", "合同公告")):
        result["P"] = "P6"
    if "施工许可证" in text or "正式开工" in text:
        result["P"] = "P7"
    if any(x in text for x in ("正在施工", "工程在建", "施工现场", "施工推进")):
        result["P"] = "P8"
    if any(x in text for x in ("竣工验收", "工程完工", "完成竣工")):
        result["P"] = "P9"
    if any(x in text for x in ("正式通车", "正式开放", "投入运营", "正式投运")):
        result["P"] = "P10"

    # Expropriation stage.
    if any(x in text for x in ("房屋调查", "调查测绘", "预评估")):
        result["E"] = "E1"
    if any(x in text for x in ("征收范围", "预公告", "意愿征询")):
        result["E"] = "E2"
    if "补偿方案" in text and any(x in text for x in ("征求意见", "正式方案")):
        result["E"] = "E3"
    if "房屋征收决定" in text or "征收决定" in text:
        result["E"] = "E4"
    if "房地产价格评估" in text and "征收" in text:
        result["E"] = "E5"
    if any(x in text for x in ("拆除完成", "完成交地", "净地")):
        result["E"] = "E6"
    return result


def extract_date_candidates(text: str, limit: int = 8) -> list[str]:
    found: list[str] = []
    patterns = [
        r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?",
        r"(20\d{2})年(\d{1,2})月",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text[:12000]):
            y, mo = int(m.group(1)), int(m.group(2))
            d = int(m.group(3)) if len(m.groups()) >= 3 and m.group(3) else 1
            try:
                s = datetime(y, mo, d).date().isoformat()
            except ValueError:
                continue
            if s not in found:
                found.append(s)
            if len(found) >= limit:
                return found
    return found


def _family(stage: str) -> tuple[str, dict[str, int]] | tuple[None, dict]:
    if stage in P_RANK:
        return "P", P_RANK
    if stage in E_RANK:
        return "E", E_RANK
    if stage in T_RANK:
        return "T", T_RANK
    return None, {}


def transition_is_safe(old_stage: str, new_stage: str, evidence: list[dict]) -> bool:
    fam_old, ranks = _family(old_stage)
    fam_new, _ = _family(new_stage)
    if not fam_old or fam_old != fam_new:
        return False
    if ranks[new_stage] <= ranks[old_stage]:
        return True
    # Automatic forward update is allowed only from A/B+ evidence and only up to rule hints.
    strong = [e for e in evidence if e.get("source_grade") in {"A", "B+"}]
    if not strong:
        return False
    if fam_new == "T":
        # T levels are experiential and must remain reviewable; never auto-promote.
        return False
    max_rank = ranks[old_stage]
    for e in strong:
        hint = conservative_stage_hint(e.get("excerpt", "")).get(fam_new)
        if hint in ranks:
            max_rank = max(max_rank, ranks[hint])
    return ranks[new_stage] <= max_rank
