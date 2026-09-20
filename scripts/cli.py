#!/usr/bin/env python3
"""TrainingEdge CLI — sync, analyze, validate, serve."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import database, sync, validator, intervals, garmin_db_adapter


def cmd_init(args):
    """Initialize database and auto-seed from Intervals.icu."""
    database.init_db()

    # Manual overrides (if provided)
    with database.get_db() as conn:
        if args.max_hr:
            database.set_setting(conn, "max_hr", str(args.max_hr))

    # Auto-seed from Intervals.icu
    if intervals.is_configured():
        print("Found Intervals.icu API key — auto-seeding CTL/ATL/FTP...")
        try:
            seed = intervals.auto_seed()
            print(f"  CTL: {seed.get('ctl', '—')}")
            print(f"  ATL: {seed.get('atl', '—')}")
            print(f"  TSB: {round(seed['ctl'] - seed['atl'], 1) if seed.get('ctl') and seed.get('atl') else '—'}")
            print(f"  FTP: {seed.get('ftp', '—')} W (from Intervals eFTP)")
            if seed.get('resting_hr'):
                print(f"  Resting HR: {seed.get('resting_hr')} bpm")
            if seed.get('weight_kg'):
                print(f"  Weight: {seed.get('weight_kg')} kg")
            print("  Done. All values seeded automatically.")
        except Exception as e:
            print(f"  Warning: auto-seed failed: {e}")
            print("  Falling back to defaults. You can re-run init after fixing the API key.")
    else:
        print("Intervals.icu API key not found.")
        print("  Run: garmin_coach.sh intervals-login")
        print("  Then re-run: python scripts/cli.py init")
        print("  (Using defaults: CTL=0, ATL=0, FTP=200)")

    from engine.auth import get_or_create_api_key
    api_key = get_or_create_api_key()
    print(f"\n  API Key: {api_key}")
    print(f"  Use: curl -H 'X-API-Key: {api_key}' http://localhost:8420/api/summary")

    print("\nDatabase initialized.")


def cmd_sync(args):
    """Sync recent activities from Garmin."""
    database.init_db()

    with database.get_db() as conn:
        ftp = float(database.get_setting(conn, "ftp") or "0") or None
        max_hr = int(float(database.get_setting(conn, "max_hr") or "190"))
        resting_hr = int(float(database.get_setting(conn, "resting_hr") or "50"))

    if args.ftp:
        ftp = args.ftp

    print(f"Syncing last {args.days} days (FTP={ftp}, MaxHR={max_hr}, RestHR={resting_hr})...")
    results = sync.sync_recent(
        days=args.days,
        activity_type=args.type,
        ftp=ftp,
        max_hr=max_hr,
        resting_hr=resting_hr,
        limit=args.limit,
    )
    print(f"\nSynced {len(results)} activities.")

    # Auto-validate against Intervals.icu (校验期)
    if intervals.is_configured() and results:
        print("\nAuto-validating against Intervals.icu...")
        try:
            val = intervals.auto_validate(days=args.days)
            print(f"  Validated: {val['validated']} | Passed: {val['passed']} | Rate: {val['pass_rate']}%")
            for d in val.get("details", []):
                status = "✅" if d["passed"] else "❌"
                print(f"    {status} {d['date']} {d['name']} — {d['summary']}")
        except Exception as e:
            print(f"  Validation skipped: {e}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


def cmd_sync_wellness(args):
    """Sync wellness data (HRV, sleep, body battery, etc.)."""
    database.init_db()
    print(f"Syncing wellness last {args.days} days...")
    try:
        result = sync.sync_garmin_wellness(days=args.days)
    except Exception as e:
        print(f"  ✗ Wellness sync failed: {e}")
        print("  Tips:")
        print("    - If you have a local garmin.db (Hermes-maintained), set GARMIN_DB_PATH to read without API calls.")
        print("      Example: GARMIN_DB_PATH=../garmin.db python scripts/cli.py sync-wellness --days 14")
        print("    - Otherwise set GARMIN_EMAIL/GARMIN_PASSWORD or provide valid tokens via GARMINTOKENS.")
        raise SystemExit(1)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    source = result.get("source", "garmin")
    days_synced = result.get("days_synced")
    hrv_count = result.get("hrv_count")
    sleep_count = result.get("sleep_count")
    errors = result.get("errors") or []

    print(f"  Source: {source}")
    if days_synced is not None:
        print(f"  Days synced: {days_synced}")
    if hrv_count is not None:
        print(f"  HRV records: {hrv_count}")
    if sleep_count is not None:
        print(f"  Sleep records: {sleep_count}")
    if errors:
        print(f"  Errors: {len(errors)}")


def cmd_sync_hermes(args):
    """Sync all available Hermes garmin.db data into TrainingEdge (no Garmin API calls)."""
    database.init_db()

    db_path = args.db or os.environ.get("GARMIN_DB_PATH") or "../garmin.db"
    p = Path(db_path).expanduser()
    if not p.is_absolute():
        p = (Path(__file__).resolve().parents[1] / p).resolve()
    else:
        p = p.resolve()

    days = 0 if args.all else args.days
    days_label = "ALL" if days <= 0 else str(days)
    print(f"Syncing Hermes garmin.db → TrainingEdge (db={p}, days={days_label})...")

    wellness_days = garmin_db_adapter.sync_from_garmin_db(p, days=days)
    fitness_days = garmin_db_adapter.sync_fitness_from_garmin_db(p, days=days)
    activities = garmin_db_adapter.sync_activities_from_garmin_db(
        p,
        days=days,
        include_splits=not args.no_splits,
        include_hr_zones=not args.no_hr_zones,
        include_notes=not args.no_notes,
        store_raw=not args.no_raw,
    )

    result = {
        "db": str(p),
        "days": days_label,
        "wellness_days_synced": wellness_days,
        "fitness_days_synced": fitness_days,
        "activities": activities,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    print("\nDone.")
    print(f"  Wellness days synced: {wellness_days}")
    print(f"  Fitness days synced:  {fitness_days}")
    print(f"  Activities imported:  {activities.get('imported', 0)} (updated {activities.get('updated', 0)})")
    if activities.get("splits_imported"):
        print(f"  With splits/laps:     {activities.get('splits_imported')}")
    if activities.get("hr_zones_imported"):
        print(f"  With HR zones:        {activities.get('hr_zones_imported')}")
    if activities.get("notes_imported"):
        print(f"  With notes:           {activities.get('notes_imported')}")


def cmd_activities(args):
    """List activities from local database."""
    database.init_db()
    with database.get_db() as conn:
        activities = database.list_activities(conn, sport=args.sport, days=args.days, limit=args.limit)

    if args.json:
        print(json.dumps(activities, ensure_ascii=False, indent=2, default=str))
    else:
        for act in activities:
            dist = f"{act['distance_m']/1000:.1f}km" if act.get('distance_m') else '—'
            dur = f"{act['total_timer_s']/60:.0f}min" if act.get('total_timer_s') else '—'
            np = f"NP={act['normalized_power']:.0f}W" if act.get('normalized_power') else ''
            tss = f"TSS={act['tss']:.0f}" if act.get('tss') else ''
            print(f"  {act['date']} | {act['name']:<30} | {dist:>8} | {dur:>6} | {np:>10} | {tss:>7}")


def cmd_fitness(args):
    """Show fitness history."""
    database.init_db()
    with database.get_db() as conn:
        history = database.list_fitness_history(conn, days=args.days)

    if args.json:
        print(json.dumps(history, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"{'Date':<12} {'CTL':>6} {'ATL':>6} {'TSB':>6} {'Ramp':>6} {'TSS':>6}")
        print("-" * 50)
        for h in history:
            print(f"{h['date']:<12} {h['ctl'] or 0:>6.1f} {h['atl'] or 0:>6.1f} {h['tsb'] or 0:>6.1f} {h['ramp_rate'] or 0:>6.2f} {h['daily_tss'] or 0:>6.1f}")


def cmd_validate(args):
    """Show validation dashboard."""
    database.init_db()
    dashboard = validator.validation_dashboard(args.days)

    if args.json:
        print(json.dumps(dashboard, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Validation Dashboard ({args.days} days)")
        print(f"  Total activities: {dashboard['total_activities']}")
        print(f"  Validated:        {dashboard['total_validated']}")
        print(f"  Passed:           {dashboard['total_passed']}")
        print(f"  Pass rate:        {dashboard['pass_rate']}%")
        print(f"  Graduation ready: {'YES' if dashboard['graduation_ready'] else 'NO'}")
        print()

        for act in dashboard['activities']:
            val = act.get('validation')
            if val:
                status = '✅' if val.get('all_passed') else '❌'
                summary = val.get('summary', '')
                print(f"  {status} {act['date']} | {act['name']:<30} | {summary}")
            else:
                print(f"  ⬜ {act['date']} | {act['name']:<30} | not validated")


def cmd_serve(args):
    """Start the web server."""
    import uvicorn
    database.init_db()
    print(f"Starting TrainingEdge on http://0.0.0.0:{args.port}")

    reload_kwargs = {}
    if args.reload:
        project_root = str(Path(__file__).resolve().parents[1])
        reload_kwargs = {
            "reload": True,
            "reload_dirs": [
                str(Path(project_root) / "api"),
                str(Path(project_root) / "engine"),
                str(Path(project_root) / "web"),
            ],
            "reload_includes": ["*.py", "*.html"],
            "reload_excludes": [".*", "__pycache__", "*.pyc", "state/*", "scripts/*"],
        }

    uvicorn.run(
        "api.app:app",
        host="0.0.0.0",
        port=args.port,
        **reload_kwargs,
    )


def main():
    parser = argparse.ArgumentParser(prog="training_edge", description="TrainingEdge CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # init — auto-seeds from Intervals.icu, only need manual max-hr override
    p_init = sub.add_parser("init", help="Initialize database (auto-seeds from Intervals.icu)")
    p_init.add_argument("--max-hr", type=int, help="Max heart rate (if not in Intervals)")
    p_init.set_defaults(func=cmd_init)

    # sync
    p_sync = sub.add_parser("sync", help="Sync recent activities from Garmin")
    p_sync.add_argument("--days", type=int, default=7)
    p_sync.add_argument("--type", default="all")
    p_sync.add_argument("--limit", type=int, default=20)
    p_sync.add_argument("--ftp", type=float)
    p_sync.add_argument("--json", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    # sync-wellness
    p_sw = sub.add_parser("sync-wellness", help="Sync wellness (HRV/sleep/body battery) from garmin.db or Garmin API")
    p_sw.add_argument("--days", type=int, default=14)
    p_sw.add_argument("--json", action="store_true")
    p_sw.set_defaults(func=cmd_sync_wellness)

    # sync-hermes (full import from Hermes garmin.db)
    p_sh = sub.add_parser("sync-hermes", help="Sync ALL Hermes garmin.db data into TrainingEdge (no Garmin API calls)")
    p_sh.add_argument("--db", help="Path to Hermes garmin.db (default: $GARMIN_DB_PATH or ../garmin.db)")
    p_sh.add_argument("--days", type=int, default=3650, help="How many days to import (ignored when --all)")
    p_sh.add_argument("--all", action="store_true", help="Import full history (MIN(date) → today)")
    p_sh.add_argument("--no-splits", action="store_true", help="Skip importing activity splits/laps")
    p_sh.add_argument("--no-hr-zones", action="store_true", help="Skip importing HR zone distribution")
    p_sh.add_argument("--no-notes", action="store_true", help="Skip importing activity subjective notes")
    p_sh.add_argument("--no-raw", action="store_true", help="Do not store Hermes raw JSON blobs into hermes_* tables")
    p_sh.add_argument("--json", action="store_true")
    p_sh.set_defaults(func=cmd_sync_hermes)

    # activities
    p_act = sub.add_parser("activities", help="List activities from local DB")
    p_act.add_argument("--sport", help="Filter by sport type")
    p_act.add_argument("--days", type=int, default=30)
    p_act.add_argument("--limit", type=int, default=20)
    p_act.add_argument("--json", action="store_true")
    p_act.set_defaults(func=cmd_activities)

    # fitness
    p_fit = sub.add_parser("fitness", help="Show CTL/ATL/TSB history")
    p_fit.add_argument("--days", type=int, default=90)
    p_fit.add_argument("--json", action="store_true")
    p_fit.set_defaults(func=cmd_fitness)

    # validate
    p_val = sub.add_parser("validate", help="Validation dashboard")
    p_val.add_argument("--days", type=int, default=30)
    p_val.add_argument("--json", action="store_true")
    p_val.set_defaults(func=cmd_validate)

    # serve
    p_serve = sub.add_parser("serve", help="Start web dashboard")
    p_serve.add_argument("--port", type=int, default=8420)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
