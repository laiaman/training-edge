#!/usr/bin/env python3
"""Audit canonical plan, SQLite projection, activity links, and month totals."""

from __future__ import annotations

import argparse
import calendar
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import database, plan_store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default=date.today().strftime("%Y-%m"))
    args = parser.parse_args()
    try:
        month_start = date.fromisoformat(f"{args.month}-01")
    except ValueError as exc:
        raise SystemExit("--month 必须为 YYYY-MM") from exc
    month_end = month_start.replace(day=calendar.monthrange(month_start.year, month_start.month)[1])

    document = plan_store.load_plan()
    canonical = {workout["uid"]: workout for workout in document.get("workouts", [])}
    errors: list[str] = []
    running = [workout for workout in canonical.values() if workout.get("sport") == "running"]
    missing_hr = [workout["uid"] for workout in running if not workout.get("target_hr_text")]
    missing_rpe = [workout["uid"] for workout in running if workout.get("target_rpe_min") is None or workout.get("target_rpe_max") is None]
    missing_pace = [workout["uid"] for workout in running if not workout.get("target_pace_text")]
    if missing_hr:
        errors.append(f"{len(missing_hr)}堂跑步课缺少心率处方")
    if missing_rpe:
        errors.append(f"{len(missing_rpe)}堂跑步课缺少RPE范围")
    if missing_pace:
        errors.append(f"{len(missing_pace)}堂跑步课缺少配速处方")
    if len(document.get("pace_zones", {})) != 8:
        errors.append(f"配速区间应为8类，当前={len(document.get('pace_zones', {}))}")
    alternate_scenarios = document.get("alternate_scenarios", {})
    scenario_b = []
    if not isinstance(alternate_scenarios, dict):
        errors.append("alternate_scenarios 必须是对象")
    else:
        scenario_b = list(alternate_scenarios)
        for name, scenario in alternate_scenarios.items():
            if not isinstance(scenario, dict):
                errors.append(f"alternate_scenarios.{name} 必须是对象")
            elif "workouts" in scenario and not isinstance(scenario["workouts"], list):
                errors.append(f"alternate_scenarios.{name}.workouts 必须是数组")

    with database.get_db() as conn:
        projected = [dict(row) for row in conn.execute(
            "SELECT * FROM planned_workouts WHERE source_uid IS NOT NULL"
        )]
        projected_by_uid = {row["source_uid"]: row for row in projected}
        if set(projected_by_uid) != set(canonical):
            errors.append(
                f"主数据投影 UID 不一致：YAML={len(canonical)} DB={len(projected_by_uid)}"
            )

        for uid, workout in canonical.items():
            row = projected_by_uid.get(uid)
            if not row:
                continue
            expected_distance = float(workout.get("target_distance_km") or 0)
            actual_distance = float(row.get("target_distance_km") or 0)
            if abs(expected_distance - actual_distance) > 0.001:
                errors.append(f"{uid} 计划距离不一致：{expected_distance} != {actual_distance}")
            if bool(workout.get("is_key_workout")) != bool(row.get("is_key_workout")):
                errors.append(f"{uid} 关键课标记不一致")
            if workout.get("workout_type") != row.get("workout_type"):
                errors.append(f"{uid} 训练类型不一致")
            for field in (
                "target_pace_text", "target_hr_text", "target_hr_min", "target_hr_max",
                "target_rpe_min", "target_rpe_max", "target_duration_source",
                "target_tss_source", "coach_note", "safety_cutoff", "source_reference",
            ):
                if workout.get(field) != row.get(field):
                    errors.append(f"{uid} {field}投影不一致")
            db_steps = json.loads(row.get("workout_steps_json") or "[]")
            if (workout.get("steps") or []) != db_steps:
                errors.append(f"{uid} 分段处方投影不一致")

        invalid_links = conn.execute(
            """SELECT count(*) FROM planned_workout_activity_links l
               JOIN planned_workouts pw ON pw.id=l.planned_workout_id
               LEFT JOIN activities a ON a.id=l.activity_id
               WHERE a.id IS NULL OR a.date != pw.date OR
                 (pw.sport='running' AND a.sport NOT IN ('running','trail_running','treadmill_running')) OR
                 (pw.sport='training' AND a.sport NOT IN ('training','strength_training','cardio_training'))"""
        ).fetchone()[0]
        if invalid_links:
            errors.append(f"存在 {invalid_links} 条跨日、跨类型或孤立活动关联")

        aggregate_rows = conn.execute(
            """SELECT pw.id,pw.source_uid,pw.sport,pw.target_distance_km,
                      pw.target_duration_min,pw.compliance_status,
                      pw.actual_activity_count,pw.actual_distance_km,pw.actual_duration_min,
                      count(a.id) linked_count,
                      coalesce(sum(a.distance_m),0)/1000.0 linked_km,
                      coalesce(sum(a.total_timer_s),0)/60.0 linked_min
               FROM planned_workouts pw
               LEFT JOIN planned_workout_activity_links l ON l.planned_workout_id=pw.id
               LEFT JOIN activities a ON a.id=l.activity_id
               WHERE pw.source_uid IS NOT NULL AND pw.sport NOT IN ('rest','stretch')
               GROUP BY pw.id"""
        ).fetchall()
        for row in aggregate_rows:
            if int(row["actual_activity_count"] or 0) != int(row["linked_count"] or 0):
                errors.append(f"{row['source_uid']} 活动数量缓存与关联不一致")
            if abs(float(row["actual_distance_km"] or 0) - float(row["linked_km"] or 0)) > 0.01:
                errors.append(f"{row['source_uid']} 实际距离缓存与关联不一致")
            if abs(float(row["actual_duration_min"] or 0) - float(row["linked_min"] or 0)) > 0.11:
                errors.append(f"{row['source_uid']} 实际时长缓存与关联不一致")
            if row["compliance_status"] == "completed" and not row["linked_count"]:
                errors.append(f"{row['source_uid']} 无活动证据却标记完成")
            if row["compliance_status"] == "completed":
                if row["sport"] == "running" and (row["target_distance_km"] or 0) > 0:
                    ratio = float(row["linked_km"] or 0) / float(row["target_distance_km"])
                elif (row["target_duration_min"] or 0) > 0:
                    ratio = float(row["linked_min"] or 0) / float(row["target_duration_min"])
                else:
                    ratio = 1.0
                if ratio < 0.8:
                    errors.append(f"{row['source_uid']} 完成比例 {ratio:.1%} 却标记完成")

        date_from, date_to = month_start.isoformat(), month_end.isoformat()
        db_month = dict(conn.execute(
            """SELECT
                 coalesce(sum(CASE WHEN sport='running' THEN target_distance_km ELSE 0 END),0) planned_km,
                 sum(CASE WHEN sport='running' AND is_key_workout=1 THEN 1 ELSE 0 END) key_total,
                 sum(CASE WHEN sport='running' AND is_key_workout=1 AND compliance_status='completed' THEN 1 ELSE 0 END) key_completed,
                 sum(CASE WHEN sport='training' THEN 1 ELSE 0 END) strength_total
               FROM planned_workouts WHERE date BETWEEN ? AND ?""",
            (date_from, date_to),
        ).fetchone())
        actual_km = conn.execute(
            """SELECT coalesce(sum(distance_m),0)/1000.0 FROM activities
               WHERE date BETWEEN ? AND ? AND sport IN ('running','trail_running','treadmill_running')""",
            (date_from, date_to),
        ).fetchone()[0]

    canonical_month_km = sum(
        float(workout.get("target_distance_km") or 0)
        for workout in canonical.values()
        if workout.get("sport") == "running" and month_start.isoformat() <= workout["date"] <= month_end.isoformat()
    )
    if abs(float(db_month["planned_km"] or 0) - canonical_month_km) > 0.01:
        errors.append(
            f"{args.month} 计划跑量不一致：YAML={canonical_month_km:.1f} DB={db_month['planned_km']:.1f}"
        )

    print({
        "month": args.month,
        "planned_km": round(float(db_month["planned_km"] or 0), 1),
        "actual_km": round(float(actual_km or 0), 1),
        "key_completed": int(db_month["key_completed"] or 0),
        "key_total": int(db_month["key_total"] or 0),
        "strength_total": int(db_month["strength_total"] or 0),
        "running_prescriptions": len(running),
        "structured_steps": sum(bool(workout.get("steps")) for workout in running),
        "pace_zones": len(document.get("pace_zones", {})),
        "scenario_b": len(scenario_b),
        "errors": errors,
    })
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
