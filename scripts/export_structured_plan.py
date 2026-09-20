#!/usr/bin/env python3
"""Export the approved 22-week marathon baseline to canonical YAML."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
sys.path.insert(0, str(ROOT))

from scripts.import_marathon_plan import _build_plan, build_alternate_scenario


def _workout_type(title: str, intensity: str, sport: str) -> str:
    if sport == "training":
        return "strength"
    if sport in {"rest", "stretch"}:
        return "rest"
    text = f"{title} {intensity}".lower()
    if "马拉松" in text or "race" in text:
        return "race"
    if "cv" in text or "间歇" in text or "threshold" in text:
        return "interval"
    if "mantz" in text or "lsd" in text or "长距离" in text or "长跑" in text:
        return "long_run"
    if "恢复" in text:
        return "recovery"
    if "m-pace" in text or "节奏" in text or "tempo" in text:
        return "tempo"
    return "easy"


def _is_key(workout_type: str, title: str) -> bool:
    return workout_type in {"interval", "long_run", "tempo", "race"} or "评估" in title


def _uid(workout: dict, position: int) -> str:
    week = workout.get("week_label", "W0").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", workout["sport"].lower()).strip("-")
    return f"{week}-{workout['date']}-{slug}-{position:02d}"


def build_document() -> dict:
    source = _build_plan()
    workouts = []
    for position, item in enumerate(source, 1):
        workout_type = _workout_type(item["title"], item.get("target_intensity", ""), item["sport"])
        workout = {
            "uid": _uid(item, position),
            "date": item["date"],
            "week_label": item.get("week_label"),
            "phase": item.get("phase"),
            "sport": item["sport"],
            "workout_type": workout_type,
            "is_key_workout": _is_key(workout_type, item["title"]),
            "title": item["title"],
            "description": item.get("description"),
            "target_distance_km": item.get("target_distance_km"),
            "target_duration_min": item.get("target_duration_min"),
            "target_tss": item.get("target_tss"),
            "target_intensity": item.get("target_intensity"),
            "target_pace_text": item.get("target_pace_text"),
            "target_hr_text": item.get("target_hr_text"),
            "target_hr_min": item.get("target_hr_min"),
            "target_hr_max": item.get("target_hr_max"),
            "target_rpe": item.get("target_rpe"),
            "target_rpe_min": item.get("target_rpe_min"),
            "target_rpe_max": item.get("target_rpe_max"),
            "target_duration_source": item.get("target_duration_source"),
            "target_tss_source": item.get("target_tss_source"),
            "steps": item.get("steps"),
            "coach_note": item.get("coach_note"),
            "safety_cutoff": item.get("safety_cutoff"),
            "source_reference": item.get("source_reference"),
        }
        workouts.append({key: value for key, value in workout.items() if value is not None})

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    document = {
        "schema_version": 1,
        "revision": 1,
        "plan": {
            "id": "marathon-2026-beijing-nanchang",
            "version": "3.0-migrated-baseline",
            "name": "2026北京/南昌双赛马拉松备战",
            "strategy": "road_marathon",
            "philosophy": ["tinman_cv", "canova", "mantz_fatigue_m_pace"],
            "start_date": "2026-06-08",
            "end_date": "2026-11-08",
            "weeks": 22,
            "decision_gate_week": 15,
        },
        "goal": {
            "primary_race": "2026北京马拉松",
            "primary_race_date": "2026-10-18",
            "secondary_race": "2026南昌马拉松",
            "secondary_race_date": "2026-11-08",
            "a_target_time": "02:55:00",
            "b_target_time": "02:58:00",
            "marathon_pace": "04:08",
            "priority": "A",
            "race_strategy": "dual_peak",
        },
        "athlete_constraints": {
            "preferred_training_days": ["周二", "周三", "周五", "周六", "周日"],
            "quality_day": "周三",
            "long_run_day": "周日",
            "strength_role": "independent_support",
            "injury_watch": ["腰骶部", "跟腱", "比目鱼肌"],
        },
        "pace_zones": {
            "recovery": {"pace": "05:30-06:00", "hr": "<120", "rpe": "1-2"},
            "easy": {"pace": "05:00-05:40", "hr": "120-140", "rpe": "3-4"},
            "aerobic_tempo": {"pace": "04:35-04:50", "hr": "135-145", "rpe": "5"},
            "steady": {"pace": "04:20-04:35", "hr": "145-155", "rpe": "6"},
            "marathon": {"pace": "04:08-04:13", "hr": "148-162", "rpe": "7-8"},
            "cv": {"pace": "03:45-03:50", "hr": "160-170", "rpe": "7-9"},
            "interval": {"pace": "03:35-03:45", "hr": "165-175", "rpe": "8-9"},
            "strides": {"pace": "03:20-03:40", "hr": "170+", "rpe": "9-10"},
        },
        "decision_gate": {
            "week": "W15",
            "evaluation_window": "2026-09-14/2026-09-20",
            "criteria": [
                {"dimension": "CV间歇", "go": "3'45\"/km稳定完成全组", "no_go": "滑落>3'50\"或无法完成"},
                {"dimension": "M-Pace节奏跑", "go": "W13 12km连续完成且HR<160", "no_go": "无法维持4'10\"或HR飘升>165"},
                {"dimension": "伤病", "go": "无新伤且旧伤无复发", "no_go": "新伤或跟腱/腰骶加重"},
                {"dimension": "主观状态", "go": "精神好且训练意愿强", "no_go": "持续倦怠或睡眠差"},
                {"dimension": "Garmin", "go": "Body Battery正常且HRV稳定", "no_go": "Body Battery持续偏低或HRV下降"},
            ],
            "recovery_hr_rule": "W15周三节奏跑后次日晨起HR高于个人均值>5bpm，叠加任一黄色指标即倾向No-Go",
        },
        "adaptation_rules": {
            "heat": [
                {"condition": "体感<20°C且湿度<65%", "pace_adjustment_sec_per_km": "0", "action": "正常执行"},
                {"condition": "体感20-25°C", "pace_adjustment_sec_per_km": "0-3", "action": "正常执行"},
                {"condition": "体感25-28°C或湿度75-85%", "pace_adjustment_sec_per_km": "5-8", "action": "间歇降一档，维持训练量与RPE"},
                {"condition": "体感28-32°C或湿度>85%", "pace_adjustment_sec_per_km": "8-12", "action": "长距离缩短20%，禁止I-Pace"},
                {"condition": "气温>32°C或体感>35°C", "pace_adjustment_sec_per_km": None, "action": "取消户外强度课，仅恢复跑或室内"},
            ],
            "hydration_fueling": [
                "体感<20°C：每8-10km补水150ml",
                "体感20-28°C：每5-7km补水150-200ml，每10km补电解质",
                "体感≥28°C：每3-4km补水200ml，每6km补电解质",
                "间歇课每3-4组补水100-150ml，高温天每2组",
                "长距离60min后每30-45min补充30-60g碳水",
            ],
        },
        "support_training": {
            "strength_schedule": [
                {"weeks": "W1-W5", "frequency": "每周1次周四", "duration_min": 45, "intensity": "中等"},
                {"weeks": "W6-W11", "frequency": "每周1次周四", "duration_min": 40, "intensity": "60-65% 1RM，不力竭"},
                {"weeks": "W12-W16", "frequency": "每周1次", "duration_min": 35, "intensity": "减量保爆发"},
                {"weeks": "W17-W19", "frequency": "停止负重下肢", "duration_min": None, "intensity": "仅轻核心和拉伸"},
                {"weeks": "W20-W22", "frequency": "完全停止", "duration_min": None, "intensity": "泡沫轴和拉伸"},
            ],
            "scheduling_constraints": ["严禁周二练腿", "严禁周六练腿", "首选周四，次选周一"],
            "achilles_maintenance": [
                {"exercise": "站姿自重提踵", "prescription": "2×15", "note": "离心3秒慢放"},
                {"exercise": "坐姿自重提踵", "prescription": "2×15", "note": "缓慢控制"},
                {"exercise": "跟腱弹震热身", "prescription": "2×20", "note": "小跳激活跟腱弹性"},
            ],
        },
        "context_sources": [
            "vault/profile/profile.md",
            "vault/profile/injuries.md",
            "vault/goals/current_goal.md",
            "vault/plans/2026_Marathon_Plan.md",
        ],
        "preparation_templates": [
            {"day": "周三", "title": "法特莱克/间歇", "distance": "8-10 km", "pace": "快段3'40\"-3'50\"", "hr": "155-165", "rpe": "7-8"},
            {"day": "周末", "title": "轻松有氧", "distance": "10-15 km", "pace": "5'00\"-5'30\"", "hr": "120-135", "rpe": "3-4"},
            {"day": "其他", "title": "恢复跑/休息", "distance": "6-8 km", "pace": "5'15\"-5'40\"", "hr": "<130", "rpe": "3"},
        ],
        "cross_training_rules": {
            "basketball": [
                {"period": "前置储备期", "rule": "暂停"},
                {"period": "W1-W5", "rule": "腰骶未完全康复期间禁止对抗性运动"},
                {"period": "W6-W11", "rule": "每周不超过1次、30-40min轻度，避开周三和周日"},
                {"period": "W12-W16", "rule": "暂停"},
                {"period": "W17-W19", "rule": "禁止"},
                {"period": "W20-W22", "rule": "禁止"},
            ]
        },
        "workouts": workouts,
        "alternate_scenarios": {
            "no_go": {
                "trigger": "W15评估 No-Go 或北京赛前退赛",
                "target_race": "2026南昌马拉松",
                "source_details": "vault/plans/2026_Marathon_Plan.md#场景b",
                "activation": "pending_confirmation",
                "workouts": build_alternate_scenario(),
            }
        },
        "metadata": {
            "created_at": now,
            "updated_at": now,
            "updated_by": "baseline_migration",
            "source_markdown": "vault/plans/2026_Marathon_Plan.md",
            "checksum": "",
        },
    }
    payload = json.loads(json.dumps(document, ensure_ascii=False))
    payload["metadata"].pop("checksum", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    document["metadata"]["checksum"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return document


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=WORKSPACE / "vault" / "plans" / "training_plan.yaml")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.output.exists() and not args.force:
        raise SystemExit(f"Refusing to overwrite existing plan: {args.output}")
    document = build_document()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    print(f"wrote {args.output} ({len(document['workouts'])} workouts, revision {document['revision']})")


if __name__ == "__main__":
    main()
