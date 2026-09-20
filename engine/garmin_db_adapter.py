"""Adapter: read data from Hermes garmin.db → upsert into training_edge.db.

Uses ATTACH DATABASE to read garmin.db as a read-only source, then writes
into TrainingEdge's own tables via the existing database module CRUD functions.

Typical usage:
    from engine.garmin_db_adapter import sync_from_garmin_db
    count = sync_from_garmin_db("../garmin.db", days=14)
"""

from __future__ import annotations

from collections import defaultdict
import json
import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import database

log = logging.getLogger(__name__)

# garmin.db field → TrainingEdge wellness field
_WELLNESS_QUERY = """
SELECT
    g_hr.calendar_date                                           AS date,
    g_hr.resting_hr                                              AS resting_hr,
    COALESCE(
        g_hrv.last_night,
        json_extract(g_hrv.raw, '$.hrvSummary.lastNightAvg')
    )                                                            AS hrv,
    ROUND(g_sleep.duration_seconds / 3600.0, 2)                  AS sleep_hours,
    g_sleep.sleep_score                                          AS sleep_score,
    g_tr.score                                                   AS readiness,
    g_ds.total_steps                                             AS steps
FROM garmin.heart_rate        AS g_hr
LEFT JOIN garmin.hrv          AS g_hrv   ON g_hr.calendar_date = g_hrv.calendar_date
LEFT JOIN garmin.sleep        AS g_sleep ON g_hr.calendar_date = g_sleep.calendar_date
LEFT JOIN garmin.training_readiness AS g_tr ON g_hr.calendar_date = g_tr.calendar_date
LEFT JOIN garmin.daily_summary      AS g_ds ON g_hr.calendar_date = g_ds.calendar_date
WHERE g_hr.calendar_date >= ?
ORDER BY g_hr.calendar_date
"""

_BODY_COMP_QUERY = """
SELECT
    g_hr.calendar_date                                           AS date,
    g_hr.resting_hr                                              AS resting_hr,
    COALESCE(
        g_hrv.last_night,
        json_extract(g_hrv.raw, '$.hrvSummary.lastNightAvg')
    )                                                            AS hrv_ms,
    ROUND(g_sleep.duration_seconds / 60.0, 1)                    AS sleep_duration_min,
    CASE WHEN g_sleep.duration_seconds > 0
         THEN ROUND(g_sleep.deep_seconds * 100.0 / g_sleep.duration_seconds, 1)
         ELSE NULL END                                           AS deep_sleep_pct,
    g_bb.highest                                                 AS body_battery
FROM garmin.heart_rate        AS g_hr
LEFT JOIN garmin.hrv          AS g_hrv   ON g_hr.calendar_date = g_hrv.calendar_date
LEFT JOIN garmin.sleep        AS g_sleep ON g_hr.calendar_date = g_sleep.calendar_date
LEFT JOIN garmin.body_battery AS g_bb    ON g_hr.calendar_date = g_bb.calendar_date
WHERE g_hr.calendar_date >= ?
ORDER BY g_hr.calendar_date
"""


_ACTIVITIES_QUERY = """
SELECT
    a.activity_id           AS activity_id,
    a.activity_type         AS activity_type,
    a.start_time_local      AS start_time_local,
    a.calendar_date         AS calendar_date,
    a.duration_seconds      AS duration_seconds,
    a.distance_meters       AS distance_meters,
    a.avg_hr                AS avg_hr,
    a.max_hr                AS max_hr,
    a.avg_speed             AS avg_speed,
    a.avg_cadence           AS avg_cadence,
    a.elevation_gain        AS elevation_gain,
    a.training_load         AS training_load,
    a.aerobic_effect        AS aerobic_effect,
    a.anaerobic_effect      AS anaerobic_effect,
    a.vo2max                AS vo2max,
    a.calories              AS calories,
    a.activity_name         AS activity_name,
    a.raw                   AS raw_json,
    w.temperature           AS temperature_f,
    w.humidity              AS humidity,
    w.wind_speed            AS wind_speed_mph,
    w.wind_direction        AS wind_direction,
    w.condition             AS weather_condition,
    json_extract(w.raw, '$.apparentTemp') AS feels_like_f,
    w.raw                   AS weather_raw_json
FROM garmin.activity AS a
LEFT JOIN garmin.activity_weather AS w ON a.activity_id = w.activity_id
WHERE a.calendar_date >= ?
ORDER BY a.start_time_local
"""

_SPLITS_QUERY = """
SELECT
    s.activity_id  AS activity_id,
    s.split_number AS split_number,
    s.distance     AS distance_m,
    s.duration     AS duration_s,
    s.avg_speed    AS avg_speed,
    s.avg_hr       AS avg_hr,
    s.max_hr       AS max_hr,
    s.avg_cadence  AS avg_cadence,
    s.elevation_gain AS elevation_gain,
    s.elevation_loss AS elevation_loss,
    s.raw          AS raw_json
FROM garmin.activity_splits AS s
JOIN garmin.activity AS a ON a.activity_id = s.activity_id
WHERE a.calendar_date >= ?
ORDER BY s.activity_id, s.split_number
"""

_HR_ZONES_QUERY = """
SELECT
    z.activity_id       AS activity_id,
    z.zone_number       AS zone_number,
    z.seconds_in_zone   AS seconds_in_zone
FROM garmin.activity_hr_zones AS z
JOIN garmin.activity AS a ON a.activity_id = z.activity_id
WHERE a.calendar_date >= ?
ORDER BY z.activity_id, z.zone_number
"""

_ACTIVITY_NOTES_QUERY = """
SELECT
    n.activity_id        AS activity_id,
    n.calendar_date      AS calendar_date,
    n.subjective_notes   AS subjective_notes,
    n.pushed_to_garmin   AS pushed_to_garmin,
    n.created_at         AS created_at
FROM garmin.activity_notes AS n
WHERE n.calendar_date >= ?
ORDER BY n.calendar_date, n.activity_id
"""

_TRAINING_STATUS_QUERY = """
SELECT
    calendar_date AS calendar_date,
    raw           AS raw_json
FROM garmin.training_status
WHERE calendar_date >= ?
ORDER BY calendar_date
"""


def _f_to_c(temp_f: Optional[float]) -> Optional[float]:
    if temp_f is None:
        return None
    try:
        return (float(temp_f) - 32.0) * 5.0 / 9.0
    except (ValueError, TypeError):
        return None


def _normalize_sport(activity_type: Optional[str]) -> Optional[str]:
    """Map Hermes activity_type into TrainingEdge's sport taxonomy when possible."""
    if not activity_type:
        return None
    t = str(activity_type).lower().strip()
    if t in ("cycling", "bike", "biking"):
        return "cycling"
    if t in ("running", "run", "trail_running", "treadmill_running"):
        return "running"
    if t in ("strength_training", "strength", "weight_training"):
        return "training"
    return t


def _resolve_since_date(
    conn: sqlite3.Connection,
    base_table: str,
    date_col: str,
    days: int,
) -> str:
    """Resolve the lower bound date string.

    If days <= 0, returns MIN(date_col) from the base_table; otherwise returns today-days.
    """
    if days > 0:
        return (date.today() - timedelta(days=days)).isoformat()
    try:
        row = conn.execute(
            f"SELECT MIN({date_col}) AS d FROM garmin.{base_table}"
        ).fetchone()
        if row and row["d"]:
            return str(row["d"])
    except Exception:
        pass
    return "1970-01-01"


def sync_fitness_from_garmin_db(garmin_db_path: str | Path, days: int = 90) -> int:
    """Sync CTL/ATL (Garmin training load model) from Hermes training_status.raw into fitness_history.

    Notes:
    - These CTL/ATL values are Garmin's training load system (not TSS-based).
    - We still store them into TrainingEdge's fitness_history so the dashboard/readiness has a signal
      even when FIT-derived TSS isn't available yet.
    """
    garmin_db_path = Path(garmin_db_path).resolve()
    if not garmin_db_path.exists():
        log.warning("garmin.db not found at %s — skipping fitness sync", garmin_db_path)
        return 0

    synced = 0
    with database.get_db() as conn:
        conn.execute("ATTACH DATABASE ? AS garmin", (str(garmin_db_path),))
        try:
            since = _resolve_since_date(conn, base_table="training_status", date_col="calendar_date", days=days)
            rows = conn.execute(_TRAINING_STATUS_QUERY, (since,)).fetchall()
            for r in rows:
                raw_txt = r["raw_json"]
                if not raw_txt or str(raw_txt).strip() in ("null", "NULL", ""):
                    continue
                try:
                    raw = json.loads(raw_txt)
                except Exception:
                    continue

                mrs = raw.get("mostRecentTrainingStatus")
                if not isinstance(mrs, dict):
                    continue
                latest = mrs.get("latestTrainingStatusData")
                if not isinstance(latest, dict) or not latest:
                    continue

                # deviceId is dynamic; take the first available device block
                _, info = next(iter(latest.items()))
                if not isinstance(info, dict):
                    continue
                acute = info.get("acuteTrainingLoadDTO") or {}
                if not isinstance(acute, dict) or not acute:
                    continue

                ctl = acute.get("dailyTrainingLoadChronic")
                atl = acute.get("dailyTrainingLoadAcute")
                if ctl is None and atl is None:
                    continue

                try:
                    ctl_f = float(ctl) if ctl is not None else None
                except Exception:
                    ctl_f = None
                try:
                    atl_f = float(atl) if atl is not None else None
                except Exception:
                    atl_f = None
                if ctl_f is None and atl_f is None:
                    continue

                tsb_f = (ctl_f - atl_f) if (ctl_f is not None and atl_f is not None) else None
                database.upsert_fitness(conn, {
                    "date": r["calendar_date"],
                    "ctl": ctl_f,
                    "atl": atl_f,
                    "tsb": tsb_f,
                    "sport": "garmin",
                })
                synced += 1
        finally:
            # Detach requires no open write transaction on the connection.
            try:
                conn.commit()
            except Exception:
                pass
            conn.execute("DETACH DATABASE garmin")

    log.info("Synced %d days of fitness (Garmin model) from garmin.db", synced)
    return synced


def sync_activities_from_garmin_db(
    garmin_db_path: str | Path,
    days: int = 3650,
    *,
    include_splits: bool = True,
    include_hr_zones: bool = True,
    include_notes: bool = True,
    store_raw: bool = True,
) -> Dict[str, Any]:
    """Import activity summaries from Hermes garmin.db into TrainingEdge activities table.

    This does NOT download FIT files. It mainly seeds the dashboard with historical activities
    and optionally imports splits/HR zones/notes + stores Hermes raw blobs in hermes_* tables.
    """
    garmin_db_path = Path(garmin_db_path).resolve()
    if not garmin_db_path.exists():
        log.warning("garmin.db not found at %s — skipping activities sync", garmin_db_path)
        return {"imported": 0, "since": None}

    summary: Dict[str, Any] = {
        "imported": 0,
        "updated": 0,
        "splits_imported": 0,
        "hr_zones_imported": 0,
        "notes_imported": 0,
        "since": None,
    }

    with database.get_db() as conn:
        conn.execute("ATTACH DATABASE ? AS garmin", (str(garmin_db_path),))
        try:
            since = _resolve_since_date(conn, base_table="activity", date_col="calendar_date", days=days)
            summary["since"] = since

            # Preload splits/hr zones/notes in one pass to avoid N+1 queries.
            splits_by_id: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
            if include_splits:
                try:
                    for r in conn.execute(_SPLITS_QUERY, (since,)).fetchall():
                        splits_by_id[int(r["activity_id"])].append(dict(r))
                except Exception as e:
                    log.warning("Split import skipped: %s", e)
                    include_splits = False

            hr_zones_by_id: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
            if include_hr_zones:
                try:
                    for r in conn.execute(_HR_ZONES_QUERY, (since,)).fetchall():
                        try:
                            hr_zones_by_id[int(r["activity_id"])].append(
                                (int(r["zone_number"]), int(r["seconds_in_zone"] or 0))
                            )
                        except Exception:
                            continue
                except Exception as e:
                    log.warning("HR zone import skipped: %s", e)
                    include_hr_zones = False

            notes_by_id: Dict[int, Dict[str, Any]] = {}
            if include_notes:
                try:
                    for r in conn.execute(_ACTIVITY_NOTES_QUERY, (since,)).fetchall():
                        aid = int(r["activity_id"])
                        notes_by_id[aid] = dict(r)
                except Exception as e:
                    log.warning("Activity notes import skipped: %s", e)
                    include_notes = False

            # Import activities
            act_rows = conn.execute(_ACTIVITIES_QUERY, (since,)).fetchall()
            for r in act_rows:
                activity_id = int(r["activity_id"])

                # Track whether this is a new insert
                existed = conn.execute(
                    "SELECT 1 FROM activities WHERE id = ? LIMIT 1", (activity_id,)
                ).fetchone() is not None

                sport = _normalize_sport(r["activity_type"])
                temp_c = _f_to_c(r["temperature_f"])

                act: Dict[str, Any] = {
                    "id": activity_id,
                    "sport": sport,
                    "name": r["activity_name"],
                    "start_time": r["start_time_local"],
                    "date": r["calendar_date"],
                    "total_elapsed_s": r["duration_seconds"],
                    "total_timer_s": r["duration_seconds"],
                    "distance_m": r["distance_meters"],
                    "avg_hr": r["avg_hr"],
                    "max_hr": r["max_hr"],
                    "avg_speed": r["avg_speed"],
                    "avg_cadence": r["avg_cadence"],
                    "total_ascent": r["elevation_gain"],
                    "total_calories": r["calories"],
                    "aerobic_te": r["aerobic_effect"],
                    "anaerobic_te": r["anaerobic_effect"],
                }
                if temp_c is not None:
                    act["avg_temperature"] = round(temp_c, 1)

                # Drop null-ish values to avoid overwriting existing enriched rows
                act = {k: v for k, v in act.items() if v is not None}

                # Splits → laps_json (snake_case keys expected by templates)
                if include_splits and splits_by_id.get(activity_id):
                    laps = []
                    for s in splits_by_id[activity_id]:
                        laps.append({
                            "total_elapsed_time": s.get("duration_s"),
                            "total_distance": s.get("distance_m"),
                            "avg_heart_rate": s.get("avg_hr"),
                            "avg_power": None,
                            "avg_speed": s.get("avg_speed"),
                            "avg_cadence": s.get("avg_cadence"),
                        })
                    act["laps_json"] = json.dumps(laps, ensure_ascii=False)
                    summary["splits_imported"] += 1

                # HR zones → hr_zones_json
                if include_hr_zones and hr_zones_by_id.get(activity_id):
                    zs = hr_zones_by_id[activity_id]
                    total_secs = sum(secs for _, secs in zs) or 0
                    zone_seconds = {f"z{n}": secs for n, secs in zs}
                    zones_out = []
                    for zn in ("z1", "z2", "z3", "z4", "z5"):
                        secs = int(zone_seconds.get(zn, 0) or 0)
                        pct = round(secs / total_secs * 100.0, 1) if total_secs > 0 else 0.0
                        zones_out.append({"zone": zn, "seconds": secs, "pct": pct})
                    act["hr_zones_json"] = json.dumps(zones_out, ensure_ascii=False)
                    summary["hr_zones_imported"] += 1

                # Upsert activity row
                database.upsert_activity(conn, act)
                if existed:
                    summary["updated"] += 1
                else:
                    summary["imported"] += 1

                # Store Hermes raw blobs / notes / weather (optional)
                if store_raw and r["raw_json"]:
                    conn.execute(
                        """INSERT INTO hermes_activity_raw
                           (activity_id, calendar_date, activity_type, start_time_local, activity_name, raw_json)
                           VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(activity_id) DO UPDATE SET
                             calendar_date=excluded.calendar_date,
                             activity_type=excluded.activity_type,
                             start_time_local=excluded.start_time_local,
                             activity_name=excluded.activity_name,
                             raw_json=excluded.raw_json,
                             imported_at=datetime('now')""",
                        (
                            activity_id,
                            r["calendar_date"],
                            r["activity_type"],
                            r["start_time_local"],
                            r["activity_name"],
                            r["raw_json"],
                        ),
                    )

                if store_raw and (r["weather_raw_json"] or r["temperature_f"] is not None):
                    conn.execute(
                        """INSERT INTO hermes_activity_weather
                           (activity_id, temperature_f, humidity, wind_speed_mph, wind_direction, condition, feels_like_f, raw_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(activity_id) DO UPDATE SET
                             temperature_f=excluded.temperature_f,
                             humidity=excluded.humidity,
                             wind_speed_mph=excluded.wind_speed_mph,
                             wind_direction=excluded.wind_direction,
                             condition=excluded.condition,
                             feels_like_f=excluded.feels_like_f,
                             raw_json=excluded.raw_json,
                             imported_at=datetime('now')""",
                        (
                            activity_id,
                            r["temperature_f"],
                            r["humidity"],
                            r["wind_speed_mph"],
                            r["wind_direction"],
                            r["weather_condition"],
                            r["feels_like_f"],
                            r["weather_raw_json"],
                        ),
                    )

                if include_notes and notes_by_id.get(activity_id):
                    n = notes_by_id[activity_id]
                    conn.execute(
                        """INSERT INTO hermes_activity_notes
                           (activity_id, calendar_date, subjective_notes, pushed_to_garmin, created_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(activity_id) DO UPDATE SET
                             calendar_date=excluded.calendar_date,
                             subjective_notes=excluded.subjective_notes,
                             pushed_to_garmin=excluded.pushed_to_garmin,
                             created_at=excluded.created_at,
                             imported_at=datetime('now')""",
                        (
                            activity_id,
                            n.get("calendar_date"),
                            n.get("subjective_notes"),
                            n.get("pushed_to_garmin"),
                            n.get("created_at"),
                        ),
                    )
                    summary["notes_imported"] += 1

        finally:
            # Detach requires no open write transaction on the connection.
            try:
                conn.commit()
            except Exception:
                pass
            conn.execute("DETACH DATABASE garmin")

    log.info(
        "Imported activities from garmin.db: imported=%d updated=%d since=%s",
        summary["imported"],
        summary["updated"],
        summary["since"],
    )
    return summary


def sync_from_garmin_db(garmin_db_path: str | Path, days: int = 14) -> int:
    """Read wellness data from garmin.db and upsert into training_edge.db.

    Args:
        garmin_db_path: Path to the Hermes-maintained garmin.db.
        days: How many days of history to sync.

    Returns:
        Number of days successfully synced.
    """
    garmin_db_path = Path(garmin_db_path).resolve()
    if not garmin_db_path.exists():
        log.warning("garmin.db not found at %s — skipping adapter sync", garmin_db_path)
        return 0

    # days <= 0 means "all history"
    since = "1970-01-01"
    synced = 0

    with database.get_db() as conn:
        conn.execute("ATTACH DATABASE ? AS garmin", (str(garmin_db_path),))
        try:
            since = _resolve_since_date(conn, base_table="heart_rate", date_col="calendar_date", days=days)

            # --- Wellness ---
            rows = conn.execute(_WELLNESS_QUERY, (since,)).fetchall()
            for row in rows:
                wellness: Dict[str, Any] = {"date": row["date"]}
                if row["resting_hr"]:
                    wellness["resting_hr"] = row["resting_hr"]
                if row["hrv"]:
                    wellness["hrv"] = int(round(float(row["hrv"])))
                if row["sleep_hours"]:
                    wellness["sleep_hours"] = row["sleep_hours"]
                if row["sleep_score"]:
                    wellness["sleep_score"] = row["sleep_score"]
                if row["readiness"]:
                    wellness["readiness"] = row["readiness"]
                if row["steps"]:
                    wellness["steps"] = row["steps"]
                if len(wellness) > 1:
                    database.upsert_wellness(conn, wellness)

            # --- Body composition (source="Garmin") ---
            rows = conn.execute(_BODY_COMP_QUERY, (since,)).fetchall()
            for row in rows:
                body: Dict[str, Any] = {"date": row["date"], "source": "Garmin"}
                if row["resting_hr"]:
                    body["resting_hr"] = row["resting_hr"]
                if row["hrv_ms"]:
                    body["hrv_ms"] = int(round(float(row["hrv_ms"])))
                if row["sleep_duration_min"]:
                    body["sleep_duration_min"] = row["sleep_duration_min"]
                if row["deep_sleep_pct"]:
                    body["deep_sleep_pct"] = row["deep_sleep_pct"]
                if row["body_battery"]:
                    body["body_battery"] = row["body_battery"]
                if len(body) > 2:
                    database.upsert_body_comp(conn, body)

                synced += 1

        finally:
            # Detach requires no open write transaction on the connection.
            try:
                conn.commit()
            except Exception:
                pass
            conn.execute("DETACH DATABASE garmin")

    log.info("Synced %d days of wellness data from garmin.db", synced)
    return synced
