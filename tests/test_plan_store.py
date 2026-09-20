from __future__ import annotations

import copy
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml

from engine import database, plan_store


def minimal_document() -> dict:
    document = {
        "schema_version": 1,
        "revision": 1,
        "plan": {"id": "test", "end_date": "2026-10-18"},
        "goal": {
            "primary_race": "测试马拉松",
            "primary_race_date": "2026-10-18",
            "a_target_time": "02:55:00",
            "marathon_pace": "04:08",
        },
        "pace_zones": {},
        "workouts": [
            {
                "uid": "w1", "date": "2026-07-20", "sport": "running",
                "title": "轻松跑", "target_distance_km": 10,
            },
            {
                "uid": "w2", "date": "2026-07-21", "sport": "training",
                "title": "核心力量", "target_duration_min": 30,
            },
        ],
        "metadata": {"checksum": ""},
    }
    document["metadata"]["checksum"] = plan_store._payload_checksum(document)
    return document


class PlanStoreWorstCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.plan_path = root / "plan.yaml"
        self.db_path = root / "test.sqlite"
        self.goal_mirror = root / "goal.md"
        self.plan_mirror = root / "plan.md"
        os.environ["TRAININGEDGE_GOAL_MIRROR_PATH"] = str(self.goal_mirror)
        os.environ["TRAININGEDGE_PLAN_MIRROR_PATH"] = str(self.plan_mirror)
        self.document = minimal_document()
        self.plan_path.write_text(yaml.safe_dump(self.document, allow_unicode=True, sort_keys=False), encoding="utf-8")
        database.init_db(self.db_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_stale_revision_pauses_sync(self) -> None:
        with database.get_db(self.db_path) as conn:
            plan_store.sync_plan_to_db(conn, self.document)
            with self.assertRaises(plan_store.PlanConflictError):
                plan_store.save_plan(conn, self.document, expected_revision=0, source="test", path=self.plan_path)

    def test_checksum_tampering_is_rejected(self) -> None:
        tampered = copy.deepcopy(self.document)
        tampered["workouts"][0]["title"] = "被直接篡改"
        self.plan_path.write_text(yaml.safe_dump(tampered, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(plan_store.PlanConflictError):
            plan_store.load_plan(self.plan_path)

    def test_completed_workout_cannot_change_or_disappear(self) -> None:
        with database.get_db(self.db_path) as conn:
            plan_store.sync_plan_to_db(conn, self.document)
            conn.execute("UPDATE planned_workouts SET compliance_status='completed' WHERE source_uid='w1'")
            changed = plan_store.apply_workout_changes(self.document, [{"uid": "w1", "patch": {"title": "改写"}}])
            with self.assertRaises(plan_store.CompletedWorkoutLockedError):
                plan_store.save_plan(conn, changed, expected_revision=1, source="test", path=self.plan_path)
            removed = copy.deepcopy(self.document)
            removed["workouts"] = [removed["workouts"][1]]
            with self.assertRaises(plan_store.CompletedWorkoutLockedError):
                plan_store.save_plan(conn, removed, expected_revision=1, source="test", path=self.plan_path)

    def test_completed_workout_allows_only_empty_prescription_backfill(self) -> None:
        with database.get_db(self.db_path) as conn:
            plan_store.sync_plan_to_db(conn, self.document)
            conn.execute("UPDATE planned_workouts SET compliance_status='completed' WHERE source_uid='w1'")
            enriched = copy.deepcopy(self.document)
            enriched["workouts"][0].update({
                "target_pace_text": "5'10\"-5'30\"",
                "target_hr_text": "<140",
                "target_hr_max": 140,
                "target_rpe_min": 3,
                "target_rpe_max": 3,
                "source_reference": "source.md:1",
            })
            saved = plan_store.save_plan(
                conn, enriched, expected_revision=1, source="migration", path=self.plan_path,
                allow_prescription_backfill=True,
            )
            row = conn.execute(
                "SELECT target_pace_text,target_hr_text,target_rpe_min,compliance_status "
                "FROM planned_workouts WHERE source_uid='w1'"
            ).fetchone()
            self.assertEqual(saved["revision"], 2)
            self.assertEqual(row["target_hr_text"], "<140")
            self.assertEqual(row["target_rpe_min"], 3)
            self.assertEqual(row["compliance_status"], "completed")

            overwritten = copy.deepcopy(saved)
            overwritten["workouts"][0]["target_hr_text"] = "<150"
            with self.assertRaises(plan_store.CompletedWorkoutLockedError):
                plan_store.save_plan(
                    conn, overwritten, expected_revision=2, source="migration", path=self.plan_path,
                    allow_prescription_backfill=True,
                )

    def test_projection_is_idempotent_and_removes_only_future_orphans(self) -> None:
        with database.get_db(self.db_path) as conn:
            plan_store.sync_plan_to_db(conn, self.document)
            plan_store.sync_plan_to_db(conn, self.document)
            self.assertEqual(conn.execute("SELECT count(*) FROM planned_workouts").fetchone()[0], 2)
            candidate = copy.deepcopy(self.document)
            candidate["workouts"] = [candidate["workouts"][0]]
            saved = plan_store.save_plan(conn, candidate, expected_revision=1, source="test", path=self.plan_path)
            self.assertEqual(saved["revision"], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM planned_workouts").fetchone()[0], 1)
            self.assertIn("Revision: `2`", self.goal_mirror.read_text(encoding="utf-8"))

    def test_invalid_distance_types_are_clean_errors(self) -> None:
        for value in (-1, "not-a-number", {}):
            invalid = copy.deepcopy(self.document)
            invalid["workouts"][0]["target_distance_km"] = value
            with self.subTest(value=value), self.assertRaises(plan_store.PlanStoreError):
                plan_store._validate_document(invalid)

    def test_invalid_rpe_ranges_are_clean_errors(self) -> None:
        for lower, upper in ((8, 6), (-1, 3), (3, 11), ("bad", 5)):
            invalid = copy.deepcopy(self.document)
            invalid["workouts"][0]["target_rpe_min"] = lower
            invalid["workouts"][0]["target_rpe_max"] = upper
            with self.subTest(lower=lower, upper=upper), self.assertRaises(plan_store.PlanStoreError):
                plan_store._validate_document(invalid)

    def test_partial_structured_changes_are_isolated(self) -> None:
        changed = plan_store.apply_plan_changes(self.document, [
            {"kind": "goal", "patch": {"a_target_time": "03:00:00"}},
            {"kind": "pace_zones", "value": {"easy": {"pace": "05:10-05:40"}}},
        ])
        self.assertEqual(changed["goal"]["a_target_time"], "03:00:00")
        self.assertEqual(changed["pace_zones"]["easy"]["pace"], "05:10-05:40")
        self.assertEqual(changed["workouts"], self.document["workouts"])

    def test_cross_training_rules_replace_whole_object_and_deep_copy(self) -> None:
        self.document["cross_training_rules"] = {"period": "W1-W2", "allowed": ["cycling"]}
        replacement = {"period": "W3-W4", "allowed": ["mobility"]}
        changed = plan_store.apply_plan_changes(self.document, [
            {"kind": "cross_training_rules", "value": replacement},
        ])
        self.assertEqual(changed["cross_training_rules"], replacement)
        self.assertIsNot(changed["cross_training_rules"], replacement)
        self.assertIsNot(changed["cross_training_rules"]["allowed"], replacement["allowed"])
        replacement["allowed"].append("basketball")
        self.assertEqual(changed["cross_training_rules"]["allowed"], ["mobility"])

        with self.assertRaises(plan_store.PlanStoreError):
            plan_store.apply_plan_changes(self.document, [
                {"kind": "cross_training_rules", "value": []},
            ])

    def test_structured_future_reperiodization_is_scoped_and_validated(self) -> None:
        replacement = [{
            "uid": "w3", "date": "2026-07-22", "week_label": "W2",
            "phase": "专项期", "sport": "running", "title": "专项跑",
            "target_distance_km": 12,
        }]
        changed = plan_store.apply_plan_changes(self.document, [
            {"kind": "plan", "patch": {"weeks": 2, "end_date": "2026-07-22"}},
            {"kind": "decision_gate", "value": {"week": "W2"}},
            {"kind": "alternate_scenarios", "value": {"fallback": {"workouts": replacement}}},
            {"kind": "support_training", "value": {"strength_schedule": []}},
            {"kind": "workouts_replace_from_date", "from_date": "2026-07-21", "workouts": replacement},
        ])
        self.assertEqual(changed["plan"]["weeks"], 2)
        self.assertEqual(changed["decision_gate"]["week"], "W2")
        self.assertEqual(changed["alternate_scenarios"]["fallback"]["workouts"][0]["uid"], "w3")
        self.assertEqual(changed["support_training"], {"strength_schedule": []})
        self.assertEqual([workout["uid"] for workout in changed["workouts"]], ["w1", "w3"])

    def test_future_reperiodization_rejects_backdated_or_duplicate_workouts(self) -> None:
        backdated = [{"uid": "w3", "date": "2026-07-20", "sport": "running", "title": "越界"}]
        with self.assertRaises(plan_store.PlanStoreError):
            plan_store.apply_plan_changes(self.document, [{
                "kind": "workouts_replace_from_date", "from_date": "2026-07-21", "workouts": backdated,
            }])
        duplicate = [{"uid": "w1", "date": "2026-07-22", "sport": "running", "title": "重复"}]
        with self.assertRaises(plan_store.PlanStoreError):
            plan_store.apply_plan_changes(self.document, [{
                "kind": "workouts_replace_from_date", "from_date": "2026-07-21", "workouts": duplicate,
            }])

    def test_future_workouts_replace_preserves_history_and_deep_copies(self) -> None:
        replacement = [{
            "uid": "w3", "date": "2026-07-21", "sport": "running", "title": "新专项跑",
            "target_distance_km": 12,
        }]
        changed = plan_store.apply_plan_changes(self.document, [{
            "kind": "future_workouts_replace", "effective_date": "2026-07-21", "workouts": replacement,
        }])
        self.assertEqual([workout["uid"] for workout in changed["workouts"]], ["w1", "w3"])
        self.assertEqual(changed["workouts"][0]["title"], "轻松跑")
        self.assertIsNot(changed["workouts"][0], self.document["workouts"][0])
        self.assertIsNot(changed["workouts"][1], replacement[0])
        replacement[0]["title"] = "外部修改"
        self.assertEqual(changed["workouts"][1]["title"], "新专项跑")

    def test_future_workouts_replace_rejects_invalid_boundary_or_backdated_workout(self) -> None:
        replacement = [{"uid": "w3", "date": "2026-07-21", "sport": "running", "title": "新课"}]
        for effective_date in ("2026-02-30", "not-a-date"):
            with self.subTest(effective_date=effective_date), self.assertRaises(plan_store.PlanStoreError):
                plan_store.apply_plan_changes(self.document, [{
                    "kind": "future_workouts_replace", "effective_date": effective_date,
                    "workouts": replacement,
                }])
        backdated = [{"uid": "w3", "date": "2026-07-20", "sport": "running", "title": "越界"}]
        with self.assertRaises(plan_store.PlanStoreError):
            plan_store.apply_plan_changes(self.document, [{
                "kind": "future_workouts_replace", "effective_date": "2026-07-21", "workouts": backdated,
            }])

    def test_future_workouts_replace_keeps_completed_history_lock_in_save_plan(self) -> None:
        with database.get_db(self.db_path) as conn:
            plan_store.sync_plan_to_db(conn, self.document)
            conn.execute("UPDATE planned_workouts SET compliance_status='completed' WHERE source_uid='w2'")
            changed = plan_store.apply_plan_changes(self.document, [{
                "kind": "future_workouts_replace", "effective_date": "2026-07-21",
                "workouts": [{
                    "uid": "w3", "date": "2026-07-22", "sport": "running", "title": "新专项跑",
                }],
            }])
            with self.assertRaises(plan_store.CompletedWorkoutLockedError):
                plan_store.save_plan(conn, changed, expected_revision=1, source="test", path=self.plan_path)

    def test_workout_patch_can_reassign_week_and_phase(self) -> None:
        changed = plan_store.apply_workout_changes(self.document, [{
            "uid": "w2", "patch": {"week_label": "W2", "phase": "专项期"},
        }])
        self.assertEqual(changed["workouts"][1]["week_label"], "W2")
        self.assertEqual(changed["workouts"][1]["phase"], "专项期")

    def test_old_database_receives_additive_columns(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy.sqlite"
        connection = sqlite3.connect(legacy_path)
        connection.execute("CREATE TABLE planned_workouts (id INTEGER PRIMARY KEY, date TEXT, sport TEXT)")
        connection.commit()
        connection.close()
        database.init_db(legacy_path)
        with sqlite3.connect(legacy_path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(planned_workouts)")}
        self.assertTrue({
            "source_uid", "plan_revision", "target_distance_km", "phase",
            "target_pace_text", "target_hr_text", "target_rpe_min", "target_rpe_max",
            "workout_steps_json", "source_reference",
        }.issubset(columns))

    def test_completed_legacy_row_receives_missing_canonical_metadata(self) -> None:
        document = copy.deepcopy(self.document)
        document["workouts"][0]["is_key_workout"] = True
        document["workouts"][0]["workout_type"] = "long_run"
        with database.get_db(self.db_path) as conn:
            plan_store.sync_plan_to_db(conn, document)
            conn.execute(
                """UPDATE planned_workouts SET compliance_status='completed',
                   target_distance_km=NULL, is_key_workout=0,
                   workout_type=NULL, source_uid=NULL WHERE title='轻松跑'"""
            )
            plan_store.sync_plan_to_db(conn, document)
            row = conn.execute(
                """SELECT target_distance_km,is_key_workout,workout_type,source_uid,title
                   FROM planned_workouts WHERE title='轻松跑'"""
            ).fetchone()
        self.assertEqual(row["target_distance_km"], 10)
        self.assertEqual(row["is_key_workout"], 1)
        self.assertEqual(row["workout_type"], "long_run")
        self.assertEqual(row["source_uid"], "w1")
        self.assertEqual(row["title"], "轻松跑")


if __name__ == "__main__":
    unittest.main()
