from __future__ import annotations

import unittest

from scripts.export_structured_plan import build_document


class SourcePrescriptionCompletenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = build_document()
        cls.running = [
            workout for workout in cls.document["workouts"]
            if workout.get("sport") == "running"
        ]

    def test_primary_running_plan_is_lossless(self) -> None:
        self.assertEqual(len(self.running), 96)
        self.assertTrue(all(workout.get("target_pace_text") for workout in self.running))
        self.assertTrue(all(workout.get("target_hr_text") for workout in self.running))
        self.assertTrue(all(workout.get("target_rpe_min") is not None for workout in self.running))
        self.assertTrue(all(workout.get("target_rpe_max") is not None for workout in self.running))
        self.assertEqual(sum(bool(workout.get("steps")) for workout in self.running), 31)

    def test_plan_level_rules_and_inactive_branch_are_complete(self) -> None:
        self.assertEqual(len(self.document["pace_zones"]), 8)
        self.assertEqual(len(self.document["decision_gate"]["criteria"]), 5)
        self.assertEqual(len(self.document["adaptation_rules"]["heat"]), 5)
        self.assertEqual(len(self.document["adaptation_rules"]["hydration_fueling"]), 5)
        self.assertEqual(len(self.document["support_training"]["achilles_maintenance"]), 3)
        self.assertEqual(len(self.document["preparation_templates"]), 3)
        branch = self.document["alternate_scenarios"]["no_go"]
        self.assertEqual(branch["activation"], "pending_confirmation")
        self.assertEqual(len(branch["workouts"]), 32)

    def test_weekly_totals_are_derived_from_workouts(self) -> None:
        totals = {}
        for workout in self.running:
            totals.setdefault(workout["week_label"], 0.0)
            totals[workout["week_label"]] += float(workout["target_distance_km"])
        self.assertEqual(totals["W2"], 49.0)
        self.assertEqual(totals["W6"], 58.0)
        self.assertEqual(totals["W11"], 74.0)
        self.assertEqual(totals["W14"], 80.0)


if __name__ == "__main__":
    unittest.main()
