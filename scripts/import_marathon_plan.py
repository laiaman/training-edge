#!/usr/bin/env python3
"""Import 22-week 2026 Marathon Plan (from vault/plans/2026_Marathon_Plan.md)
into TrainingEdge's training_plans + planned_workouts tables.

Excludes body-weight-related data per user request.
Only creates running + strength + rest entries.

Usage:
    python scripts/import_marathon_plan.py [--dry-run] [--clear]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import database


def _date_of_week_day(week_start: date, weekday: int) -> date:
    """week_start is Monday (weekday=0). Return the date for the given ISO weekday (0=Mon..6=Sun)."""
    return week_start + timedelta(days=weekday)


DAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6}
SOURCE_PLAN_PATH = Path(__file__).resolve().parents[2] / "vault" / "plans" / "2026_Marathon_Plan.md"


def _clean_md_cell(value: str) -> str:
    value = re.sub(r"[*_`]", "", value or "")
    return re.sub(r"\s+", " ", value).strip()


def _parse_markdown_rows(start_marker: str, end_marker: str) -> list[dict]:
    """Read lossless workout prescriptions from the approved Markdown tables."""
    text = SOURCE_PLAN_PATH.read_text(encoding="utf-8")
    section = text.split(start_marker, 1)[1].split(end_marker, 1)[0]
    rows: list[dict] = []
    current_week = ""
    last_workout: dict | None = None
    section_start_line = text[: text.index(start_marker)].count("\n")
    for offset, line in enumerate(section.splitlines(), 1):
        if not line.lstrip().startswith("|"):
            continue
        cells = [_clean_md_cell(cell) for cell in line.strip().strip("|").split("|")]
        if len(cells) < 7 or cells[0] in {"---", "周次"}:
            continue
        week_match = re.search(r"W(\d+)(-B)?", cells[0])
        if week_match:
            current_week = f"W{week_match.group(1)}{'-B' if week_match.group(2) else ''}"
        day_match = re.match(r"([一二三四五六日])(?:\s|$|-)", cells[1])
        if current_week and day_match and cells[2] and "周总量" not in cells[2]:
            last_workout = {
                "week_label": current_week,
                "day": day_match.group(1),
                "title": cells[2],
                "distance": cells[3],
                "pace": cells[4],
                "hr": cells[5],
                "rpe": cells[6],
                "source_line": section_start_line + offset,
                "coach_notes": [],
            }
            rows.append(last_workout)
            continue
        note = cells[2] if len(cells) > 2 else ""
        if last_workout and any(marker in note for marker in ("执行建议", "桥梁课目的", "安全截断", "降低赛前3周")):
            last_workout["coach_notes"].append(note)
    return rows


def _parse_hr(value: str) -> tuple[int | None, int | None]:
    numbers = [int(number) for number in re.findall(r"\d+", value or "")]
    if not numbers:
        return None, None
    if "<" in value or "≤" in value:
        return None, numbers[0]
    if ">" in value or "≥" in value:
        return numbers[0], None
    if len(numbers) >= 2:
        return numbers[0], numbers[1]
    return numbers[0], numbers[0]


def _parse_rpe(value: str) -> tuple[float | None, float | None]:
    numbers = [float(number) for number in re.findall(r"\d+(?:\.\d+)?", value or "")]
    if not numbers:
        return None, None
    return numbers[0], numbers[1] if len(numbers) > 1 else numbers[0]


def _parse_distance(value: str) -> float | None:
    match = re.search(r"~?\s*(\d+(?:\.\d+)?)\s*km", value or "", re.IGNORECASE)
    return float(match.group(1)) if match else None


def _intensity_from_title(title: str) -> str:
    lowered = title.lower()
    if "马拉松" in title or "🏁" in title:
        return "race"
    if "恢复" in title or "散步" in title or "极轻松" in title:
        return "recovery"
    if "cv" in lowered or "间歇" in title or "冲刺" in title:
        return "threshold"
    if "m-pace" in lowered or "节奏" in title:
        return "tempo"
    if "进展" in title or "mantz" in lowered:
        return "moderate"
    return "easy"


def _prescription_steps(row: dict) -> list[dict]:
    text = row["pace"]
    complex_markers = ("×", "组休", "恢复", "前", "后", "末", "主段", "快段", "+")
    if not any(marker in f"{row['distance']} {text}" for marker in complex_markers):
        return []
    pieces = [piece.strip() for piece in re.split(r"\s+(?=(?:后|末|快段|主段|恢复|组休|M-Pace:))|\s+\+\s+", text) if piece.strip()]
    return [
        {
            "order": index,
            "instruction": piece,
            "structure": row["distance"] if index == 1 else None,
        }
        for index, piece in enumerate(pieces or [text], 1)
    ]


def _enrich_workout(workout: dict, row: dict) -> None:
    hr_min, hr_max = _parse_hr(row["hr"])
    rpe_min, rpe_max = _parse_rpe(row["rpe"])
    workout.update({
        "target_pace_text": row["pace"],
        "target_hr_text": row["hr"],
        "target_hr_min": hr_min,
        "target_hr_max": hr_max,
        "target_rpe_min": rpe_min,
        "target_rpe_max": rpe_max,
        "target_rpe": rpe_min if rpe_min == rpe_max else None,
        "target_duration_source": "estimated",
        "target_tss_source": "estimated",
        "source_reference": f"vault/plans/2026_Marathon_Plan.md:{row['source_line']}",
    })
    steps = _prescription_steps(row)
    if steps:
        workout["steps"] = steps
    notes = row.get("coach_notes") or []
    if notes:
        workout["coach_note"] = "\n".join(notes)
        safety = next((note for note in notes if "安全截断" in note), None)
        if safety:
            workout["safety_cutoff"] = safety


def _enrich_primary_plan(workouts: list[dict]) -> None:
    rows = _parse_markdown_rows("### 基础期 W1-W5", "#### 场景B：")
    indexed = {(row["week_label"], row["day"]): row for row in rows if "-B" not in row["week_label"]}
    matched = 0
    for workout in workouts:
        if workout.get("sport") != "running":
            workout["target_duration_source"] = "source" if workout.get("target_duration_min") is not None else None
            workout["target_tss_source"] = "estimated" if workout.get("target_tss") is not None else None
            if workout.get("sport") == "training":
                workout["steps"] = [
                    {"order": 1, "instruction": "深跳 3×4（神经激活）"},
                    {"order": 2, "instruction": "轻重量RDL 3×6（<50% 1RM，RIR 3-4）"},
                    {"order": 3, "instruction": "保加利亚分腿蹲 2×8/腿（轻重量）"},
                    {"order": 4, "instruction": "提踵超级组 3组"},
                    {"order": 5, "instruction": "引体向上 2×次极限"},
                    {"order": 6, "instruction": "RKC平板支撑 3×30s"},
                ]
                workout["source_reference"] = "vault/plans/2026_Marathon_Plan.md:396"
            continue
        day = "一二三四五六日"[date.fromisoformat(workout["date"]).weekday()]
        row = indexed.get((workout["week_label"], day))
        if row is None:
            raise ValueError(f"Markdown 中找不到处方: {workout['week_label']} {day} {workout['title']}")
        _enrich_workout(workout, row)
        matched += 1
    if matched != 96:
        raise ValueError(f"跑步处方匹配不完整: {matched}/96")


def build_alternate_scenario() -> list[dict]:
    """Return the inactive W16-B..W22-B branch without adding it to the calendar."""
    week_starts = {f"W{week}-B": date(2026, 9, 21) + timedelta(weeks=week - 16) for week in range(16, 23)}
    rows = _parse_markdown_rows("#### 场景B：", "## 5. 跑量趋势")
    workouts: list[dict] = []
    for position, row in enumerate(rows, 1):
        monday = week_starts[row["week_label"]]
        distance_km = _parse_distance(row["distance"])
        intensity = _intensity_from_title(row["title"])
        pace_map = {"recovery": 340, "easy": 320, "moderate": 280, "tempo": 250, "threshold": 230, "race": 248}
        pace = pace_map[intensity]
        workout = {
            "uid": f"{row['week_label'].lower()}-{(monday + timedelta(days=DAY_MAP[row['day']])).isoformat()}-running-{position:02d}",
            "date": (monday + timedelta(days=DAY_MAP[row["day"]])).isoformat(),
            "week_label": row["week_label"],
            "phase": "南昌备选路径",
            "sport": "running",
            "title": f"[{row['week_label']}] {row['title']}",
            "description": f"{row['distance']} {row['pace']} HR {row['hr']}",
            "target_distance_km": distance_km,
            "target_duration_min": round(distance_km * pace / 60.0, 0) if distance_km is not None else None,
            "target_tss": _estimate_tss_running(distance_km, pace, intensity) if distance_km is not None else None,
            "target_intensity": intensity,
        }
        _enrich_workout(workout, row)
        workouts.append(workout)
    if len(workouts) != 32:
        raise ValueError(f"场景B处方匹配不完整: {len(workouts)}/32")
    return workouts


def _estimate_tss_running(distance_km: float, pace_sec_per_km: float, intensity: str) -> float:
    """Rough TRIMP-based TSS estimate for running (no power meter)."""
    base_map = {
        "recovery": 35,
        "easy": 50,
        "moderate": 65,
        "tempo": 75,
        "threshold": 85,
        "interval": 90,
        "race": 100,
    }
    base_per_hour = base_map.get(intensity, 55)
    duration_h = (distance_km * pace_sec_per_km) / 3600.0
    return round(base_per_hour * duration_h, 1)


def _build_plan() -> list[dict]:
    """Return all planned workouts as dicts ready for DB insertion."""
    workouts = []

    def add(week_label: str, phase: str, week_monday: date, day_cn: str,
            sport: str, title: str, description: str,
            distance_km: float | None, target_duration_min: float | None,
            intensity: str, target_tss: float | None = None):
        weekday = DAY_MAP[day_cn]
        d = _date_of_week_day(week_monday, weekday)

        if target_tss is None and distance_km and intensity:
            pace_map = {
                "recovery": 340, "easy": 320, "moderate": 280,
                "tempo": 250, "threshold": 230, "interval": 215, "race": 248,
            }
            pace = pace_map.get(intensity, 310)
            target_tss = _estimate_tss_running(distance_km, pace, intensity)

        if target_duration_min is None and distance_km:
            pace_map = {
                "recovery": 340, "easy": 320, "moderate": 280,
                "tempo": 250, "threshold": 230, "interval": 215, "race": 248,
            }
            pace = pace_map.get(intensity, 310)
            target_duration_min = round(distance_km * pace / 60.0, 0)

        workouts.append({
            "date": d.isoformat(),
            "sport": sport,
            "title": f"[{week_label}] {title}",
            "description": description,
            "target_distance_km": distance_km,
            "target_duration_min": target_duration_min,
            "target_tss": target_tss,
            "target_intensity": intensity,
            "week_label": week_label,
            "phase": phase,
        })

    # =========================================================================
    # 基础期 W1-W5 (6.08 - 7.12) — 每周4练
    # =========================================================================

    # W1: 06.08 (Mon) - 06.14
    w = date(2026, 6, 8)
    add("W1", "基础期", w, "二", "running", "轻松有氧", "8km Z2 5'15\"-5'40\"/km HR<140", 8, None, "easy")
    add("W1", "基础期", w, "三", "running", "轻法特莱克", "12km 含10×1min快段@3'50\" 慢段5'10\"", 12, None, "moderate")
    add("W1", "基础期", w, "五", "running", "轻松有氧", "8km Z2 5'15\"-5'40\"/km HR<140", 8, None, "easy")
    add("W1", "基础期", w, "日", "running", "LSD", "18km 5'00\"-5'20\"/km HR 125-135", 18, None, "easy")

    # W2: 06.15 - 06.21
    w = date(2026, 6, 15)
    add("W2", "基础期", w, "二", "running", "轻松有氧", "10km Z2 5'15\"-5'40\"/km HR<140", 10, None, "easy")
    add("W2", "基础期", w, "三", "running", "CV间歇（入门）", "11km 含5×1000m@3'52\"-3'57\" 组休2min30s HR 160-168", 11, None, "threshold")
    add("W2", "基础期", w, "五", "running", "轻松有氧", "8km Z2 5'15\"-5'40\"/km HR<140", 8, None, "easy")
    add("W2", "基础期", w, "日", "running", "LSD", "20km 5'00\"-5'15\"/km HR 125-135", 20, None, "easy")

    # W3: 06.22 - 06.28
    w = date(2026, 6, 22)
    add("W3", "基础期", w, "二", "running", "轻松有氧", "10km Z2 5'15\"-5'40\"/km HR<140", 10, None, "easy")
    add("W3", "基础期", w, "三", "running", "有氧短间歇", "10km 含10×400m@3'40\"-3'45\" 恢复200m慢跑 HR 155-165", 10, None, "threshold")
    add("W3", "基础期", w, "五", "running", "轻松有氧", "10km Z2 5'15\"-5'40\"/km HR<140", 10, None, "easy")
    add("W3", "基础期", w, "日", "running", "渐进LSD", "22km 前17km 5'10\"-5'20\" 后5km渐进到4'50\" HR 125-140", 22, None, "easy")

    # W4: 06.29 - 07.05
    w = date(2026, 6, 29)
    add("W4", "基础期", w, "二", "running", "轻松有氧", "10km Z2 5'15\"-5'40\"/km HR<140", 10, None, "easy")
    add("W4", "基础期", w, "三", "running", "进展法特莱克", "14km 含6×2min快@4'00\"-4'05\" 恢复2min HR 150-160", 14, None, "moderate")
    add("W4", "基础期", w, "五", "running", "轻松有氧", "10km Z2 5'15\"-5'40\"/km HR<140", 10, None, "easy")
    add("W4", "基础期", w, "日", "running", "渐进LSD", "24km 前16km 5'00\"-5'10\" 后8km渐进到4'45\" HR 125-142", 24, None, "easy")

    # W5: 07.06 - 07.12 (恢复周)
    w = date(2026, 7, 6)
    add("W5", "基础期", w, "二", "running", "恢复跑", "8km 5'20\"-5'40\"/km HR<130", 8, None, "recovery")
    add("W5", "基础期", w, "三", "running", "轻跑+冲刺", "8km+4×100m冲刺 5'10\"-5'30\"/km HR<135", 8, None, "easy")
    add("W5", "基础期", w, "五", "running", "恢复跑", "6km 5'20\"-5'40\"/km HR<130", 6, None, "recovery")
    add("W5", "基础期", w, "日", "running", "中距离轻松", "16km 5'00\"-5'15\"/km HR 125-135", 16, None, "easy")

    # =========================================================================
    # 强化期 W6-W11 (7.13 - 8.23) — 每周5练
    # =========================================================================

    # W6: 07.13 - 07.19
    w = date(2026, 7, 13)
    add("W6", "强化期", w, "二", "running", "轻松有氧", "6km Z2 5'15\"-5'40\"/km HR<140", 6, None, "easy")
    add("W6", "强化期", w, "三", "running", "连续节奏跑", "12km 含8km@4'30\"-4'35\" HR 145-155", 12, None, "tempo")
    add("W6", "强化期", w, "五", "running", "轻松有氧", "10km Z2 5'15\"-5'40\"/km HR<140", 10, None, "easy")
    add("W6", "强化期", w, "六", "running", "恢复跑", "5km 5'20\"-5'40\"/km HR<130", 5, None, "recovery")
    add("W6", "强化期", w, "日", "running", "LSD", "25km 4'50\"-5'10\"/km HR 130-140", 25, None, "easy")

    # W7: 07.20 - 07.26
    w = date(2026, 7, 20)
    add("W7", "强化期", w, "二", "running", "轻松有氧", "10km Z2 5'15\"-5'40\"/km HR<140", 10, None, "easy")
    add("W7", "强化期", w, "三", "running", "CV间歇（入门）", "12km 含6×1000m@3'50\"-3'55\" 组休2min-2min30s HR 160-168", 12, None, "threshold")
    add("W7", "强化期", w, "五", "running", "轻松有氧", "12km Z2 5'15\"-5'40\"/km HR<140", 12, None, "easy")
    add("W7", "强化期", w, "六", "running", "恢复跑", "8km 5'20\"-5'40\"/km HR<130", 8, None, "recovery")
    add("W7", "强化期", w, "日", "running", "进展长距离", "26km 前23k@4'50\" 末3k@4'20\"-4'25\" HR 130-155", 26, None, "moderate")

    # W8: 07.27 - 08.02
    w = date(2026, 7, 27)
    add("W8", "强化期", w, "二", "running", "轻松有氧", "10km Z2 5'15\"-5'40\"/km HR<140", 10, None, "easy")
    add("W8", "强化期", w, "三", "running", "CV间歇（进阶）", "13km 含7×1000m@3'50\"-3'55\" 组休2min HR 160-168", 13, None, "threshold")
    add("W8", "强化期", w, "五", "running", "轻松有氧", "12km Z2 5'15\"-5'40\"/km HR<140", 12, None, "easy")
    add("W8", "强化期", w, "六", "running", "恢复跑", "8km 5'20\"-5'40\"/km HR<130", 8, None, "recovery")
    add("W8", "强化期", w, "日", "running", "进展长距离", "28km 前23k@4'50\" 末5k@4'20\"-4'25\" HR 130-155", 28, None, "moderate")

    # W9: 08.03 - 08.09
    w = date(2026, 8, 3)
    add("W9", "强化期", w, "二", "running", "轻松有氧", "10km Z2 5'15\"-5'40\"/km HR<140", 10, None, "easy")
    add("W9", "强化期", w, "三", "running", "CV巡航", "12km 含3×2000m@3'55\"-4'00\" 组休2.5-3min HR 160-168", 12, None, "threshold")
    add("W9", "强化期", w, "五", "running", "轻松有氧", "12km Z2 5'15\"-5'40\"/km HR<140", 12, None, "easy")
    add("W9", "强化期", w, "六", "running", "恢复跑", "8km 5'20\"-5'40\"/km HR<130", 8, None, "recovery")
    add("W9", "强化期", w, "日", "running", "LSD", "30km 4'45\"-5'00\"/km HR 130-145", 30, None, "easy")

    # W10: 08.10 - 08.16 (恢复周)
    w = date(2026, 8, 10)
    add("W10", "强化期", w, "二", "running", "恢复跑", "8km 5'20\"-5'40\"/km HR<130", 8, None, "recovery")
    add("W10", "强化期", w, "三", "running", "轻跑+冲刺", "8km+4×15s冲刺 5'10\"-5'30\"/km HR<135", 8, None, "easy")
    add("W10", "强化期", w, "五", "running", "恢复跑", "8km 5'20\"-5'40\"/km HR<130", 8, None, "recovery")
    add("W10", "强化期", w, "日", "running", "轻松长跑", "20km 5'00\"-5'10\"/km HR 125-135", 20, None, "easy")

    # W11: 08.17 - 08.23
    w = date(2026, 8, 17)
    add("W11", "强化期", w, "二", "running", "轻松有氧", "10km Z2 5'15\"-5'40\"/km HR<140", 10, None, "easy")
    add("W11", "强化期", w, "三", "running", "CV间歇（顶峰）", "14km 含8×1000m@3'48\"-3'53\" 前2组组休2min 后6组90s HR 160-168", 14, None, "threshold")
    add("W11", "强化期", w, "五", "running", "轻松有氧+冲刺", "12km+4×100m 5'15\"-5'40\"/km HR<140", 12, None, "easy")
    add("W11", "强化期", w, "六", "running", "恢复跑", "8km 5'20\"-5'40\"/km HR<130", 8, None, "recovery")
    add("W11", "强化期", w, "日", "running", "进展长距离", "30km 前22k@4'45\" 末8k@4'15\"-4'20\" HR 130-160", 30, None, "moderate")

    # =========================================================================
    # 专项期 W12-W16 (8.24 - 9.27) — 每周5练
    # =========================================================================

    # W12: 08.24 - 08.30
    w = date(2026, 8, 24)
    add("W12", "专项期", w, "二", "running", "轻松有氧", "10km 5'10\"-5'30\"/km HR<135", 10, None, "easy")
    add("W12", "专项期", w, "三", "running", "CV→M-Pace桥梁课", "~14km 4×1000m@CV 3'50\"+6km@M-Pace 4'10\" HR 155-165", 14, None, "threshold")
    add("W12", "专项期", w, "五", "running", "轻松有氧", "12km 5'10\"-5'30\"/km HR<135", 12, None, "easy")
    add("W12", "专项期", w, "六", "running", "恢复跑", "10km 5'15\"-5'30\"/km HR<130", 10, None, "recovery")
    add("W12", "专项期", w, "日", "running", "进展长跑", "32km 前24k@4'45\" 末8k@4'10\" HR 130-160", 32, None, "moderate")

    # W13: 08.31 - 09.06
    w = date(2026, 8, 31)
    add("W13", "专项期", w, "二", "running", "轻松有氧", "10km 5'10\"-5'30\"/km HR<135", 10, None, "easy")
    add("W13", "专项期", w, "三", "running", "M-Pace节奏跑", "14km 含12km@4'10\" HR 150-160", 14, None, "tempo")
    add("W13", "专项期", w, "五", "running", "轻松恢复", "12km 5'15\"-5'30\"/km HR<130", 12, None, "easy")
    add("W13", "专项期", w, "六", "running", "恢复跑", "10km 5'15\"-5'30\"/km HR<130", 10, None, "recovery")
    add("W13", "专项期", w, "日", "running", "Mantz长跑", "32km 前22k@4'40\" 后10k@4'10\" HR 135-160", 32, None, "moderate")

    # W14: 09.07 - 09.13
    w = date(2026, 9, 7)
    add("W14", "专项期", w, "二", "running", "轻松有氧", "10km 5'10\"-5'30\"/km HR<135", 10, None, "easy")
    add("W14", "专项期", w, "三", "running", "M-Pace交替跑", "14km 4×2.5km@4'10\"-4'13\" 恢复1km慢 HR 155-165", 14, None, "tempo")
    add("W14", "专项期", w, "五", "running", "轻松恢复", "12km 5'15\"-5'30\"/km HR<130", 12, None, "easy")
    add("W14", "专项期", w, "六", "running", "恢复跑", "10km 5'15\"-5'30\"/km HR<130", 10, None, "recovery")
    add("W14", "专项期", w, "日", "running", "Mantz长跑", "34km 前22k@4'40\" 后12k@4'10\" HR 135-162 ⚠️30km处HR>162或跟腱异样则降至4'30\"", 34, None, "tempo")

    # W15: 09.14 - 09.20 (决策周)
    w = date(2026, 9, 14)
    add("W15", "专项期", w, "二", "running", "恢复跑", "8km 5'20\"-5'40\"/km HR<130", 8, None, "recovery")
    add("W15", "专项期", w, "三", "running", "轻度节奏评估", "10km@4'30\" 观察HR/体感 HR 140-150", 10, None, "moderate")
    add("W15", "专项期", w, "五", "running", "恢复跑", "8km 5'20\"-5'40\"/km HR<130", 8, None, "recovery")
    add("W15", "专项期", w, "日", "running", "中距离轻松", "24km 4'50\"-5'00\"/km HR 130-140", 24, None, "easy")

    # W16: 09.21 - 09.27 (巅峰周)
    w = date(2026, 9, 21)
    add("W16", "专项期", w, "二", "running", "轻松有氧", "12km 5'10\"-5'30\"/km HR<135", 12, None, "easy")
    add("W16", "专项期", w, "三", "running", "M-Pace节奏（巅峰）", "17km 含15km@4'10\" HR 150-162", 17, None, "tempo")
    add("W16", "专项期", w, "五", "running", "轻松有氧", "12km 5'10\"-5'30\"/km HR<135", 12, None, "easy")
    add("W16", "专项期", w, "六", "running", "恢复跑+补给演练", "10km 5'15\"-5'30\"/km HR<130 全装备补给测试", 10, None, "recovery")
    add("W16", "专项期", w, "日", "running", "Mantz长跑（巅峰）", "32km 前20k@4'40\" 后12k@4'08\" HR 135-165", 32, None, "tempo")

    # =========================================================================
    # 北京减量期 W17-W19 (9.28 - 10.18)
    # =========================================================================

    # W17: 09.28 - 10.04 (75%负荷)
    w = date(2026, 9, 28)
    add("W17", "减量期", w, "二", "running", "轻松有氧", "8km 5'10\"-5'30\"/km HR<135", 8, None, "easy")
    add("W17", "减量期", w, "三", "running", "M-Pace维持", "12km 含4×2km@4'10\" 组休2min HR 150-160", 12, None, "tempo")
    add("W17", "减量期", w, "五", "running", "轻松有氧", "8km 5'10\"-5'30\"/km HR<135", 8, None, "easy")
    add("W17", "减量期", w, "六", "running", "恢复跑", "8km 5'20\"-5'40\"/km HR<130", 8, None, "recovery")
    add("W17", "减量期", w, "日", "running", "缩减长跑", "24km 前21k@4'50\" 末3k@4'10\" HR 130-155", 24, None, "moderate")

    # W18: 10.05 - 10.11 (50%负荷)
    w = date(2026, 10, 5)
    add("W18", "减量期", w, "二", "running", "轻松有氧", "6km 5'10\"-5'30\"/km HR<135", 6, None, "easy")
    add("W18", "减量期", w, "三", "running", "唤醒间歇", "8km 含6×400m@3'40\" 组休90s HR 155-165", 8, None, "interval")
    add("W18", "减量期", w, "五", "running", "恢复跑", "6km 5'20\"-5'40\"/km HR<130", 6, None, "recovery")
    add("W18", "减量期", w, "日", "running", "赛前拉练（全装备）", "16km 均匀4'20\"/km HR 140-150", 16, None, "moderate")

    # W19: 10.12 - 10.18 (赛周)
    w = date(2026, 10, 12)
    add("W19", "减量期", w, "二", "running", "赛前神经唤醒", "6km 含3×1km@4'08\"-4'10\" 慢段轻松 HR 150-160", 6, None, "moderate")
    add("W19", "减量期", w, "四", "running", "极轻松慢跑", "4km 5'30\"-6'00\"/km HR<125", 4, None, "recovery")
    add("W19", "减量期", w, "五", "running", "散步", "3km 自由配速 HR<120", 3, None, "recovery")
    add("W19", "减量期", w, "日", "running", "🏁 北京马拉松", "42.195km A目标: 4'08\"/km (2:55) B目标: 4'13\"/km (2:58)", 42.195, None, "race")

    # =========================================================================
    # 赛后过渡期 W20-W22 场景A (10.19 - 11.08)
    # =========================================================================

    # W20: 10.19 - 10.25 (恢复)
    w = date(2026, 10, 19)
    add("W20", "赛后过渡", w, "一", "rest", "完全休息", "完全休息/散步", None, 0, "recovery", 0)
    add("W20", "赛后过渡", w, "二", "rest", "完全休息", "完全休息/散步", None, 0, "recovery", 0)
    add("W20", "赛后过渡", w, "三", "rest", "完全休息", "完全休息/散步", None, 0, "recovery", 0)
    add("W20", "赛后过渡", w, "四", "running", "极轻松慢跑", "5km 5'40\"-6'00\"/km HR<120", 5, None, "recovery")
    add("W20", "赛后过渡", w, "六", "running", "轻松有氧", "8km 5'20\"-5'40\"/km HR<130", 8, None, "recovery")

    # W21: 10.26 - 11.01 (唤醒)
    w = date(2026, 10, 26)
    add("W21", "赛后过渡", w, "二", "running", "轻松有氧", "6km 5'20\"-5'40\"/km HR<130", 6, None, "easy")
    add("W21", "赛后过渡", w, "三", "running", "轻法特莱克", "8km 含6×30s快@3'50\" 充分恢复 HR 140-155", 8, None, "moderate")
    add("W21", "赛后过渡", w, "五", "running", "轻松有氧", "6km 5'20\"-5'40\"/km HR<130", 6, None, "easy")
    add("W21", "赛后过渡", w, "日", "running", "轻松中距离", "14km 4'40\"-5'00\"/km HR 130-140", 14, None, "easy")

    # W22: 11.02 - 11.08 (赛周)
    w = date(2026, 11, 2)
    add("W22", "赛后过渡", w, "二", "running", "M-Pace唤醒", "6km 含3×1km@4'10\" 放松 HR 145-155", 6, None, "moderate")
    add("W22", "赛后过渡", w, "四", "running", "极轻松慢跑", "4km 5'30\"-6'00\"/km HR<125", 4, None, "recovery")
    add("W22", "赛后过渡", w, "五", "running", "散步", "3km 自由配速 HR<120", 3, None, "recovery")
    add("W22", "赛后过渡", w, "日", "running", "🏁 南昌马拉松（佛系）", "42.195km 均匀4'15\"-4'20\"/km 自然心率", 42.195, None, "race")

    # =========================================================================
    # 力量训练 (每周1次, 周四, W1-W16)
    # =========================================================================
    strength_phases = [
        # (week_label, monday, phase, duration_min, description)
        ("W1", date(2026, 6, 8), "基础期", 45, "中等强度维持力量课"),
        ("W2", date(2026, 6, 15), "基础期", 45, "中等强度维持力量课"),
        ("W3", date(2026, 6, 22), "基础期", 45, "中等强度维持力量课"),
        ("W4", date(2026, 6, 29), "基础期", 45, "中等强度维持力量课"),
        ("W5", date(2026, 7, 6), "基础期", 45, "恢复周 轻量力量"),
        ("W6", date(2026, 7, 13), "强化期", 40, "60-65% 1RM 不力竭 维持力量课"),
        ("W7", date(2026, 7, 20), "强化期", 40, "60-65% 1RM 不力竭 维持力量课"),
        ("W8", date(2026, 7, 27), "强化期", 40, "60-65% 1RM 不力竭 维持力量课"),
        ("W9", date(2026, 8, 3), "强化期", 40, "60-65% 1RM 不力竭 维持力量课"),
        ("W10", date(2026, 8, 10), "强化期", 40, "恢复周 轻量力量"),
        ("W11", date(2026, 8, 17), "强化期", 40, "60-65% 1RM 不力竭 维持力量课"),
        ("W12", date(2026, 8, 24), "专项期", 35, "减量保爆发 力量维持"),
        ("W13", date(2026, 8, 31), "专项期", 35, "减量保爆发 力量维持"),
        ("W14", date(2026, 9, 7), "专项期", 35, "减量保爆发 力量维持"),
        ("W15", date(2026, 9, 14), "专项期", 35, "决策周 轻量力量"),
        ("W16", date(2026, 9, 21), "专项期", 35, "巅峰周 最后一次力量课"),
    ]
    for wl, monday, phase, dur, desc in strength_phases:
        exercises = [
            {"name": "深跳", "sets": 3, "reps": 4, "notes": "神经激活"},
            {"name": "轻重量RDL", "sets": 3, "reps": 6, "notes": "<50% 1RM RIR 3-4"},
            {"name": "保加利亚分腿蹲", "sets": 2, "reps": "8/腿", "notes": "轻重量"},
            {"name": "提踵超级组", "sets": 3, "reps": 15, "notes": "离心3-4秒"},
            {"name": "引体向上", "sets": 2, "reps": "次极限"},
            {"name": "RKC平板支撑", "sets": 3, "reps": "30s"},
        ]
        tss_est = round(dur * 0.6, 1)
        add(wl, phase, monday, "四", "training",
            "力量维持课",
            f"{desc}\n深跳3×4 + RDL3×6 + 分腿蹲2×8 + 提踵3×15 + 引体2×max + 平板支撑3×30s",
            None, dur, "moderate", tss_est)

    _enrich_primary_plan(workouts)
    return workouts


def main():
    parser = argparse.ArgumentParser(description="Import marathon plan into TrainingEdge")
    parser.add_argument("--dry-run", action="store_true", help="Print workouts without writing to DB")
    parser.add_argument("--clear", action="store_true", help="Clear existing planned workouts before import")
    args = parser.parse_args()

    database.init_db()
    workouts = _build_plan()

    if args.dry_run:
        print(f"Total workouts: {len(workouts)}")
        for w in workouts:
            print(f"  {w['date']} | {w['sport']:>10} | {w['title']:<40} | TSS={w.get('target_tss', '-'):>5} | {w.get('target_duration_min', '-')}min")
        return

    with database.get_db() as conn:
        # Create plan entry
        plan_start = "2026-06-08"
        plan_end = "2026-11-08"
        plan_name = "2026北京/南昌双赛马拉松备战 (目标2:55)"

        existing = conn.execute(
            "SELECT id FROM training_plans WHERE name = ?", (plan_name,)
        ).fetchone()

        if existing:
            plan_id = existing["id"]
            print(f"Found existing plan (id={plan_id}), updating...")
        else:
            conn.execute(
                """INSERT INTO training_plans (name, start_date, end_date, phase, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                (plan_name, plan_start, plan_end, "全周期",
                 "22周备战计划：基础期W1-5 → 强化期W6-11 → 专项期W12-16 → 减量W17-19 → 过渡W20-22"),
            )
            plan_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            print(f"Created plan: {plan_name} (id={plan_id})")

        if args.clear:
            deleted = conn.execute(
                "DELETE FROM planned_workouts WHERE plan_id = ?", (plan_id,)
            ).rowcount
            print(f"Cleared {deleted} existing workouts for plan {plan_id}")

        # Delete existing workouts in the plan date range to avoid duplicates
        conn.execute(
            "DELETE FROM planned_workouts WHERE plan_id = ? OR (date >= ? AND date <= ?)",
            (plan_id, plan_start, plan_end),
        )

        inserted = 0
        for w in workouts:
            w["plan_id"] = plan_id
            cols = list(w.keys())
            vals = [w[c] for c in cols]
            placeholders = ",".join(["?"] * len(cols))
            conn.execute(
                f"INSERT INTO planned_workouts ({','.join(cols)}) VALUES ({placeholders})",
                vals,
            )
            inserted += 1

        conn.commit()

    print(f"\nDone! Imported {inserted} workouts into plan '{plan_name}' (id={plan_id})")
    print(f"  跑步训练: {sum(1 for w in workouts if w['sport'] == 'running')} 堂")
    print(f"  力量训练: {sum(1 for w in workouts if w['sport'] == 'training')} 堂")
    print(f"  休息日:   {sum(1 for w in workouts if w['sport'] == 'rest')} 天")
    print(f"  日期范围: {plan_start} → {plan_end}")


if __name__ == "__main__":
    main()
