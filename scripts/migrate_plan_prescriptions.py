#!/usr/bin/env python3
"""Restore source prescriptions without mutating completed execution facts."""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import database, plan_store
from scripts.export_structured_plan import build_document


CORE_FIELDS = ("date", "sport", "title", "description", "target_distance_km")


def build_candidate(current: dict) -> dict:
    candidate = build_document()
    current_by_uid = {workout["uid"]: workout for workout in current.get("workouts", [])}
    candidate_by_uid = {workout["uid"]: workout for workout in candidate.get("workouts", [])}
    if set(current_by_uid) != set(candidate_by_uid):
        raise SystemExit(
            f"迁移已停止：活动课表UID变化，当前={len(current_by_uid)} 新={len(candidate_by_uid)}"
        )
    for uid, before in current_by_uid.items():
        after = candidate_by_uid[uid]
        changed = [field for field in CORE_FIELDS if before.get(field) != after.get(field)]
        if changed:
            raise SystemExit(f"迁移已停止：{uid} 核心处方发生变化: {', '.join(changed)}")
    candidate["metadata"] = copy.deepcopy(current.get("metadata", {}))
    candidate["metadata"]["prescription_source"] = "vault/plans/2026_Marathon_Plan.md"
    candidate["metadata"]["prescription_migration"] = "lossless_v1"
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    database.init_db()
    current = plan_store.load_plan()
    candidate = build_candidate(current)
    running = [workout for workout in candidate["workouts"] if workout.get("sport") == "running"]
    summary = {
        "base_revision": current["revision"],
        "workouts": len(candidate["workouts"]),
        "running": len(running),
        "hr_complete": sum(bool(workout.get("target_hr_text")) for workout in running),
        "rpe_complete": sum(workout.get("target_rpe_min") is not None for workout in running),
        "structured_steps": sum(bool(workout.get("steps")) for workout in running),
        "pace_zones": len(candidate.get("pace_zones", {})),
        "scenario_b": len(candidate["alternate_scenarios"]["no_go"]["workouts"]),
    }
    if args.dry_run:
        print({**summary, "dry_run": True})
        return

    with database.get_db() as conn:
        saved = plan_store.save_plan(
            conn,
            candidate,
            expected_revision=current["revision"],
            source="source_prescription_restoration",
            allow_prescription_backfill=True,
        )
        conn.commit()
    print({**summary, "revision": saved["revision"], "dry_run": False})


if __name__ == "__main__":
    main()
