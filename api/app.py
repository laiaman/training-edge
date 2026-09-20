"""FastAPI application — REST API + web dashboard for TrainingEdge."""

from __future__ import annotations

import asyncio
import calendar
import copy
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import hashlib
import hmac
import os
import secrets
import uuid

logger = logging.getLogger(__name__)

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from engine import database, metrics, validator, plan_store
from engine.auth import verify_api_key, get_or_create_api_key
from engine.readiness import (
    compute_readiness, compute_weekly_deviation,
    compute_body_trend_summary, get_metric_comparisons,
    get_body_comp_comparisons, compute_decision_summary,
    compute_acwr, get_race_info,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="TrainingEdge",
    description="自建运动数据分析平台 — FIT 解析 + 训练指标计算",
    version="0.1.0",
)


# ---------------------------------------------------------------------------
# Web Access Protection — simple password gate for public tunnel access
# ---------------------------------------------------------------------------

_ACCESS_PASSWORD = os.environ.get("TRAININGEDGE_PASSWORD", "")
_SESSION_SECRET = os.environ.get("TRAININGEDGE_SESSION_SECRET", secrets.token_hex(32))
_AUTH_COOKIE = "oc_session"
_PUBLIC_PATHS = {"/api/health", "/login", "/static"}
# 只读决策 API — 供教练 Skills / 本地脚本调用，无需登录（写操作仍受 API Key 保护）
_PUBLIC_API_PREFIXES = (
    "/api/readiness",
    "/api/weekly-deviation",
    "/api/decision-summary",
    "/api/acwr",
    "/api/race-info",
    "/api/body-trend-summary",
)


def _make_session_token(password: str) -> str:
    """Create HMAC session token from password."""
    return hmac.new(_SESSION_SECRET.encode(), password.encode(), hashlib.sha256).hexdigest()[:32]


class AccessGateMiddleware(BaseHTTPMiddleware):
    """Block unauthenticated web access when TRAININGEDGE_PASSWORD is set."""

    async def dispatch(self, request: Request, call_next):
        # Skip if no password configured (local/dev mode)
        if not _ACCESS_PASSWORD:
            request.state.web_authenticated = True
            return await call_next(request)

        path = request.url.path

        # Allow public paths
        if any(path.startswith(p) for p in _PUBLIC_PATHS):
            return await call_next(request)
        if any(path.startswith(p) for p in _PUBLIC_API_PREFIXES):
            return await call_next(request)

        # Allow API calls with valid API key
        if path.startswith("/api/") and (
            request.headers.get("X-API-Key") or request.query_params.get("api_key")
        ):
            return await call_next(request)

        # Check session cookie
        session = request.cookies.get(_AUTH_COOKIE, "")
        expected = _make_session_token(_ACCESS_PASSWORD)
        if hmac.compare_digest(session, expected):
            request.state.web_authenticated = True
            return await call_next(request)

        # Not authenticated → redirect to login
        return RedirectResponse(f"/login?next={path}", status_code=302)


app.add_middleware(AccessGateMiddleware)

BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = BASE_DIR / "web" / "static"
TEMPLATES_DIR = BASE_DIR / "web" / "templates"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

NAVIGATION_ITEMS = (
    {"key": "dashboard", "label": "面板", "href": "/", "description": "训练状态与近期执行概览"},
    {"key": "body", "label": "身体数据", "href": "/body-data", "description": "恢复、体重与健康趋势"},
    {"key": "goal", "label": "目标与周期", "href": "/goal", "description": "赛事目标、配速区间与周期分期"},
    {"key": "plan", "label": "训练计划", "href": "/plan", "description": "月/周课表与计划调整"},
    {"key": "settings", "label": "设置", "href": "/settings", "description": "模型、档案、同步与界面配置"},
)
DEFAULT_NAVIGATION_ORDER = [item["key"] for item in NAVIGATION_ITEMS]


def _ordered_navigation_items(raw_order: Any = None, *, strict: bool = False) -> List[Dict[str, str]]:
    try:
        order = json.loads(raw_order) if isinstance(raw_order, str) else raw_order
    except json.JSONDecodeError as exc:
        if strict:
            raise ValueError("导航排序必须是 JSON 数组") from exc
        order = None
    valid = (
        isinstance(order, list)
        and len(order) == len(DEFAULT_NAVIGATION_ORDER)
        and len(set(order)) == len(order)
        and set(order) == set(DEFAULT_NAVIGATION_ORDER)
    )
    if not valid:
        if strict and order is not None:
            raise ValueError("导航排序必须且只能包含全部现有 Tab")
        order = DEFAULT_NAVIGATION_ORDER
    by_key = {item["key"]: item for item in NAVIGATION_ITEMS}
    return [dict(by_key[key]) for key in order]


def _navigation_context(_: Request) -> Dict[str, Any]:
    with database.get_db() as conn:
        raw_order = database.get_setting(conn, "navigation_order")
    return {"navigation_items": _ordered_navigation_items(raw_order)}


templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR),
    context_processors=[_navigation_context],
)


# ---------------------------------------------------------------------------
# Auto-sync scheduler
# ---------------------------------------------------------------------------

_SYNC_INTERVAL_HOURS = int(os.environ.get("TRAININGEDGE_SYNC_INTERVAL_HOURS", "6"))
_sync_task: Optional[asyncio.Task] = None


async def _auto_sync_loop():
    """Background loop: sync Garmin activities + wellness every N hours."""
    from engine import sync as garmin_sync

    await asyncio.sleep(30)  # 启动后等 30 秒再首次同步，避免和 init_db 竞争

    while True:
        try:
            logger.info("[auto-sync] starting Garmin sync (interval=%dh)", _SYNC_INTERVAL_HOURS)

            # 同步最近 3 天活动
            try:
                act_result = garmin_sync.sync_recent(days=3)
                logger.info("[auto-sync] activities: synced %d activities", len(act_result))
            except Exception as e:
                logger.error("[auto-sync] activities sync failed: %s", e)

            # 同步最近 3 天 wellness（HRV / 睡眠）
            try:
                well_result = garmin_sync.sync_garmin_wellness(days=3)
                logger.info("[auto-sync] wellness: %s", well_result.get("message", well_result))
            except Exception as e:
                logger.error("[auto-sync] wellness sync failed: %s", e)

            # 同步后自动跑 match_compliance
            try:
                import sqlite3
                db_path = os.environ.get("TRAININGEDGE_DB_PATH", "/data/training_edge.db")
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    database.match_compliance(conn)
                logger.info("[auto-sync] match_compliance done")
            except Exception as e:
                logger.error("[auto-sync] match_compliance failed: %s", e)

        except Exception as e:
            logger.error("[auto-sync] unexpected error: %s", e)

        await asyncio.sleep(_SYNC_INTERVAL_HOURS * 3600)


@app.on_event("startup")
def startup():
    database.init_db()
    try:
        with database.get_db() as conn:
            plan_store.sync_plan_to_db(conn, plan_store.load_plan())
            database.reconcile_all_compliance(conn)
    except plan_store.PlanStoreError as exc:
        logger.error("[plan-store] startup sync paused: %s", exc)

    global _sync_task
    if _SYNC_INTERVAL_HOURS > 0:
        loop = asyncio.get_event_loop()
        _sync_task = loop.create_task(_auto_sync_loop())
        logger.info("[auto-sync] scheduled every %d hours", _SYNC_INTERVAL_HOURS)
    else:
        logger.info("[auto-sync] disabled (TRAININGEDGE_SYNC_INTERVAL_HOURS=0)")


# ---------------------------------------------------------------------------
# Login page (only active when TRAININGEDGE_PASSWORD is set)
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: str = ""):
    if not _ACCESS_PASSWORD:
        return RedirectResponse("/")
    return templates.TemplateResponse(request=request, name="login.html", context={
        "request": request, "next": next, "error": error,
    })


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    password = form.get("password", "")
    next_url = form.get("next", "/")

    if password == _ACCESS_PASSWORD:
        resp = RedirectResponse(next_url, status_code=302)
        # 根据请求协议判断是否设置 secure（HTTP 内网访问兼容）
        is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        resp.set_cookie(
            _AUTH_COOKIE,
            _make_session_token(_ACCESS_PASSWORD),
            httponly=True,
            secure=is_https,
            samesite="lax",
            max_age=86400 * 30,  # 30 days
        )
        return resp
    return RedirectResponse(f"/login?next={next_url}&error=密码错误", status_code=302)


# ═══════════════════════════════════════════════════════════════════════════════
# REST API — for skill consumption
# ═══════════════════════════════════════════════════════════════════════════════

# Health check - no auth required
@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.6.0"}


# Status - auth required
@app.get("/api/status", dependencies=[Depends(verify_api_key)])
async def status():
    database.init_db()
    with database.get_db() as conn:
        act_count = conn.execute("SELECT COUNT(*) as c FROM activities").fetchone()['c']
        last_sync = conn.execute("SELECT MAX(date) as d FROM activities").fetchone()['d']
        import os
        db_path = database.DB_PATH
        db_size_mb = os.path.getsize(db_path) / 1024 / 1024 if os.path.exists(str(db_path)) else 0
    return {
        "status": "ok",
        "activities": act_count,
        "last_sync": last_sync,
        "db_size_mb": round(db_size_mb, 2),
    }


# Summary endpoint for TrainingEdge Skill - auth required
@app.get("/api/summary", dependencies=[Depends(verify_api_key)])
async def summary():
    database.init_db()
    with database.get_db() as conn:
        # Fitness
        fitness_rows = conn.execute(
            "SELECT * FROM fitness_history ORDER BY date DESC LIMIT 1"
        ).fetchone()
        ctl = fitness_rows['ctl'] if fitness_rows else None
        atl = fitness_rows['atl'] if fitness_rows else None
        tsb = fitness_rows['tsb'] if fitness_rows else None

        # Weekly stats
        from engine.database import weekly_stats
        weekly = weekly_stats(conn)

        # Latest body comp
        from engine.database import get_latest_body_comp
        body = get_latest_body_comp(conn)

        # Today's plan
        from datetime import date as dt_date
        today = dt_date.today().isoformat()
        planned = conn.execute(
            "SELECT * FROM planned_workouts WHERE date=? ORDER BY id", (today,)
        ).fetchall()
        today_plan = [dict(p) for p in planned] if planned else []

        # Recent activities
        recent = conn.execute(
            "SELECT id, date, name, sport, tss, normalized_power, distance_m FROM activities ORDER BY date DESC LIMIT 5"
        ).fetchall()

        # Muscle fatigue
        from engine.database import get_muscle_fatigue
        fatigue = get_muscle_fatigue(conn, today)

        # TSB status text
        if tsb is not None:
            if tsb < -20:
                tsb_status = "建议休息或轻松恢复"
            elif tsb < 0:
                tsb_status = "正常训练，注意疲劳积累"
            elif tsb <= 15:
                tsb_status = "状态良好，可以安排强度训练"
            else:
                tsb_status = "可能脱训，需要增加训练量"
        else:
            tsb_status = "暂无数据"

    return {
        "date": today,
        "fitness": {
            "ctl": ctl, "atl": atl, "tsb": tsb,
            "status": tsb_status,
        },
        "body": dict(body) if body else None,
        "today_plan": today_plan,
        "muscle_fatigue": fatigue,
        "weekly": weekly,
        "recent_activities": [dict(r) for r in recent],
    }


@app.get("/api/activities", dependencies=[Depends(verify_api_key)])
def api_list_activities(
    sport: Optional[str] = None,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
):
    """List recent activities with computed metrics."""
    with database.get_db() as conn:
        activities = database.list_activities(conn, sport=sport, days=days, limit=limit)
    return {"ok": True, "count": len(activities), "activities": activities}


@app.get("/api/activity/{activity_id}", dependencies=[Depends(verify_api_key)])
def api_get_activity(activity_id: int):
    """Get a single activity with all computed metrics."""
    with database.get_db() as conn:
        activity = database.get_activity(conn, activity_id)
    if not activity:
        raise HTTPException(404, f"Activity {activity_id} not found")

    # Parse JSON fields
    for json_field in ["power_zones_json", "hr_zones_json", "pdc_json", "laps_json", "validation_json"]:
        if activity.get(json_field):
            activity[json_field.replace("_json", "")] = json.loads(activity[json_field])

    return {"ok": True, "activity": activity}


@app.get("/api/fitness", dependencies=[Depends(verify_api_key)])
def api_fitness_history(days: int = Query(90, ge=1, le=730)):
    """Get CTL/ATL/TSB history."""
    with database.get_db() as conn:
        history = database.list_fitness_history(conn, days=days)
    return {"ok": True, "count": len(history), "history": history}


@app.get("/api/wellness", dependencies=[Depends(verify_api_key)])
def api_wellness(days: int = Query(30, ge=1, le=365)):
    """Get daily wellness data."""
    with database.get_db() as conn:
        wellness = database.list_wellness(conn, days=days)
    return {"ok": True, "count": len(wellness), "wellness": wellness}


@app.get("/api/pdc", dependencies=[Depends(verify_api_key)])
def api_pdc_bests(days: int = Query(90, ge=1, le=365)):
    """Get power duration curve (best efforts)."""
    with database.get_db() as conn:
        bests = database.get_pdc_bests(conn, days=days)
    return {"ok": True, "bests": bests}


@app.get("/api/validation", dependencies=[Depends(verify_api_key)])
def api_validation(days: int = Query(30, ge=1, le=365)):
    """Get validation dashboard data."""
    return {"ok": True, "dashboard": validator.validation_dashboard(days)}


@app.post("/api/validate/{activity_id}", dependencies=[Depends(verify_api_key)])
def api_validate_activity(activity_id: int, intervals_data: Dict[str, Any]):
    """Validate a single activity against Intervals.icu data."""
    result = validator.validate_activity(activity_id, intervals_data)
    return {
        "ok": True,
        "result": {
            "activity_id": result.activity_id,
            "date": result.activity_date,
            "name": result.activity_name,
            "all_passed": result.all_passed,
            "summary": result.summary,
            "comparisons": [
                {
                    "field": c.field, "ours": c.ours, "theirs": c.theirs,
                    "diff": c.diff, "tolerance": c.tolerance,
                    "passed": c.passed, "note": c.note,
                }
                for c in result.comparisons
            ],
        },
    }


@app.get("/api/analyze/{activity_id}", dependencies=[Depends(verify_api_key)])
def api_analyze(activity_id: int):
    """Full analysis for a single activity — designed for skill consumption.

    Returns everything the AI skill needs in one call.
    """
    with database.get_db() as conn:
        activity = database.get_activity(conn, activity_id)
        if not activity:
            raise HTTPException(404, f"Activity {activity_id} not found")

        activity_date = activity.get("date", "")
        wellness = database.get_wellness(conn, activity_date) if activity_date else None
        fitness = conn.execute(
            "SELECT * FROM fitness_history WHERE date = ?", (activity_date,)
        ).fetchone()
        fitness = dict(fitness) if fitness else None

        # Parse JSON blobs
        pz = json.loads(activity["power_zones_json"]) if activity.get("power_zones_json") else None
        hz = json.loads(activity["hr_zones_json"]) if activity.get("hr_zones_json") else None
        pdc = json.loads(activity["pdc_json"]) if activity.get("pdc_json") else None
        laps = json.loads(activity["laps_json"]) if activity.get("laps_json") else None

    # Build the unified response the skill expects
    return {
        "ok": True,
        "activity": {
            "id": activity["id"],
            "name": activity["name"],
            "sport": activity["sport"],
            "date": activity["date"],
            "start_time": activity["start_time"],
            "distance_km": round(activity["distance_m"] / 1000, 2) if activity.get("distance_m") else None,
            "duration_min": round(activity["total_elapsed_s"] / 60, 1) if activity.get("total_elapsed_s") else None,
            "moving_duration_min": round(activity["total_timer_s"] / 60, 1) if activity.get("total_timer_s") else None,
            "avg_hr_bpm": activity["avg_hr"],
            "max_hr_bpm": activity["max_hr"],
            "avg_power_w": activity["avg_power"],
            "max_power_w": activity["max_power"],
            "normalized_power_w": activity["normalized_power"],
            "avg_speed_kph": round(activity["avg_speed"] * 3.6, 2) if activity.get("avg_speed") else None,
            "avg_cadence_rpm": activity["avg_cadence"],
            "elevation_gain_m": activity["total_ascent"],
            "calories_kcal": activity["total_calories"],
            "aerobic_te": activity["aerobic_te"],
            "anaerobic_te": activity["anaerobic_te"],
        },
        "training_load": {
            "tss": activity["tss"],
            "intensity_factor": activity["intensity_factor"],
            "ftp_w": activity["device_ftp"] or activity["estimated_ftp"],
            "estimated_ftp_w": activity["estimated_ftp"],
            "w_prime_j": activity["w_prime"],
            "xpower_w": activity["xpower"],
        },
        "fitness": {
            "ctl": fitness["ctl"] if fitness else None,
            "atl": fitness["atl"] if fitness else None,
            "tsb": fitness["tsb"] if fitness else None,
            "ramp_rate": fitness["ramp_rate"] if fitness else None,
        },
        "zones": {
            "power_zones": pz,
            "hr_zones": hz,
        },
        "drift": {
            "method": activity["drift_method"],
            "drift_pct": activity["drift_pct"],
            "classification": activity["drift_classification"],
        },
        "running": {
            "trimp": activity["trimp"],
            "vdot": activity["vdot"],
            "carbs_used_g": activity["carbs_used_g"],
        },
        "pdc": pdc,
        "laps": laps,
        "wellness": dict(wellness) if wellness else None,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Decision Cockpit APIs — v1 结论层接口
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/readiness")
def api_readiness():
    """今日训练就绪度评估。"""
    with database.get_db() as conn:
        result = compute_readiness(conn)
    return {"ok": True, **result.to_dict()}


@app.get("/api/weekly-deviation")
def api_weekly_deviation(week: Optional[str] = None):
    """本周训练执行偏差分析。"""
    with database.get_db() as conn:
        result = compute_weekly_deviation(conn, ref_date=week)
    return {"ok": True, **result.to_dict()}


@app.get("/api/body-trend-summary")
def api_body_trend_summary():
    """身体数据趋势结论。"""
    with database.get_db() as conn:
        result = compute_body_trend_summary(conn)
    return {"ok": True, **result.to_dict()}


@app.get("/api/decision-summary")
def api_decision_summary():
    """综合决策摘要 — 聚合就绪度、周偏差、身体趋势。"""
    with database.get_db() as conn:
        result = compute_decision_summary(conn)
    return {"ok": True, **result}


@app.get("/api/acwr")
def api_acwr():
    """ACWR 急慢性负荷比（近7天 vs 近28天日均 TSS）。"""
    with database.get_db() as conn:
        result = compute_acwr(conn)
    return {"ok": True, **result}


@app.get("/api/race-info")
def api_race_info():
    """目标赛事倒计时（canonical training_plan.goal 为唯一真源）。"""
    try:
        document = plan_store.load_plan()
    except plan_store.PlanStoreError as exc:
        raise HTTPException(503, str(exc)) from exc
    goal = document.get("goal")
    if not isinstance(goal, dict):
        raise HTTPException(503, "结构化计划缺少 goal")
    with database.get_db() as conn:
        result = get_race_info(conn, goal=goal)
    return {"ok": True, **result}


@app.get("/api/constraint-status")
def api_constraint_status():
    """本周计划约束满足情况检查。

    检查项：休息日约束、运动频率约束、连续高负荷天数约束。
    返回格式: {"constraints": [{"rule": "...", "status": "met/unmet/in_progress", "detail": "..."}]}
    """
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    with database.get_db() as conn:
        # 获取本周计划训练
        workouts = conn.execute(
            "SELECT * FROM planned_workouts WHERE date >= ? AND date <= ? ORDER BY date",
            (monday.isoformat(), sunday.isoformat()),
        ).fetchall()
        workouts = [dict(w) for w in workouts]

        # 获取本周实际活动
        activities = conn.execute(
            "SELECT * FROM activities WHERE date >= ? AND date <= ? ORDER BY date",
            (monday.isoformat(), sunday.isoformat()),
        ).fetchall()
        activities = [dict(a) for a in activities]
        linked_activity_ids = {
            str(row["activity_id"])
            for row in conn.execute(
                """SELECT l.activity_id
                     FROM planned_workout_activity_links l
                     JOIN planned_workouts pw ON pw.id=l.planned_workout_id
                    WHERE pw.date >= ? AND pw.date <= ?""",
                (monday.isoformat(), sunday.isoformat()),
            ).fetchall()
        }

        # 获取当前训练阶段约束
        try:
            from engine.plan_generator import detect_training_phase, _PHASE_CONSTRAINTS, TrainingPhase
            phase, _ = detect_training_phase(conn)
            phase_constraints = _PHASE_CONSTRAINTS.get(phase, _PHASE_CONSTRAINTS[TrainingPhase.BUILD])
        except Exception:
            phase_constraints = {"min_rest_days": 1, "max_intensity_days": 2, "max_daily_tss": 150}

    constraints = []
    week_done = today > sunday

    # ── 辅助：统计实际活动 ──
    active_dates = set(a.get("date", "")[:10] for a in activities)
    passed_dates = set()
    for i in range(7):
        d = monday + timedelta(days=i)
        if d <= today:
            passed_dates.add(d.isoformat())
    rest_dates_actual = passed_dates - active_dates
    rest_count_actual = len(rest_dates_actual)

    sport_actual: Dict[str, int] = {}
    for workout in workouts:
        if (workout.get("actual_activity_count") or 0) > 0:
            sport = workout.get("sport", "unknown")
            sport_actual[sport] = sport_actual.get(sport, 0) + 1
    for a in activities:
        if str(a.get("id")) in linked_activity_ids:
            continue
        s = a.get("sport", "unknown")
        sport_actual[s] = sport_actual.get(s, 0) + 1

    # 按日期汇总实际 TSS
    daily_tss: Dict[str, float] = {}
    for a in activities:
        d = a.get("date", "")[:10]
        daily_tss[d] = daily_tss.get(d, 0) + (a.get("tss") or 0)
    daily_planned_tss: Dict[str, float] = {}
    for w in workouts:
        d = w.get("date", "")[:10]
        daily_planned_tss[d] = daily_planned_tss.get(d, 0) + (w.get("target_tss") or 0)

    # ── 约束1: 跑步执行频率 ──
    running_count = sum(sport_actual.get(sport, 0) for sport in ("running", "trail_running", "treadmill_running"))
    planned_running_count = sum(1 for w in workouts if w.get("sport") == "running")
    running_target = max(1, planned_running_count)
    if week_done:
        status = "met" if running_count >= running_target else "unmet"
    else:
        status = "met" if running_count >= running_target else "in_progress"
    constraints.append({
        "rule": "按周计划完成跑步",
        "status": status,
        "detail": f"已完成 {running_count}/{running_target} 次",
    })

    # ── 约束2: 周三质量课、周日长距离 ──
    def _day_has_running(day: date, keywords: tuple[str, ...]) -> bool:
        day_text = day.isoformat()
        candidates = [w for w in workouts if w.get("date", "")[:10] == day_text and w.get("sport") == "running"]
        if day <= today:
            candidates += [a for a in activities if a.get("date", "")[:10] == day_text and a.get("sport") == "running"]
        text = " ".join(f"{item.get('title', '')} {item.get('name', '')} {item.get('description', '')}" for item in candidates).lower()
        return bool(candidates) and (not keywords or any(keyword in text for keyword in keywords))

    wednesday = monday + timedelta(days=2)
    sunday_run = monday + timedelta(days=6)
    quality_ok = _day_has_running(wednesday, ("间歇", "cv", "阈值", "tempo", "马配", "质量"))
    long_ok = _day_has_running(sunday_run, ("长", "long", "耐力"))
    anchors_ok = quality_ok and long_ok
    constraints.append({
        "rule": "周三质量课与周日长距离",
        "status": "met" if anchors_ok else ("unmet" if week_done else "in_progress"),
        "detail": f"周三{'已安排' if quality_ok else '待确认'}；周日{'已安排' if long_ok else '待确认'}",
    })

    # ── 约束3: 力量作为伤病预防辅助 ──
    strength_count = sport_actual.get("training", 0)
    planned_strength_count = sum(1 for w in workouts if w.get("sport") == "training")
    strength_target = planned_strength_count
    if week_done:
        status = "met" if strength_count >= strength_target else "unmet"
    else:
        status = "met" if strength_count >= strength_target else "in_progress"
    constraints.append({
        "rule": "力量与伤病预防",
        "status": status,
        "detail": f"已完成 {strength_count}/{strength_target} 次",
    })

    # ── 约束4: 避免连续 3 天高负荷 ──
    max_consecutive_high = 2
    high_tss_threshold = 80
    consecutive_high = 0
    max_found = 0
    for i in range(7):
        d = (monday + timedelta(days=i)).isoformat()
        tss = daily_tss.get(d, 0) if d <= today.isoformat() else daily_planned_tss.get(d, 0)
        if tss >= high_tss_threshold:
            consecutive_high += 1
            max_found = max(max_found, consecutive_high)
        else:
            consecutive_high = 0
    if max_found > max_consecutive_high:
        status = "unmet"
        detail = f"出现 {max_found} 天连续高负荷，超过上限"
    else:
        status = "met"
        detail = f"最长连续高负荷 {max_found} 天，当前满足"
    constraints.append({
        "rule": "避免连续 3 天高负荷",
        "status": status,
        "detail": detail,
    })

    # ── 约束5: 跑步质量课后次日不安排下肢大重量 ──
    intensity_dates = set()
    for w in workouts:
        if w.get("sport") == "running":
            intensity = (w.get("target_intensity") or "").lower()
            title = (w.get("title") or "").lower()
            if any(k in intensity for k in ("z4", "z5", "vo2", "threshold")) or "间歇" in title or "关键" in title:
                intensity_dates.add(w.get("date", "")[:10])
    for a in activities:
        if a.get("sport") == "running" and (a.get("tss") or 0) >= 80:
            intensity_dates.add(a.get("date", "")[:10])

    leg_conflict = False
    for intensity_date_str in intensity_dates:
        next_day = (date.fromisoformat(intensity_date_str) + timedelta(days=1)).isoformat()
        # 检查次日是否有力量训练（含腿部）
        for w in workouts:
            if w.get("date", "")[:10] == next_day and w.get("sport") == "training":
                mg = (w.get("muscle_groups") or w.get("muscle_groups_json") or "").lower()
                title = (w.get("title") or "").lower()
                if any(k in mg or k in title for k in ("quad", "leg", "hamstr", "glute", "下肢", "腿", "臀")):
                    leg_conflict = True
    constraints.append({
        "rule": "质量跑后次日不安排下肢大重量",
        "status": "unmet" if leg_conflict else "met",
        "detail": "存在冲突" if leg_conflict else "当前满足",
    })

    return {"ok": True, "week_start": monday.isoformat(), "week_end": sunday.isoformat(), "constraints": constraints}


# ═══════════════════════════════════════════════════════════════════════════════
# Web Dashboard — for visual monitoring
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    """Main dashboard page — v1 Decision Cockpit."""
    with database.get_db() as conn:
        activities = [
            activity for activity in database.list_activities(conn, days=365, limit=200)
            if activity.get("sport") in {"running", "training"}
        ][:100]
        fitness = database.list_fitness_history(conn, days=180)
        val = validator.validation_dashboard(30)
        wk_stats = database.weekly_stats(conn)
        wellness = database.list_wellness(conn, days=30)

        # v1: 结论层数据
        readiness = compute_readiness(conn)
        deviation = compute_weekly_deviation(conn)
        metric_cards = get_metric_comparisons(conn)
        decision_summary = compute_decision_summary(conn)
        acwr = compute_acwr(conn)
        try:
            document = plan_store.load_plan()
        except plan_store.PlanStoreError as exc:
            raise HTTPException(503, str(exc)) from exc
        race_info = get_race_info(conn, goal=document.get("goal"))

        # 今日计划训练
        from datetime import date as _date
        today_iso = _date.today().isoformat()
        today_planned = conn.execute(
            "SELECT * FROM planned_workouts WHERE date = ? AND sport NOT IN ('rest','stretch') ORDER BY id",
            (today_iso,),
        ).fetchall()
        today_plan = [dict(p) for p in today_planned] if today_planned else []

        # 活动 → 计划匹配（为最近活动表关联计划名称）
        plan_match_map = {}
        matched_rows = conn.execute(
            """SELECT l.activity_id, pw.title AS plan_title, pw.compliance_status
               FROM planned_workout_activity_links l
               JOIN planned_workouts pw ON pw.id=l.planned_workout_id"""
        ).fetchall()
        for mr in matched_rows:
            try:
                plan_match_map[int(mr["activity_id"])] = {
                    "title": mr["plan_title"], "status": mr["compliance_status"]
                }
            except (ValueError, TypeError):
                pass

        # 注入匹配信息到 activities + 跑步配速
        for act in activities:
            pm = plan_match_map.get(act["id"])
            if pm:
                act["plan_title"] = pm["title"]
                act["plan_match"] = "match"
            else:
                act["plan_title"] = None
                act["plan_match"] = None

            # 跑步配速（min/km），力量训练为 None
            act["pace_str"] = None
            if (act.get("sport") or "").lower() in ("running", "trail_running", "treadmill_running"):
                dist = act.get("distance_m") or 0
                dur = act.get("total_timer_s") or 0
                if dist > 100 and dur > 0:
                    sec_per_km = dur / (dist / 1000.0)
                    act["pace_str"] = f"{int(sec_per_km // 60)}'{int(sec_per_km % 60):02d}\""

    return templates.TemplateResponse(
        request=request, name="dashboard.html", context={
        "activities": activities,
        "fitness": fitness,
        "validation": val,
        "weekly": wk_stats,
        "wellness": wellness,
        # v1: Decision Cockpit
        "readiness": readiness.to_dict(),
        "deviation": deviation.to_dict(),
        "metric_cards": metric_cards,
        "decision_summary": decision_summary,
        "today_plan": today_plan,
        "acwr": acwr,
        "race_info": race_info,
    })


@app.get("/activity/{activity_id}", response_class=HTMLResponse)
def activity_detail(request: Request, activity_id: int):
    """Activity detail page."""
    with database.get_db() as conn:
        activity = database.get_activity(conn, activity_id)
        if not activity:
            raise HTTPException(404)

        # Get records for charts
        records = conn.execute(
            "SELECT * FROM records WHERE activity_id = ? ORDER BY offset_s",
            (activity_id,),
        ).fetchall()
        records = [dict(r) for r in records]

        # Get AI review if available
        ai_review = database.get_ai_review(conn, activity_id)

    # Parse JSON
    pz = json.loads(activity["power_zones_json"]) if activity.get("power_zones_json") else []
    hz = json.loads(activity["hr_zones_json"]) if activity.get("hr_zones_json") else []
    pdc = json.loads(activity["pdc_json"]) if activity.get("pdc_json") else {}
    laps = json.loads(activity["laps_json"]) if activity.get("laps_json") else []
    validation = json.loads(activity["validation_json"]) if activity.get("validation_json") else None

    return templates.TemplateResponse(request=request, name="activity.html", context={
        "request": request,
        "activity": activity,
        "records": records,
        "power_zones": pz,
        "hr_zones": hz,
        "pdc": pdc,
        "laps": laps,
        "validation": validation,
        "ai_review": ai_review,
    })


def _parse_target_time(value: str) -> int:
    try:
        hours, minutes, seconds = (int(part) for part in value.split(":"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(422, "目标时间格式必须为 HH:MM:SS") from exc
    if hours < 1 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise HTTPException(422, "目标时间超出有效范围")
    return hours * 3600 + minutes * 60 + seconds


def _format_pace(seconds: float) -> str:
    # Goal pace is a ceiling: rounding up could make the displayed pace miss the target.
    whole_seconds = max(1, int(seconds))
    return f"{whole_seconds // 60:02d}:{whole_seconds % 60:02d}"


def _pace_range(base_seconds: float, lower_offset: int, upper_offset: int) -> str:
    return f"{_format_pace(base_seconds + lower_offset)}-{_format_pace(base_seconds + upper_offset)}"


def _replace_pace_value(value: Any, old_pace: str, new_pace: str, old_display: str, new_display: str) -> Any:
    if isinstance(value, str):
        return value.replace(old_pace, new_pace).replace(old_display, new_display)
    if isinstance(value, list):
        return [_replace_pace_value(item, old_pace, new_pace, old_display, new_display) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_pace_value(item, old_pace, new_pace, old_display, new_display)
            for key, item in value.items()
        }
    return value


def _build_pace_zones(target_seconds: int) -> Dict[str, Dict[str, str]]:
    marathon_pace = target_seconds / 42.195
    return {
        "recovery": {"pace": _pace_range(marathon_pace, 82, 112), "hr": "<120", "rpe": "1-2"},
        "easy": {"pace": _pace_range(marathon_pace, 52, 92), "hr": "120-140", "rpe": "3-4"},
        "aerobic_tempo": {"pace": _pace_range(marathon_pace, 27, 42), "hr": "135-145", "rpe": "5"},
        "steady": {"pace": _pace_range(marathon_pace, 12, 27), "hr": "145-155", "rpe": "6"},
        "marathon": {"pace": _pace_range(marathon_pace, 0, 5), "hr": "148-162", "rpe": "7-8"},
        "cv": {"pace": _pace_range(marathon_pace, -23, -18), "hr": "160-170", "rpe": "7-9"},
        "interval": {"pace": _pace_range(marathon_pace, -33, -23), "hr": "165-175", "rpe": "8-9"},
        "strides": {"pace": _pace_range(marathon_pace, -48, -28), "hr": "170+", "rpe": "9-10"},
    }


def _phase_for_date(workout_date: date, race_date: date) -> str:
    days_left = (race_date - workout_date).days
    if days_left < 0:
        return "赛后恢复期"
    if days_left == 0:
        return "比赛日"
    if days_left <= 14:
        return "减量期"
    if days_left <= 42:
        return "巅峰专项期"
    if days_left <= 84:
        return "专项构建期"
    return "基础整合期"


PACE_ZONE_DESCRIPTIONS = {
    "recovery": {"label": "恢复跑", "description": "疲劳日或高强度课后使用，以促进恢复为目的。"},
    "easy": {"label": "轻松有氧", "description": "建立有氧基础和累计跑量，应能轻松交谈。"},
    "aerobic_tempo": {"label": "有氧节奏", "description": "基础期长跑前段的可持续有氧强度。"},
    "steady": {"label": "稳态跑", "description": "介于轻松跑和马配之间，强化持续有氧耐力。"},
    "marathon": {"label": "马拉松配速", "description": "目标比赛专项强度，用于马配巡航和长跑后段。"},
    "cv": {"label": "CV／乳酸阈", "description": "可控的快，提升乳酸清除和阈值能力，不追求力竭。"},
    "interval": {"label": "短间歇", "description": "提升 VO₂max 与速度耐力，需严格控制总量和恢复。"},
    "strides": {"label": "冲刺／神经激活", "description": "短时快速且充分恢复，用于神经肌肉唤醒。"},
}


@app.get("/goal", response_class=HTMLResponse)
def goal_page(request: Request):
    try:
        document = plan_store.load_plan()
    except plan_store.PlanStoreError as exc:
        raise HTTPException(503, str(exc)) from exc
    return templates.TemplateResponse(request=request, name="goal.html", context={
        "request": request,
        "goal": document["goal"],
        "pace_zones": document.get("pace_zones", {}),
        "pace_zone_descriptions": PACE_ZONE_DESCRIPTIONS,
        "plan_revision": document["revision"],
    })


@app.post("/api/goal-impact-proposals", dependencies=[Depends(verify_api_key)])
async def api_goal_impact_proposal(request: Request):
    data = await request.json()
    document = plan_store.load_plan()
    if data.get("expected_revision") != document["revision"]:
        raise HTTPException(409, "计划已更新，请刷新后重新预览影响")
    draft = data.get("goal") or {}
    target_seconds = _parse_target_time(draft.get("a_target_time", ""))
    try:
        race_date = date.fromisoformat(draft.get("primary_race_date", ""))
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "主赛日期格式必须为 YYYY-MM-DD") from exc
    goal_patch = {
        key: draft[key] for key in (
            "primary_race", "primary_race_date", "secondary_race",
            "secondary_race_date", "a_target_time", "b_target_time", "priority",
        ) if key in draft
    }
    goal_patch["marathon_pace"] = _format_pace(target_seconds / 42.195)

    future_workouts = [
        workout for workout in document.get("workouts", [])
        if workout.get("date", "") >= date.today().isoformat()
    ]
    phase_by_uid = {
        workout["uid"]: _phase_for_date(date.fromisoformat(workout["date"]), race_date)
        for workout in future_workouts
    }
    secondary_date_text = goal_patch.get("secondary_race_date") or document["goal"].get("secondary_race_date")
    try:
        secondary_date = date.fromisoformat(secondary_date_text) if secondary_date_text else race_date
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "备选赛日期格式必须为 YYYY-MM-DD") from exc
    plan_end = max(race_date, secondary_date)
    changes: List[Dict[str, Any]] = [
        {
            "kind": "goal", "patch": goal_patch, "label": "赛事目标",
            "before": document["goal"], "after": goal_patch,
        },
        {
            "kind": "pace_zones", "value": _build_pace_zones(target_seconds), "label": "配速区间",
            "before": document.get("pace_zones", {}), "after": _build_pace_zones(target_seconds),
        },
        {
            "kind": "phase_partition", "plan_patch": {"end_date": plan_end.isoformat()},
            "phase_by_uid": phase_by_uid, "label": "周期分期",
            "before": {"future_workouts": len(future_workouts)},
            "after": {"race_date": race_date.isoformat(), "future_workouts": len(phase_by_uid)},
        },
    ]
    old_pace = document["goal"].get("marathon_pace", "")
    new_pace = goal_patch["marathon_pace"]
    replacements = []
    if old_pace and old_pace != new_pace:
        old_display = f"{int(old_pace[:2])}'{old_pace[-2:]}\""
        new_display = f"{int(new_pace[:2])}'{new_pace[-2:]}\""
        for workout in future_workouts:
            patch = {}
            for field in ("description", "target_intensity", "target_pace_text", "steps"):
                value = workout.get(field)
                replaced = _replace_pace_value(value, old_pace, new_pace, old_display, new_display)
                if replaced != value:
                    patch[field] = replaced
            if patch:
                replacements.append({"uid": workout["uid"], "patch": patch})
    changes.append({
        "kind": "workout_batch", "changes": replacements, "label": "未来课表中的目标马配",
        "before": {"affected": len(replacements), "pace": old_pace},
        "after": {"affected": len(replacements), "pace": new_pace},
    })
    with database.get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO plan_change_proposals (base_revision, status, reason, changes_json)
               VALUES (?, 'pending', ?, ?)""",
            (document["revision"], "目标调整影响预览", json.dumps(changes, ensure_ascii=False)),
        )
        proposal_id = int(cursor.lastrowid)
    return {"ok": True, "proposal_id": proposal_id, "base_revision": document["revision"], "changes": changes}


@app.get("/plan", response_class=HTMLResponse)
def running_plan_page(
    request: Request,
    view: str = "month",
    month: Optional[str] = None,
    week: Optional[str] = None,
):
    """Running-first month calendar with an optional detailed week view."""
    if view not in {"month", "week"}:
        raise HTTPException(400, "view 必须为 month 或 week")
    today = date.today()
    try:
        if view == "week":
            reference = date.fromisoformat(week) if week else today
            range_start = reference - timedelta(days=reference.weekday())
            range_end = range_start + timedelta(days=6)
            month_reference = reference.replace(day=1)
        else:
            month_reference = datetime.strptime(month, "%Y-%m").date() if month else today.replace(day=1)
            first_day = month_reference
            last_day = month_reference.replace(day=calendar.monthrange(month_reference.year, month_reference.month)[1])
            range_start = first_day - timedelta(days=first_day.weekday())
            range_end = last_day + timedelta(days=6 - last_day.weekday())
    except ValueError as exc:
        raise HTTPException(400, "日期格式无效") from exc

    if view == "month":
        summary_start, summary_end = first_day, last_day
        summary_scope_label = "本月"
    else:
        summary_start, summary_end = range_start, range_end
        summary_scope_label = "本周"

    with database.get_db() as conn:
        database.match_compliance(conn, None)
        workouts = database.list_planned_workouts(conn, range_start.isoformat(), range_end.isoformat())
        activities = [dict(row) for row in conn.execute(
            "SELECT * FROM activities WHERE date>=? AND date<=? ORDER BY date, start_time",
            (range_start.isoformat(), range_end.isoformat()),
        ).fetchall()]
        linked_activity_rows = conn.execute(
            """SELECT l.planned_workout_id,
                      a.id, a.date, a.start_time, a.sport, a.name,
                      a.distance_m, a.total_timer_s, a.avg_hr, a.max_hr,
                      a.avg_cadence, a.total_ascent, a.aerobic_te,
                      a.anaerobic_te, a.tss
                 FROM planned_workout_activity_links l
                 JOIN planned_workouts pw ON pw.id=l.planned_workout_id
                 JOIN activities a ON a.id=l.activity_id
                WHERE pw.date>=? AND pw.date<=?
                ORDER BY pw.date, a.start_time, a.id""",
            (range_start.isoformat(), range_end.isoformat()),
        ).fetchall()
        has_api_key = bool(database.get_setting(conn, "llm_api_key"))
        readiness = compute_readiness(conn).to_dict()

    try:
        plan_document = plan_store.load_plan()
    except plan_store.PlanStoreError as exc:
        raise HTTPException(503, str(exc)) from exc

    linked_activities_by_workout: Dict[int, List[Dict[str, Any]]] = {}
    for row in linked_activity_rows:
        activity = dict(row)
        workout_id = activity.pop("planned_workout_id")
        distance_m = activity.get("distance_m") or 0
        duration_s = activity.get("total_timer_s") or 0
        activity["pace_seconds_per_km"] = (
            duration_s / (distance_m / 1000.0)
            if distance_m > 0 and duration_s > 0 else None
        )
        linked_activities_by_workout.setdefault(workout_id, []).append(activity)

    workouts_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for workout in workouts:
        try:
            workout["steps"] = json.loads(workout.get("workout_steps_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            workout["steps"] = []
        workout["actual_activities"] = linked_activities_by_workout.get(workout["id"], [])
        workouts_by_date.setdefault(workout["date"], []).append(workout)
    activities_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for activity in activities:
        activity_summary = {
            key: activity.get(key) for key in (
                "id", "date", "start_time", "sport", "name", "distance_m",
                "total_timer_s", "avg_hr", "max_hr", "avg_cadence",
            )
        }
        activities_by_date.setdefault((activity.get("date") or "")[:10], []).append(activity_summary)

    days = []
    cursor = range_start
    while cursor <= range_end:
        day_workouts = workouts_by_date.get(cursor.isoformat(), [])
        days.append({
            "date": cursor.isoformat(),
            "day": cursor.day,
            "day_name": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][cursor.weekday()],
            "in_month": cursor.month == month_reference.month,
            "is_today": cursor == today,
            "is_current_week": cursor - timedelta(days=cursor.weekday()) == today - timedelta(days=today.weekday()),
            "workouts": day_workouts,
            "activities": activities_by_date.get(cursor.isoformat(), []),
        })
        cursor += timedelta(days=1)

    calendar_weeks = []
    for index in range(0, len(days), 7):
        week_days = days[index:index + 7]
        week_workouts = [workout for day_item in week_days for workout in day_item["workouts"]]
        running_workouts = [workout for workout in week_workouts if workout.get("sport") == "running"]
        label = next((workout.get("week_label") for workout in week_workouts if workout.get("week_label")), "")
        phase = next((workout.get("phase") for workout in week_workouts if workout.get("phase")), "")
        calendar_weeks.append({
            "days": week_days,
            "label": label,
            "phase": phase,
            "is_current": any(day_item["is_current_week"] for day_item in week_days),
            "planned_km": sum(workout.get("target_distance_km") or 0 for workout in running_workouts),
            "completed": sum(1 for workout in week_workouts if workout.get("compliance_status") == "completed"),
            "total": len([workout for workout in week_workouts if workout.get("sport") not in {"rest", "stretch"}]),
        })

    summary_workouts = [
        workout for workout in workouts
        if summary_start.isoformat() <= workout.get("date", "") <= summary_end.isoformat()
    ]
    summary_activities = [
        activity for activity in activities
        if summary_start.isoformat() <= (activity.get("date") or "")[:10] <= summary_end.isoformat()
    ]
    visible_running = [workout for workout in summary_workouts if workout.get("sport") == "running"]
    actual_running = [
        activity for activity in summary_activities
        if activity.get("sport") in {"running", "trail_running", "treadmill_running"}
    ]
    key_workouts = [workout for workout in visible_running if workout.get("is_key_workout")]
    summary = {
        "planned_km": sum(workout.get("target_distance_km") or 0 for workout in visible_running),
        "actual_km": sum(activity.get("distance_m") or 0 for activity in actual_running) / 1000.0,
        "key_completed": sum(1 for workout in key_workouts if workout.get("compliance_status") == "completed"),
        "key_partial": sum(1 for workout in key_workouts if workout.get("compliance_status") == "partial"),
        "key_total": len(key_workouts),
        "strength_total": sum(1 for workout in summary_workouts if workout.get("sport") == "training"),
        "scope_label": summary_scope_label,
    }
    previous_month = (month_reference - timedelta(days=1)).replace(day=1)
    next_month = (month_reference.replace(day=calendar.monthrange(month_reference.year, month_reference.month)[1]) + timedelta(days=1)).replace(day=1)
    current_week_start = today - timedelta(days=today.weekday())

    return templates.TemplateResponse(request=request, name="plan.html", context={
        "request": request,
        "view": view,
        "month_value": month_reference.strftime("%Y-%m"),
        "month_title": f"{month_reference.year}年{month_reference.month}月",
        "previous_month": previous_month.strftime("%Y-%m"),
        "next_month": next_month.strftime("%Y-%m"),
        "range_start": range_start.isoformat(),
        "range_end": range_end.isoformat(),
        "calendar_weeks": calendar_weeks,
        "week_days": days if view == "week" else [],
        "current_week_start": current_week_start.isoformat(),
        "selected_week_start": range_start.isoformat(),
        "previous_week": (range_start - timedelta(days=7)).isoformat(),
        "next_week": (range_start + timedelta(days=7)).isoformat(),
        "summary": summary,
        "plan_revision": plan_document["revision"],
        "plan_info": plan_document["plan"],
        "goal": plan_document["goal"],
        "has_api_key": has_api_key,
        "readiness": readiness,
    })


@app.get("/plan/legacy", response_class=HTMLResponse)
def plan_page(request: Request, week: Optional[str] = None):
    """Training plan weekly calendar page."""
    target = f"/plan?view=week&week={week}" if week else "/plan?view=week"
    return RedirectResponse(target, status_code=307)

    ref = date.fromisoformat(week) if week else date.today()
    week_start = ref - timedelta(days=ref.weekday())  # Monday
    week_end = week_start + timedelta(days=6)          # Sunday

    with database.get_db() as conn:
        # Auto-match actual activities to planned workouts
        database.match_compliance(conn, None)
        conn.commit()

        workouts = database.list_planned_workouts(conn, week_start.isoformat(), week_end.isoformat())

        # Actual activities for the week
        activities = conn.execute(
            "SELECT * FROM activities WHERE date >= ? AND date <= ? ORDER BY date",
            (week_start.isoformat(), week_end.isoformat()),
        ).fetchall()
        activities = [dict(a) for a in activities]

        # Muscle fatigue for today
        muscle_fatigue = database.get_muscle_fatigue(conn, date.today().isoformat())

        # Latest fitness (CTL/ATL/TSB)
        fitness_row = conn.execute(
            "SELECT * FROM fitness_history ORDER BY date DESC LIMIT 1"
        ).fetchone()
        fitness = dict(fitness_row) if fitness_row else None

        # Check if API key is configured
        has_api_key = bool(database.get_setting(conn, "llm_api_key"))

        # Load athlete profile from settings
        from engine.plan_generator import (
            DEFAULT_PROFILE, detect_training_phase, _PHASE_CONSTRAINTS,
            TrainingPhase,
        )
        athlete = dict(DEFAULT_PROFILE)
        for key in DEFAULT_PROFILE:
            val = database.get_setting(conn, f"athlete_{key}")
            if val:
                if key in ("ftp", "max_hr", "resting_hr", "weekly_hours_available"):
                    athlete[key] = float(val) if "." in val else int(val)
                elif key == "constraints":
                    try:
                        athlete[key] = json.loads(val)
                    except Exception:
                        pass
                else:
                    athlete[key] = val

        # ── P0: Training phase & trigger info ──
        try:
            phase, phase_reason = detect_training_phase(conn)
        except Exception:
            phase, phase_reason = TrainingPhase.BASE, "检测失败，默认基础期"
        phase_constraints = _PHASE_CONSTRAINTS.get(phase, _PHASE_CONSTRAINTS[TrainingPhase.BUILD])

        # Last generation metadata (AI reasoning)
        last_plan_phase = database.get_setting(conn, "last_plan_phase") or ""
        last_plan_trigger = database.get_setting(conn, "last_plan_trigger") or ""
        last_plan_generated_at = database.get_setting(conn, "last_plan_generated_at") or ""

        # ── P0: Week summary stats ──
        sport_counts = {}
        total_duration_min = 0
        for w in workouts:
            s = w.get("sport", "rest")
            sport_counts[s] = sport_counts.get(s, 0) + 1
            total_duration_min += (w.get("target_duration_min") or 0)
        total_hours = total_duration_min / 60

        # ── P1: Last week actual stats ──
        prev_monday = week_start - timedelta(days=7)
        prev_sunday = prev_monday + timedelta(days=6)
        prev_workouts = database.list_planned_workouts(conn, prev_monday.isoformat(), prev_sunday.isoformat())
        prev_activities = conn.execute(
            "SELECT * FROM activities WHERE date >= ? AND date <= ? ORDER BY date",
            (prev_monday.isoformat(), prev_sunday.isoformat()),
        ).fetchall()
        prev_activities = [dict(a) for a in prev_activities]
        prev_tss_planned = sum(w.get('target_tss') or 0 for w in prev_workouts)
        prev_tss_actual = (
            sum(a['tss'] for a in prev_activities)
            if prev_activities and all(a.get('tss') is not None for a in prev_activities)
            else None
        )
        prev_completed = sum(1 for w in prev_workouts if w.get('compliance_status') == 'completed')
        prev_total = len(prev_workouts)

        # ── P1: Next week preview ──
        next_monday = week_start + timedelta(days=7)
        next_sunday = next_monday + timedelta(days=6)
        next_workouts = database.list_planned_workouts(conn, next_monday.isoformat(), next_sunday.isoformat())
        next_tss_planned = sum(w.get('target_tss') or 0 for w in next_workouts)
        next_sport_counts = {}
        for w in next_workouts:
            s = w.get("sport", "rest")
            next_sport_counts[s] = next_sport_counts.get(s, 0) + 1

    # Build weekdays
    day_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    weekdays = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        ds = d.isoformat()
        day_workouts = [w for w in workouts if w['date'] == ds]
        day_activities = [a for a in activities if (a.get('date') or '')[:10] == ds]
        weekdays.append({
            'date': ds,
            'day_name': day_names[i],
            'day_short': f'{d.month}/{d.day}',
            'workouts': day_workouts,
            'activities': day_activities,
            'is_today': d == date.today(),
        })

    week_tss_planned = sum(w.get('target_tss') or 0 for w in workouts)
    week_tss_actual = (
        sum(a['tss'] for a in activities)
        if activities and all(a.get('tss') is not None for a in activities)
        else None
    )
    completed = sum(1 for w in workouts if w.get('compliance_status') == 'completed')

    prev_week = (week_start - timedelta(days=7)).isoformat()
    next_week = (week_start + timedelta(days=7)).isoformat()

    # Phase display names
    phase_names = {
        "base": "基础期 Base", "build": "构建期 Build", "peak": "巅峰期 Peak",
        "recovery": "恢复期 Recovery", "transition": "过渡期 Transition",
    }

    # v1: 偏差分析 + 就绪度
    with database.get_db() as conn_inner:
        deviation = compute_weekly_deviation(conn_inner, ref_date=week_start.isoformat())
        readiness = compute_readiness(conn_inner)

    return templates.TemplateResponse(request=request, name="plan.html", context={
        "request": request,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "week_start_display": f"{week_start.month}月{week_start.day}日",
        "week_end_display": f"{week_end.month}月{week_end.day}日",
        "prev_week": prev_week,
        "next_week": next_week,
        "weekdays": weekdays,
        "workouts": workouts,
        "muscle_fatigue": muscle_fatigue,
        "week_tss_planned": week_tss_planned,
        "week_tss_actual": week_tss_actual,
        "completed": completed,
        "total_planned": len(workouts),
        "fitness": fitness,
        "has_api_key": has_api_key,
        "athlete": athlete,
        # P0: Phase & reasoning
        "phase": phase,
        "phase_name": phase_names.get(phase, phase),
        "phase_reason": phase_reason,
        "phase_constraints": phase_constraints,
        "last_plan_trigger": last_plan_trigger,
        "last_plan_generated_at": last_plan_generated_at,
        # P0: Week summary
        "sport_counts": sport_counts,
        "total_hours": total_hours,
        # P1: Last week
        "prev_tss_planned": prev_tss_planned,
        "prev_tss_actual": prev_tss_actual,
        "prev_completed": prev_completed,
        "prev_total": prev_total,
        # P1: Next week
        "next_tss_planned": next_tss_planned,
        "next_sport_counts": next_sport_counts,
        "next_total": len(next_workouts),
        # v1: Decision Cockpit
        "deviation": deviation.to_dict(),
        "readiness": readiness.to_dict(),
    })


@app.post("/api/workouts", dependencies=[Depends(verify_api_key)])
async def api_upsert_workout(request: Request):
    """Create or update a canonical workout with optimistic locking."""
    data = await request.json()
    expected_revision = data.pop("expected_revision", None)
    uid = str(data.pop("uid", "") or "")
    if expected_revision is None:
        raise HTTPException(409, "缺少 expected_revision，请刷新计划后重试")
    try:
        with database.get_db() as conn:
            document = plan_store.load_plan()
            if uid:
                candidate = plan_store.apply_workout_changes(
                    document, [{"uid": uid, "patch": data}]
                )
            else:
                candidate = copy.deepcopy(document)
                workout = {
                    "uid": f"manual-{data.get('date', date.today().isoformat())}-{uuid.uuid4().hex[:8]}",
                    **data,
                }
                candidate.setdefault("workouts", []).append(workout)
            saved = plan_store.save_plan(
                conn, candidate, expected_revision=int(expected_revision), source="web"
            )
        return {"ok": True, "revision": saved["revision"]}
    except plan_store.PlanConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except plan_store.CompletedWorkoutLockedError as exc:
        raise HTTPException(423, str(exc)) from exc
    except plan_store.PlanStoreError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/workouts/{workout_id}", dependencies=[Depends(verify_api_key)])
def api_delete_workout(workout_id: int):
    """Deletion is intentionally disabled until it can carry a revision token."""
    raise HTTPException(405, "请在训练详情中将未来训练标记为休息，不直接删除")


@app.get("/api/workouts", dependencies=[Depends(verify_api_key)])
def api_list_workouts(
    date_from: str = Query(...),
    date_to: str = Query(...),
):
    """List planned workouts for a date range."""
    with database.get_db() as conn:
        rows = database.list_planned_workouts(conn, date_from, date_to)
    return {"ok": True, "count": len(rows), "workouts": rows}


@app.get("/api/plan-document", dependencies=[Depends(verify_api_key)])
def api_plan_document():
    try:
        document = plan_store.load_plan()
    except plan_store.PlanStoreError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"ok": True, "plan": document}


@app.post("/api/plan-proposals/{proposal_id}/apply", dependencies=[Depends(verify_api_key)])
async def api_apply_plan_proposal(proposal_id: int, request: Request):
    data = await request.json()
    selected = data.get("selected", [])
    if not isinstance(selected, list):
        raise HTTPException(422, "selected 必须是变更序号数组")
    try:
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT * FROM plan_change_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "找不到调整提案")
            if row["status"] != "pending":
                raise HTTPException(409, "调整提案已处理")
            document = plan_store.load_plan()
            if document["revision"] != row["base_revision"]:
                raise plan_store.PlanConflictError("计划已更新，请重新生成 AI 调整提案")
            changes = json.loads(row["changes_json"])
            chosen = [changes[index] for index in selected if isinstance(index, int) and 0 <= index < len(changes)]
            if not chosen:
                raise HTTPException(422, "至少选择一项变更")
            candidate = plan_store.apply_plan_changes(document, chosen)
            saved = plan_store.save_plan(
                conn, candidate, expected_revision=row["base_revision"], source="ai_proposal_confirmed"
            )
            conn.execute(
                "UPDATE plan_change_proposals SET status='applied', applied_at=datetime('now') WHERE id=?",
                (proposal_id,),
            )
        return {"ok": True, "revision": saved["revision"], "applied": len(chosen)}
    except plan_store.PlanConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except plan_store.CompletedWorkoutLockedError as exc:
        raise HTTPException(423, str(exc)) from exc
    except plan_store.PlanStoreError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/templates", dependencies=[Depends(verify_api_key)])
async def api_upsert_template(request: Request):
    """Create or update a weekly template."""
    data = await request.json()
    with database.get_db() as conn:
        database.upsert_weekly_template(conn, data)
    return {"ok": True}


@app.get("/api/templates", dependencies=[Depends(verify_api_key)])
def api_list_templates():
    """List all weekly templates."""
    with database.get_db() as conn:
        rows = database.list_weekly_templates(conn)
    return {"ok": True, "count": len(rows), "templates": rows}


@app.post("/api/generate-plan")
async def api_generate_plan(request: Request):
    """Generate a pending AI proposal; never overwrite the active plan.

    Body JSON:
      - profile: optional athlete profile overrides
      - week_offset: 0=本周, 1=下周 (default 1)
    """
    data = await request.json()
    profile = data.get("profile")
    week_offset = data.get("week_offset", 1)

    try:
        from engine.plan_generator import generate_weekly_plan
        with database.get_db() as conn:
            generated = generate_weekly_plan(conn, profile=profile, week_offset=week_offset)
            document = plan_store.load_plan()
            current_by_date_sport = {
                (workout["date"], workout["sport"]): workout
                for workout in document.get("workouts", [])
                if workout.get("date") >= date.today().isoformat()
            }
            changes = []
            comparable_fields = (
                "title", "description", "target_distance_km", "target_duration_min",
                "target_tss", "target_intensity", "target_pace_text", "target_hr_min",
                "target_hr_max", "target_hr_text", "target_rpe", "target_rpe_min",
                "target_rpe_max", "target_duration_source", "target_tss_source", "steps",
                "coach_note", "safety_cutoff",
            )
            for suggestion in generated:
                current = current_by_date_sport.get((suggestion.get("date"), suggestion.get("sport")))
                if not current:
                    continue
                patch = {
                    field: suggestion.get(field)
                    for field in comparable_fields
                    if field in suggestion and suggestion.get(field) != current.get(field)
                }
                if patch:
                    changes.append({
                        "uid": current["uid"],
                        "patch": patch,
                        "label": current.get("title") or current["date"],
                        "date": current["date"],
                        "before": {field: current.get(field) for field in patch},
                        "after": patch,
                    })
            if not changes:
                return {"ok": True, "proposal_id": None, "changes": [], "message": "AI 未提出需要修改的项目"}
            cursor = conn.execute(
                """INSERT INTO plan_change_proposals
                   (base_revision, status, reason, changes_json)
                   VALUES (?, 'pending', ?, ?)""",
                (
                    document["revision"],
                    "用户主动请求 AI 调整",
                    json.dumps(changes, ensure_ascii=False),
                ),
            )
            proposal_id = int(cursor.lastrowid)
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "base_revision": document["revision"],
            "changes": changes,
            "message": f"已生成 {len(changes)} 项待确认调整",
        }
    except ImportError as e:
        raise HTTPException(500, f"依赖缺失: {e}")
    except Exception as e:
        raise HTTPException(500, f"生成失败: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Activity AI Review — 活动 AI 复盘
# ═══════════════════════════════════════════════════════════════════════════════

def generate_activity_review(conn, activity_id: int) -> dict:
    """生成活动 AI 复盘。

    1. 获取活动数据
    2. 获取当日健康数据
    3. 获取当日计划训练（如有）
    4. 调用 LLM 生成结构化复盘
    5. 解析并存储结果
    """
    from engine import llm_client

    activity = database.get_activity(conn, activity_id)
    if not activity:
        raise HTTPException(404, f"活动 {activity_id} 不存在")

    activity_date = activity.get("date", "")
    sport = activity.get("sport", "running")
    if sport not in {"running", "training", "strength_training"}:
        raise HTTPException(422, "当前 AI 复盘仅支持跑步和力量训练")

    # 获取当日健康/体能数据
    wellness = database.get_wellness(conn, activity_date) if activity_date else None
    fitness_row = conn.execute(
        "SELECT * FROM fitness_history WHERE date = ?", (activity_date,)
    ).fetchone()
    fitness = dict(fitness_row) if fitness_row else None

    # 获取当日计划训练
    planned = conn.execute(
        "SELECT * FROM planned_workouts WHERE date = ? ORDER BY id", (activity_date,)
    ).fetchall()
    planned_workouts = [dict(p) for p in planned] if planned else []

    # 解析 JSON 字段
    hr_zones = json.loads(activity["hr_zones_json"]) if activity.get("hr_zones_json") else None
    laps = json.loads(activity["laps_json"]) if activity.get("laps_json") else None

    # 构建活动摘要（给 LLM 的上下文）
    sport_names = {"running": "跑步", "training": "力量训练", "strength_training": "力量训练"}
    sport_cn = sport_names.get(sport, sport)

    activity_summary = f"""活动名称: {activity.get('name', '未知')}
运动类型: {sport_cn}
日期: {activity_date}
距离: {round(activity['distance_m'] / 1000, 1) if activity.get('distance_m') else '无'}km
总时长: {round(activity['total_elapsed_s'] / 60) if activity.get('total_elapsed_s') else '无'}分钟
运动时间: {round(activity['total_timer_s'] / 60) if activity.get('total_timer_s') else '无'}分钟
平均心率: {activity.get('avg_hr') or '无'}bpm
最大心率: {activity.get('max_hr') or '无'}bpm
TSS: {round(activity['tss']) if activity.get('tss') else '无'}
爬升: {round(activity['total_ascent']) if activity.get('total_ascent') else '无'}m
平均步频: {activity.get('avg_cadence') or '无'}spm
平均配速: {round((1000 / activity['avg_speed']) / 60, 2) if activity.get('avg_speed') else '无'}min/km
有氧训练效果: {activity.get('aerobic_te') or '无'}
无氧训练效果: {activity.get('anaerobic_te') or '无'}
心率漂移: {f"{round(activity['drift_pct'], 1)}% ({activity.get('drift_classification', '')})" if activity.get('drift_pct') is not None else '无'}
TRIMP: {round(activity['trimp']) if activity.get('trimp') else '无'}
碳水消耗: {round(activity['carbs_used_g']) if activity.get('carbs_used_g') else '无'}g
热量: {activity.get('total_calories') or '无'}kcal"""

    # 体能状态
    fitness_context = ""
    if fitness:
        fitness_context = f"""
当日体能状态:
  CTL(长期负荷): {fitness.get('ctl') or '无'}
  ATL(短期负荷): {fitness.get('atl') or '无'}
  TSB(体能余量): {fitness.get('tsb') or '无'}
  Ramp Rate: {fitness.get('ramp_rate') or '无'}"""

    # 健康数据
    wellness_context = ""
    if wellness:
        wellness_context = f"""
当日健康数据:
  静息心率: {wellness.get('resting_hr') or '无'}bpm
  HRV: {wellness.get('hrv') or '无'}ms
  睡眠时长: {wellness.get('sleep_hours') or '无'}小时
  睡眠评分: {wellness.get('sleep_score') or '无'}
  体重: {wellness.get('weight_kg') or '无'}kg"""

    # 计划训练
    plan_context = ""
    if planned_workouts:
        plan_items = []
        for pw in planned_workouts:
            rpe_min = pw.get("target_rpe_min") if pw.get("target_rpe_min") is not None else pw.get("target_rpe")
            rpe_max = pw.get("target_rpe_max") if pw.get("target_rpe_max") is not None else pw.get("target_rpe")
            plan_items.append(
                f"  - {pw.get('title', '未命名')}: {pw.get('sport', '')}, "
                f"目标时长 {pw.get('target_duration_min') or '无'}分钟, "
                f"目标TSS {pw.get('target_tss') or '无'}, "
                f"配速 {pw.get('target_pace_text') or pw.get('target_intensity') or '无'}, "
                f"心率 {pw.get('target_hr_text') or '无'}, "
                f"RPE {rpe_min if rpe_min is not None else '无'}-{rpe_max if rpe_max is not None else '无'}, "
                f"安全截断 {pw.get('safety_cutoff') or '无'}"
            )
        plan_context = f"\n当日计划训练:\n" + "\n".join(plan_items)

    zones_context = ""
    if hr_zones:
        zone_lines = [f"  {z.get('zone', '')}: {z.get('pct', 0):.0f}%" for z in hr_zones]
        zones_context = "\n心率区间分布:\n" + "\n".join(zone_lines)

    # LLM 提示词
    system_prompt = """你是一位专业马拉松跑步教练，精通 Tinman CV、Canova 专项耐力和 Mantz 带疲劳目标配速训练。

你的分析必须基于数据，客观、简洁、具有训练指导价值。

免责声明：本分析仅用于训练管理参考，不构成医学诊断或医疗建议。如有健康疑虑，请咨询专业医生。

请严格按照以下 JSON 格式输出，不要添加任何额外说明文字：

{
  "summary": {
    "overall_label": "一个2-6字的跑步评价标签",
    "one_line_summary": "一句话总结本次跑步的核心特征和训练价值",
    "completion_status": "完成度评价：完美执行/基本完成/部分完成/未完成",
    "fatigue_impact": "对疲劳的影响评价：低/中/高/极高",
    "plan_impact": "对后续训练计划的影响：无影响/轻微调整/需要调整/需要重新规划"
  },
  "key_findings": [
    "第一个关键发现（最重要的训练信号）",
    "第二个关键发现",
    "第三个关键发现"
  ],
  "narrative": {
    "training_type": "识别本次是恢复跑、轻松跑、CV/间歇、M-Pace、长距离或比赛，并说明依据",
    "execution_quality": "逐组或分段评估配速、心率、恢复和对应步频，不用全活动平均步频代替分段判断",
    "physiological_cost": "分析生理成本：TSS负荷、心率漂移、碳水消耗、恢复需求",
    "capacity_signal": "分析 CV、M-Pace、长距离后段和心率漂移所反映的能力信号",
    "abnormal_and_noise": "指出异常数据或干扰因素（如天气、设备问题、路况等）",
    "next_steps": "基于本次训练结果，对后续1-2天训练的具体建议"
  },
  "confidence": {
    "level": "高/中/低",
    "reasons": ["影响置信度的因素1", "影响置信度的因素2"]
  }
}"""

    user_prompt = f"""请对以下跑步活动进行结构化复盘分析：

{activity_summary}
{fitness_context}
{wellness_context}
{plan_context}
{zones_context}

请严格按照 JSON 格式输出分析结果。"""

    # 调用 LLM
    response_text = llm_client.chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=3000,
        temperature=0.5,
    )

    # 解析 LLM 响应
    review_data = llm_client.extract_json(response_text)

    # 补充元数据
    review_data["analysis_version"] = "running_review_v1"
    review_data["generated_at"] = datetime.now().isoformat()
    review_data["review_status"] = "completed"
    review_data["sport_type"] = sport

    # 存储到数据库
    database.upsert_ai_review(conn, activity_id, review_data)

    return review_data


@app.get("/api/activities/{activity_id}/ai-review")
def api_get_ai_review(activity_id: int):
    """获取活动的 AI 复盘结果，如不存在则自动生成。"""
    with database.get_db() as conn:
        review = database.get_ai_review(conn, activity_id)
        if review:
            return {"ok": True, "review": review, "source": "cached"}

        # 自动生成
        try:
            review_data = generate_activity_review(conn, activity_id)
            review = database.get_ai_review(conn, activity_id)
            return {"ok": True, "review": review, "source": "generated"}
        except Exception as e:
            logger.exception("AI 复盘生成失败: activity_id=%s", activity_id)
            raise HTTPException(500, f"AI 复盘生成失败: {e}")


@app.post("/api/activities/{activity_id}/ai-review/regenerate")
def api_regenerate_ai_review(activity_id: int):
    """重新生成活动的 AI 复盘。"""
    try:
        with database.get_db() as conn:
            review_data = generate_activity_review(conn, activity_id)
            return {"ok": True, "review": review_data, "message": "AI 复盘已重新生成"}
    except Exception as e:
        logger.exception("AI 复盘重新生成失败: activity_id=%s", activity_id)
        raise HTTPException(500, f"AI 复盘重新生成失败: {e}")


@app.get("/api/activities/{activity_id}/ai-review/summary")
def api_get_ai_review_summary(activity_id: int):
    """获取活动 AI 复盘的摘要部分（适合 Telegram 等简短场景）。"""
    with database.get_db() as conn:
        review = database.get_ai_review(conn, activity_id)
        if not review:
            raise HTTPException(404, "暂无 AI 复盘，请先生成")

        summary = review.get("summary", {})
        key_findings = review.get("key_findings", [])
        narrative = review.get("narrative", {})

        return {
            "ok": True,
            "activity_id": activity_id,
            "overall_label": summary.get("overall_label", ""),
            "one_line_summary": summary.get("one_line_summary", ""),
            "key_findings": key_findings,
            "next_steps": narrative.get("next_steps", ""),
            "fatigue_impact": summary.get("fatigue_impact", ""),
        }


@app.get("/body-data", response_class=HTMLResponse)
def body_data_page(request: Request):
    """Body composition data page — v1 Decision Cockpit."""
    with database.get_db() as conn:
        latest = database.get_latest_body_comp(conn)
        history = database.list_body_comp(conn, days=730)
        # Get the previous record for trend comparison
        previous = None
        if len(history) >= 2:
            previous = history[1]

        # Get latest Garmin-sourced record (has HRV, sleep, body battery)
        garmin_records = database.list_body_comp(conn, days=90, source="Garmin")
        latest_garmin = garmin_records[0] if garmin_records else None

        # Get wellness data for HRV/sleep trend charts (separate from body_comp)
        wellness_history = database.list_wellness(conn, days=90)

        # v1: 结论层数据
        body_trend = compute_body_trend_summary(conn)
        body_comparisons = get_body_comp_comparisons(conn)
        metric_cards = get_metric_comparisons(conn)

    return templates.TemplateResponse(request=request, name="body_data.html", context={
        "request": request,
        "latest": latest,
        "previous": previous,
        "history": history,
        "latest_garmin": latest_garmin,
        "wellness_history": wellness_history,
        # v1: Decision Cockpit
        "body_trend": body_trend.to_dict(),
        "body_comparisons": body_comparisons,
        "metric_cards": metric_cards,
    })


@app.post("/api/body-composition", dependencies=[Depends(verify_api_key)])
async def api_upsert_body_comp(request: Request):
    """Upsert a body composition record."""
    data = await request.json()
    with database.get_db() as conn:
        database.upsert_body_comp(conn, data)
    return {"ok": True}


@app.get("/api/body-composition", dependencies=[Depends(verify_api_key)])
def api_list_body_comp(
    days: int = Query(90, ge=1, le=730),
    source: Optional[str] = None,
):
    """List body composition history."""
    with database.get_db() as conn:
        records = database.list_body_comp(conn, days=days, source=source)
    return {"ok": True, "count": len(records), "records": records}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    """Settings page — API config + athlete profile."""
    setting_keys = [
        "llm_api_key", "llm_api_base", "llm_proxy", "llm_model", "llm_vision_model",
        "athlete_max_hr", "athlete_resting_hr",
        "athlete_goal", "athlete_focus", "athlete_weekly_hours_available",
        "athlete_event_name", "athlete_event_date",
        "athlete_constraints", "athlete_constraints_text",
        "garmin_token_path", "navigation_order",
    ]
    settings = {}
    with database.get_db() as conn:
        for key in setting_keys:
            val = database.get_setting(conn, key)
            if val:
                # Mask API key for display (only show last 4 chars)
                if key == "llm_api_key" and len(val) > 8:
                    settings[key] = "sk-" + "*" * 20 + val[-4:]
                    settings["llm_api_key_set"] = True
                else:
                    settings[key] = val
                    
    # Check if Intervals API key is configured
    from engine import intervals
    has_intervals_env = intervals.is_configured()
    
    return templates.TemplateResponse(request=request, name="settings.html", context={
        "request": request,
        "settings": settings,
        "has_intervals_env": has_intervals_env,
    })


@app.post("/api/settings")
async def api_save_settings(request: Request):
    """Save settings to database."""
    data = await request.json()
    if "navigation_order" in data:
        try:
            ordered_items = _ordered_navigation_items(data["navigation_order"], strict=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        data["navigation_order"] = json.dumps(
            [item["key"] for item in ordered_items], ensure_ascii=False
        )
    with database.get_db() as conn:
        for key, value in data.items():
            if value is not None and str(value).strip():
                # Don't overwrite API key if masked (mask format: sk-****...8975)
                if key == "llm_api_key" and "*" in str(value):
                    continue
                database.set_setting(conn, key, str(value))
    return {"ok": True}


@app.post("/api/test-llm")
async def api_test_llm():
    """Test LLM API connection."""
    try:
        from engine import llm_client
        text = llm_client.chat_completion(
            messages=[{"role": "user", "content": "Say 'TrainingEdge connected!' in one line."}],
            max_tokens=50,
            temperature=0,
        )
        return {"ok": True, "response": text.strip(), "model": llm_client.get_model()}
    except Exception as e:
        raise HTTPException(500, f"LLM 连接失败: {e}")


@app.post("/api/sync-garmin")
async def api_sync_garmin(request: Request):
    """Sync data from Garmin Connect."""
    data = await request.json()
    sync_type = data.get("type", "activities")

    try:
        from engine import sync as garmin_sync

        if sync_type == "wellness":
            result = garmin_sync.sync_garmin_wellness(days=14)
            msg = (
                f"已同步 {result['days_synced']} 天数据: "
                f"HRV {result['hrv_count']} 条, "
                f"睡眠 {result['sleep_count']} 条"
            )
            if result["errors"]:
                msg += f" (错误: {len(result['errors'])})"
            return {"ok": True, "message": msg, "detail": result}
        elif sync_type == "activities":
            results = garmin_sync.sync_recent(days=7)
            # Auto-match to planned workouts
            with database.get_db() as conn:
                matched = database.match_compliance(conn)
            return {
                "ok": True,
                "message": f"已同步 {len(results)} 个活动" + (f"，匹配 {matched} 个计划" if matched else ""),
                "count": len(results),
            }
        elif sync_type == "all":
            # 先同步活动
            act_results = garmin_sync.sync_recent(days=7)
            with database.get_db() as conn:
                matched = database.match_compliance(conn)
            
            # 再同步健康数据
            well_result = garmin_sync.sync_garmin_wellness(days=14)
            
            msg = (
                f"同步全部完成。活动: {len(act_results)}个" + (f"(匹配{matched})" if matched else "") +
                f"，健康: {well_result['days_synced']}天。"
            )
            return {
                "ok": True, 
                "message": msg,
                "detail": {
                    "activities": len(act_results),
                    "wellness": well_result
                }
            }
        else:
             raise HTTPException(400, "未知的同步类型")
    except Exception as e:
        raise HTTPException(500, f"Garmin 同步失败: {e}")


@app.post("/api/sync-intervals")
async def api_sync_intervals(request: Request):
    """Sync data from Intervals.icu."""
    data = await request.json()
    sync_type = data.get("type", "activities")

    try:
        from engine import intervals
        if not intervals.is_configured():
            return {"ok": False, "detail": "未配置 Intervals.icu API Key，请在 .env 文件中设置 INTERVALS_API_KEY"}

        if sync_type == "wellness":
            # 1. Sync wellness (last 14 days)
            end_date = date.today()
            start_date = end_date - timedelta(days=14)
            wellness_data = intervals.fetch_wellness_range(start_date.isoformat(), end_date.isoformat())

            synced_wellness = 0
            with database.get_db() as conn:
                for w in wellness_data:
                    database.upsert_wellness(conn, w)
                    synced_wellness += 1

            # 2. Sync today's fitness to settings (CTL, ATL)
            seed_result = intervals.auto_seed()

            msg = f"已同步 {synced_wellness} 天健康数据。"
            if seed_result.get("ctl"):
                msg += f" 当前 CTL: {seed_result['ctl']}."
            return {"ok": True, "message": msg}

        else:
            # Sync activities and validate
            val_result = intervals.auto_validate(days=14)
            validated = val_result.get('validated', 0)
            passed = val_result.get('passed', 0)
            
            # 检查是否有重复或者异常的细节可以在 detail 中展示
            msg = f"已与 Intervals.icu 校验最近14天活动。共比对 {validated} 个活动，完全一致 {passed} 个。"
            return {"ok": True, "message": msg, "detail": val_result}

    except Exception as e:
        logger.exception("Intervals.icu 同步失败")
        raise HTTPException(500, f"Intervals 同步失败: {e}")


@app.post("/api/inbody-ocr")
async def api_inbody_ocr(files: List[UploadFile] = File(...)):
    """Upload InBody photos and extract data using Claude Vision.

    Accepts 1-4 images. Returns extracted data and saves to database.
    No API key required (web form upload).
    """
    if not files:
        raise HTTPException(400, "请上传至少一张 InBody 照片")
    if len(files) > 4:
        raise HTTPException(400, "最多上传 4 张照片")

    # Read all images
    images = []
    for f in files:
        content = await f.read()
        if len(content) > 10 * 1024 * 1024:  # 10MB limit per image
            raise HTTPException(400, f"图片 {f.filename} 过大（最大 10MB）")
        images.append(content)

    try:
        from engine.inbody_ocr import extract_inbody_data
        data = extract_inbody_data(images)
    except ImportError as e:
        raise HTTPException(500, f"依赖缺失: {e}")
    except ValueError as e:
        raise HTTPException(422, f"识别失败: {e}")
    except Exception as e:
        raise HTTPException(500, f"识别出错: {e}")

    # Save to database
    with database.get_db() as conn:
        database.upsert_body_comp(conn, data)

    return {"ok": True, "data": data, "message": f"已识别并保存 {data.get('date', '未知日期')} 的 InBody 数据"}
