from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from engine import database
from engine.readiness import compute_weekly_deviation


class ComplianceEvidenceTests(unittest.TestCase):
    def test_cross_day_activity_never_completes_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "compliance.sqlite"
            database.init_db(db_path)
            monday = date.today() - timedelta(days=date.today().weekday())
            workout_day = monday + timedelta(days=2)
            empty_day = monday + timedelta(days=4)
            with database.get_db(db_path) as conn:
                conn.executemany(
                    """INSERT INTO activities
                       (id,date,sport,name,distance_m,total_timer_s,tss,start_time)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    [
                        (101, workout_day.isoformat(), "running", "短跑记录", 5000, 1680, None, f"{workout_day} 06:00:00"),
                        (102, workout_day.isoformat(), "running", "完整训练", 14800, 5580, None, f"{workout_day} 18:30:00"),
                    ],
                )
                conn.execute(
                    """INSERT INTO planned_workouts
                       (date,sport,title,target_distance_km,target_duration_min,compliance_status)
                       VALUES (?,?,?,?,?,'pending')""",
                    (workout_day.isoformat(), "running", "节奏跑", 12, 50),
                )
                conn.execute(
                    """INSERT INTO planned_workouts
                       (date,sport,title,target_distance_km,target_duration_min,compliance_status,actual_activity_id)
                       VALUES (?,?,?,?,?,'completed',?)""",
                    (empty_day.isoformat(), "running", "轻松跑", 10, 53, "102"),
                )

                matched = database.match_compliance(conn, workout_day.isoformat())
                rows = [dict(row) for row in conn.execute(
                    """SELECT date,compliance_status,actual_activity_id,
                              actual_activity_count,actual_distance_km
                       FROM planned_workouts ORDER BY date"""
                )]
                deviation = compute_weekly_deviation(conn, workout_day.isoformat())

            self.assertEqual(matched, 1)
            self.assertEqual(rows[0]["compliance_status"], "completed")
            self.assertEqual(str(rows[0]["actual_activity_id"]), "101")
            self.assertEqual(rows[0]["actual_activity_count"], 2)
            self.assertAlmostEqual(rows[0]["actual_distance_km"], 19.8, places=1)
            self.assertEqual(rows[1]["compliance_status"], "pending")
            self.assertIsNone(rows[1]["actual_activity_id"])
            self.assertEqual(deviation.actual_count, 1)
            self.assertEqual(deviation.primary_actual, 1)
            self.assertAlmostEqual(deviation.actual_tss, 0)
            self.assertFalse(deviation.tss_available)


if __name__ == "__main__":
    unittest.main()
