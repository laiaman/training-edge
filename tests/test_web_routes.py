from __future__ import annotations

import os
import json
import socket
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import uvicorn


class RunningWebRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        source_plan = Path(__file__).resolve().parents[2] / "vault" / "plans" / "training_plan.yaml"
        cls.plan_path = root / "training_plan.yaml"
        shutil.copy2(source_plan, cls.plan_path)
        os.environ["TRAININGEDGE_PLAN_PATH"] = str(cls.plan_path)
        os.environ["TRAININGEDGE_GOAL_MIRROR_PATH"] = str(root / "goal.md")
        os.environ["TRAININGEDGE_PLAN_MIRROR_PATH"] = str(root / "plan.md")
        os.environ["TRAININGEDGE_DB_PATH"] = str(root / "web.sqlite")
        os.environ["TRAININGEDGE_PASSWORD"] = ""
        from api.app import app
        cls.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.socket.bind(("127.0.0.1", 0))
        cls.port = cls.socket.getsockname()[1]
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
        cls.server_thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.socket]}, daemon=True)
        cls.server_thread.start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.02)
        if not cls.server.started:
            raise RuntimeError("测试服务器未能启动")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.server_thread.join(timeout=5)
        cls.temporary.cleanup()

    def request(self, path: str, payload: dict | None = None) -> tuple[int, str, dict | None]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data else "GET",
        )
        try:
            with urlopen(request, timeout=5) as response:
                text = response.read().decode("utf-8")
                return response.status, text, json.loads(text) if response.headers.get_content_type() == "application/json" else None
        except HTTPError as error:
            text = error.read().decode("utf-8")
            return error.code, text, json.loads(text) if text else None

    def test_month_week_goal_and_activity_templates_render(self) -> None:
        for url, marker in (
            ("/plan", "月"),
            ("/plan?view=week&week=2026-07-13", "周三"),
            ("/plan/legacy?week=2026-07-13", "周三"),
            ("/goal", "生成影响预览"),
            ("/", "最近活动"),
        ):
            with self.subTest(url=url):
                status, text, _ = self.request(url)
                self.assertEqual(status, 200, text[:500])
                self.assertIn(marker, text)

    def test_invalid_calendar_parameters_fail_cleanly(self) -> None:
        self.assertEqual(self.request("/plan?view=year")[0], 400)
        self.assertEqual(self.request("/plan?view=month&month=2026-99")[0], 400)

    def test_unplanned_activity_is_compact_count_on_rest_day_calendar(self) -> None:
        from engine import database

        activity_id = 990002
        with database.get_db() as conn:
            conn.execute(
                """INSERT INTO activities
                   (id,date,start_time,sport,name,distance_m,total_timer_s,avg_hr,max_hr,avg_cadence)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (activity_id, "2026-08-25", "2026-08-25 18:48:00", "running",
                 "rest-day-activity-test", 10125, 3594, 129, 145, 176),
            )

        try:
            status, html, _ = self.request("/plan?view=month&month=2026-08")
            self.assertEqual(status, 200, html[:500])
            self.assertIn("1 条活动", html)
            self.assertIn("openDayActivities(this)", html)
            self.assertIn('class="activity-count-badge"', html)
            self.assertIn("activities.length === 1", html)
            self.assertIn("window.location.href = `/activity/${encodeURIComponent(activities[0].id)}`", html)
            self.assertNotIn('class="actual-activity-card"', html)
        finally:
            with database.get_db() as conn:
                conn.execute("DELETE FROM activities WHERE id=?", (activity_id,))

    def test_race_info_uses_canonical_goal_over_legacy_settings(self) -> None:
        from engine import database

        canonical_goal = self.request("/api/plan-document")[2]["plan"]["goal"]
        with database.get_db() as conn:
            database.set_setting(conn, "race_name", "旧设置赛事")
            database.set_setting(conn, "race_date", "2099-01-01")
            database.set_setting(conn, "race_target", "9:59")

        status, text, payload = self.request("/api/race-info")
        self.assertEqual(status, 200, text)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["name"], canonical_goal["primary_race"])
        self.assertEqual(payload["date"], canonical_goal["primary_race_date"])
        self.assertEqual(payload["target"], f"{int(canonical_goal['a_target_time'][:2])}:{canonical_goal['a_target_time'][3:5]}")

    def test_navigation_order_is_validated_and_applied(self) -> None:
        status, settings_html, _ = self.request("/settings")
        self.assertEqual(status, 200)
        self.assertIn("顶部导航 Tab 排序", settings_html)
        self.assertEqual(self.request("/api/settings", {"navigation_order": ["plan"]})[0], 422)

        order = ["plan", "goal", "dashboard", "body", "settings"]
        self.assertEqual(self.request("/api/settings", {"navigation_order": order})[0], 200)
        _, page_html, _ = self.request("/plan")
        positions = [page_html.index(link) for link in (
            '<a href="/plan">训练计划</a>', '<a href="/goal">目标与周期</a>',
            '<a href="/">面板</a>', '<a href="/body-data">身体数据</a>',
            '<a href="/settings">设置</a>',
        )]
        self.assertEqual(positions, sorted(positions))

    def test_goal_page_uses_chinese_pace_zone_descriptions(self) -> None:
        status, html, _ = self.request("/goal")
        self.assertEqual(status, 200)
        for text in ("恢复跑", "轻松有氧", "有氧节奏", "稳态跑", "马拉松配速", "CV／乳酸阈", "短间歇", "冲刺／神经激活"):
            self.assertIn(text, html)

    def test_drawer_does_not_present_source_total_as_step_distance_or_repeat_safety(self) -> None:
        status, html, _ = self.request("/plan?view=month&month=2026-09")
        self.assertEqual(status, 200)
        self.assertNotIn("escapeHtml(step.structure)", html)
        self.assertIn("workout.coach_note !== workout.safety_cutoff", html)
        self.assertIn("安全截断[:：]?", html)

    def test_goal_proposal_applies_only_selected_item_once(self) -> None:
        before = self.request("/api/plan-document")[2]["plan"]
        payload = {
            "expected_revision": before["revision"],
            "goal": {
                "primary_race": "选择性测试赛事", "primary_race_date": "2026-10-25",
                "secondary_race": "备选赛事", "secondary_race_date": "2026-11-08",
                "a_target_time": "02:56:00", "b_target_time": "03:00:00", "priority": "A",
            },
        }
        status, _, proposal = self.request("/api/goal-impact-proposals", payload)
        self.assertEqual(status, 200)
        self.assertEqual(proposal["changes"][0]["after"]["marathon_pace"], "04:10")
        apply_path = f"/api/plan-proposals/{proposal['proposal_id']}/apply"
        status, text, applied = self.request(apply_path, {"selected": [0]})
        self.assertEqual(status, 200, text)
        self.assertEqual(applied["applied"], 1)
        after = self.request("/api/plan-document")[2]["plan"]
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["goal"]["primary_race"], "选择性测试赛事")
        self.assertEqual(after["pace_zones"], before["pace_zones"])
        self.assertEqual(self.request(apply_path, {"selected": [0]})[0], 409)

    def test_goal_preview_does_not_mutate_plan_and_stale_revision_conflicts(self) -> None:
        before = self.request("/api/plan-document")[2]["plan"]
        payload = {
            "expected_revision": before["revision"],
            "goal": {
                "primary_race": "测试赛事", "primary_race_date": "2026-10-25",
                "secondary_race": "备选赛事", "secondary_race_date": "2026-11-08",
                "a_target_time": "02:55:00", "b_target_time": "03:00:00", "priority": "A",
            },
        }
        status, text, result = self.request("/api/goal-impact-proposals", payload)
        self.assertEqual(status, 200, text)
        self.assertEqual(len(result["changes"]), 4)
        self.assertEqual(result["changes"][0]["after"]["marathon_pace"], "04:08")
        after = self.request("/api/plan-document")[2]["plan"]
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["goal"], before["goal"])
        payload["expected_revision"] = 0
        self.assertEqual(self.request("/api/goal-impact-proposals", payload)[0], 409)

    def test_bad_goal_inputs_are_rejected(self) -> None:
        revision = self.request("/api/plan-document")[2]["plan"]["revision"]
        base = {"expected_revision": revision, "goal": {"primary_race_date": "2026-10-18", "a_target_time": "bad"}}
        self.assertEqual(self.request("/api/goal-impact-proposals", base)[0], 422)

    def test_completed_drawer_contains_linked_activity_without_empty_readonly_fields(self) -> None:
        from engine import database

        with database.get_db() as conn:
            workout = conn.execute(
                "SELECT * FROM planned_workouts WHERE date='2026-07-15' AND sport='running' LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(workout)
            workout_id = workout["id"]
            original = dict(workout)
            conn.execute(
                """INSERT INTO activities
                   (id,date,start_time,sport,name,distance_m,total_timer_s,avg_hr,max_hr,avg_cadence)
                   VALUES (990001,'2026-07-15','2026-07-15 18:30:00','running',
                           'drawer-activity-test',5000,1500,145,166,178)"""
            )
            conn.execute(
                "INSERT INTO planned_workout_activity_links (planned_workout_id,activity_id) VALUES (?,990001)",
                (workout_id,),
            )
            conn.execute(
                """UPDATE planned_workouts
                      SET compliance_status='completed',actual_activity_count=1,
                          actual_distance_km=5,actual_duration_min=25
                    WHERE id=?""",
                (workout_id,),
            )

        try:
            status, html, _ = self.request("/plan?view=month&month=2026-07")
            self.assertEqual(status, 200)
            self.assertIn("实际完成", html)
            self.assertIn("更多训练参数（RPE、心率、安全截断）", html)
            self.assertIn("drawer-activity-test", html)
            self.assertIn("actualActivityList", html)
        finally:
            with database.get_db() as conn:
                conn.execute("DELETE FROM planned_workout_activity_links WHERE activity_id=990001")
                conn.execute("DELETE FROM activities WHERE id=990001")
                conn.execute(
                    """UPDATE planned_workouts
                          SET compliance_status=?,actual_activity_count=?,actual_distance_km=?,
                              actual_duration_min=?,actual_activity_id=?,actual_tss=?
                        WHERE id=?""",
                    (
                        original["compliance_status"], original["actual_activity_count"],
                        original["actual_distance_km"], original["actual_duration_min"],
                        original["actual_activity_id"], original["actual_tss"], workout_id,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
