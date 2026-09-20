#!/usr/bin/env python3
"""TrainingEdge 冒烟测试 — 每次代码修改后运行，确保页面和 API 正常。

用法:
    python scripts/smoke_test.py              # 默认 http://127.0.0.1:8420
    python scripts/smoke_test.py --base http://host:port
    python scripts/smoke_test.py --offline     # 仅做模板 + 引擎层测试，不需要启动服务
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _test_offline() -> list[tuple[str, str]]:
    """不依赖 HTTP 服务，直接验证模板语法和引擎函数。"""
    results: list[tuple[str, str]] = []

    # Always initialize the selected test database. The caller should point
    # TRAININGEDGE_DB_PATH at a disposable path for isolated runs.
    try:
        sys.path.insert(0, str(ROOT))
        from engine import database as _database
        _database.init_db()
    except Exception as e:
        results.append(("数据库初始化", f"FAIL - {e}"))

    # 1. Jinja2 模板语法
    try:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(str(ROOT / "web" / "templates")))
        for name in (
            "dashboard.html", "plan.html", "goal.html", "activity.html",
            "base.html", "body_data.html", "settings.html",
        ):
            tpl_path = ROOT / "web" / "templates" / name
            if tpl_path.exists():
                try:
                    env.get_template(name)
                    results.append((f"模板语法/{name}", "PASS"))
                except Exception as e:
                    results.append((f"模板语法/{name}", f"FAIL - {e}"))
    except ImportError:
        results.append(("模板语法", "SKIP - jinja2 not installed"))

    # 2. Python AST 语法
    import ast
    for py_file in ["engine/readiness.py", "engine/database.py", "engine/plan_store.py", "api/app.py"]:
        fp = ROOT / py_file
        if fp.exists():
            try:
                ast.parse(fp.read_text())
                results.append((f"Python语法/{py_file}", "PASS"))
            except SyntaxError as e:
                results.append((f"Python语法/{py_file}", f"FAIL - {e}"))

    # 3. 引擎函数
    sys.path.insert(0, str(ROOT))
    try:
        from engine.readiness import (
            compute_readiness,
            compute_weekly_deviation,
            get_metric_comparisons,
            compute_decision_summary,
            compute_acwr,
            get_race_info,
        )
        from engine.database import get_db

        with get_db() as conn:
            r = compute_readiness(conn)
            results.append(("引擎/compute_readiness", "PASS" if r.status else "FAIL"))

            d = compute_weekly_deviation(conn)
            results.append(("引擎/compute_weekly_deviation", "PASS" if d.judgment else "FAIL"))

            mc = get_metric_comparisons(conn)
            results.append(("引擎/get_metric_comparisons", "PASS" if isinstance(mc, dict) else "FAIL"))

            ds = compute_decision_summary(conn)
            results.append(("引擎/compute_decision_summary", "PASS" if ds.get("today_status") else "FAIL"))

            aw = compute_acwr(conn)
            results.append(("引擎/compute_acwr", "PASS" if "acwr" in aw and "status" in aw else "FAIL"))

            ri = get_race_info(conn)
            results.append(("引擎/get_race_info", "PASS" if ri.get("name") and ri.get("date") else "FAIL"))

    except Exception as e:
        results.append(("引擎函数", f"FAIL - {e}"))

    # 4. 模板完整渲染
    try:
        from jinja2 import Environment, FileSystemLoader
        from engine.readiness import (
            compute_readiness,
            compute_weekly_deviation,
            get_metric_comparisons,
            compute_decision_summary,
            compute_acwr,
            get_race_info,
        )
        from engine import database

        env = Environment(loader=FileSystemLoader(str(ROOT / "web" / "templates")))
        tpl = env.get_template("dashboard.html")

        with database.get_db() as conn:
            readiness = compute_readiness(conn)
            deviation = compute_weekly_deviation(conn)
            metric_cards = get_metric_comparisons(conn)
            decision_summary = compute_decision_summary(conn)
            acwr = compute_acwr(conn)
            race_info = get_race_info(conn)
            activities = database.list_activities(conn, days=30, limit=10)
            fitness = database.list_fitness_history(conn, days=90)

            from datetime import date
            today_iso = date.today().isoformat()
            today_planned = conn.execute(
                "SELECT * FROM planned_workouts WHERE date = ? AND sport NOT IN ('rest','stretch') ORDER BY id",
                (today_iso,),
            ).fetchall()
            today_plan = [dict(p) for p in today_planned] if today_planned else []

            plan_match_map = {}
            matched_rows = conn.execute(
                """SELECT l.activity_id, pw.title AS plan_title, pw.compliance_status
                     FROM planned_workout_activity_links l
                     JOIN planned_workouts pw ON pw.id=l.planned_workout_id"""
            ).fetchall()
            for mr in matched_rows:
                try:
                    plan_match_map[int(mr["activity_id"])] = {
                        "title": mr["plan_title"], "status": mr["compliance_status"],
                    }
                except (ValueError, TypeError):
                    pass
            for act in activities:
                pm = plan_match_map.get(act["id"])
                act["plan_title"] = pm["title"] if pm else None
                act["plan_match"] = "match" if pm else None

            html = tpl.render(
                activities=activities,
                fitness=fitness,
                pdc_season=[],
                pdc_alltime=[],
                validation={},
                weekly={},
                wellness=[],
                readiness=readiness.to_dict(),
                deviation=deviation.to_dict(),
                metric_cards=metric_cards,
                decision_summary=decision_summary,
                today_plan=today_plan,
                acwr=acwr,
                race_info=race_info,
            )

            ok = len(html) > 1000 and "Traceback" not in html
            results.append(("模板渲染/dashboard.html", "PASS" if ok else f"FAIL - len={len(html)}"))
            results.append(("Dashboard/ACWR区块", "PASS" if "ACWR" in html else "FAIL - missing"))
            results.append(("Dashboard/赛事倒计时", "PASS" if "天" in html and race_info.get("days_left") is not None else "SKIP"))
    except Exception as e:
        results.append(("模板渲染/dashboard.html", f"FAIL - {e}"))

    return results


def _test_online(base: str) -> list[tuple[str, str]]:
    """通过 HTTP 请求验证所有页面和 API。"""
    results: list[tuple[str, str]] = []

    html_pages = [
        ("/", "训练面板"), ("/plan", "训练计划"), ("/goal", "目标与周期"),
        ("/body-data", "身体数据"), ("/settings", "设置"),
    ]
    for path, name in html_pages:
        try:
            r = urllib.request.urlopen(f"{base}{path}", timeout=10)
            body = r.read().decode()
            if r.status != 200:
                results.append((f"页面/{name}", f"FAIL - HTTP {r.status}"))
            elif "Internal Server Error" in body or "Traceback" in body:
                results.append((f"页面/{name}", "FAIL - server error in body"))
            elif "<html" not in body:
                results.append((f"页面/{name}", "FAIL - not HTML"))
            else:
                results.append((f"页面/{name}", "PASS"))
        except Exception as e:
            results.append((f"页面/{name}", f"FAIL - {e}"))

    api_endpoints = [
        ("/api/health", "health"),
        ("/api/readiness", "readiness"),
        ("/api/acwr", "acwr"),
        ("/api/race-info", "race-info"),
    ]
    for path, name in api_endpoints:
        try:
            r = urllib.request.urlopen(f"{base}{path}", timeout=10)
            body = r.read().decode()
            json.loads(body)
            results.append((f"API/{name}", "PASS"))
        except Exception as e:
            results.append((f"API/{name}", f"FAIL - {e}"))

    # Dashboard 关键区块检查（若启用密码保护，会返回登录页，视为正常）
    try:
        r = urllib.request.urlopen(f"{base}/", timeout=10)
        body = r.read().decode()
        if "登录 — TrainingEdge" in body or "login-card" in body:
            results.append(("Dashboard/密码保护", "PASS"))
        else:
            sections = {
                "状态评估卡": any(kw in body for kw in ("建议休息", "可正常训练", "可执行关键课", "建议恢复训练", "数据不足")),
                "指标卡网格": "metric-grid" in body,
                "训练负荷图": "fitnessChart" in body,
                "最近活动表": "最近活动" in body,
                "ACWR区块": "ACWR" in body,
            }
            for name, found in sections.items():
                results.append((f"Dashboard/{name}", "PASS" if found else "FAIL - missing"))
    except Exception as e:
        results.append(("Dashboard内容检查", f"FAIL - {e}"))

    return results


def main():
    parser = argparse.ArgumentParser(description="TrainingEdge 冒烟测试")
    parser.add_argument("--base", default="http://127.0.0.1:8420")
    parser.add_argument("--offline", action="store_true", help="仅做离线测试（模板 + 引擎），不需要服务运行")
    args = parser.parse_args()

    all_results: list[tuple[str, str]] = []

    print("=== TrainingEdge Smoke Test ===\n")

    # 离线测试（始终运行）
    print("--- 离线测试 ---")
    offline = _test_offline()
    all_results.extend(offline)
    for name, status in offline:
        icon = "✓" if status == "PASS" else "⊘" if status.startswith("SKIP") else "✗"
        print(f"  {icon} {name}: {status}")

    # 在线测试（除非 --offline）
    if not args.offline:
        print("\n--- 在线测试 ---")
        online = _test_online(args.base)
        all_results.extend(online)
        for name, status in online:
            icon = "✓" if status == "PASS" else "✗"
            print(f"  {icon} {name}: {status}")

    failed = [r for r in all_results if r[1] != "PASS" and not r[1].startswith("SKIP")]
    print(f"\n{'ALL PASS' if not failed else f'{len(failed)} FAILED'}")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
