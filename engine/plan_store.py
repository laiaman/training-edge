"""Versioned structured training-plan storage.

The YAML file is canonical. SQLite remains the operational read model used by
the web UI and Garmin compliance matching. All writes pass through this module
so browser edits and agent edits share conflict detection and history locking.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
DEFAULT_PLAN_PATH = WORKSPACE_ROOT / "vault" / "plans" / "training_plan.yaml"
DEFAULT_GOAL_MIRROR_PATH = WORKSPACE_ROOT / "vault" / "goals" / "current_goal.md"
DEFAULT_PLAN_MIRROR_PATH = WORKSPACE_ROOT / "vault" / "plans" / "2026_Marathon_Plan.md"
logger = logging.getLogger(__name__)

PRESCRIPTION_BACKFILL_FIELDS = {
    "target_pace_text", "target_hr_min", "target_hr_max", "target_hr_text",
    "target_rpe", "target_rpe_min", "target_rpe_max", "target_duration_source",
    "target_tss_source", "steps", "coach_note", "safety_cutoff", "source_reference",
}


class PlanStoreError(RuntimeError):
    """Base error returned to API callers without leaking implementation detail."""


class PlanConflictError(PlanStoreError):
    """The caller edited an older revision than the current canonical file."""


class CompletedWorkoutLockedError(PlanStoreError):
    """A completed workout was changed or removed."""


def resolve_plan_path() -> Path:
    raw = os.environ.get("TRAININGEDGE_PLAN_PATH")
    if not raw:
        return DEFAULT_PLAN_PATH
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _payload_checksum(document: Dict[str, Any]) -> str:
    payload = copy.deepcopy(document)
    payload.setdefault("metadata", {}).pop("checksum", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_document(document: Dict[str, Any]) -> None:
    if not isinstance(document, dict):
        raise PlanStoreError("计划文档必须是对象")
    if document.get("schema_version") != 1:
        raise PlanStoreError("不支持的计划 schema_version")
    if not isinstance(document.get("revision"), int) or document["revision"] < 1:
        raise PlanStoreError("计划 revision 必须是正整数")
    goal = document.get("goal")
    if not isinstance(goal, dict) or not goal.get("primary_race_date"):
        raise PlanStoreError("计划缺少主赛日期")
    try:
        date.fromisoformat(str(goal["primary_race_date"]))
    except ValueError as exc:
        raise PlanStoreError("主赛日期格式必须为 YYYY-MM-DD") from exc

    seen_uids: set[str] = set()
    for workout in document.get("workouts", []):
        if not isinstance(workout, dict):
            raise PlanStoreError("训练项必须是对象")
        uid = str(workout.get("uid") or "").strip()
        if not uid or uid in seen_uids:
            raise PlanStoreError(f"训练 uid 缺失或重复: {uid or '空'}")
        seen_uids.add(uid)
        try:
            date.fromisoformat(str(workout.get("date")))
        except (TypeError, ValueError) as exc:
            raise PlanStoreError(f"训练 {uid} 日期无效") from exc
        if workout.get("sport") not in {"running", "training", "rest", "stretch"}:
            raise PlanStoreError(f"训练 {uid} 运动类型不允许")
        distance = workout.get("target_distance_km")
        try:
            if distance is not None and float(distance) < 0:
                raise PlanStoreError(f"训练 {uid} 距离不能为负数")
        except (TypeError, ValueError) as exc:
            raise PlanStoreError(f"训练 {uid} 距离必须是数字") from exc
        rpe_min, rpe_max = workout.get("target_rpe_min"), workout.get("target_rpe_max")
        try:
            if rpe_min is not None and rpe_max is not None and float(rpe_min) > float(rpe_max):
                raise PlanStoreError(f"训练 {uid} 的RPE下限不能高于上限")
            if rpe_min is not None and not 0 <= float(rpe_min) <= 10:
                raise PlanStoreError(f"训练 {uid} 的RPE下限必须在0-10")
            if rpe_max is not None and not 0 <= float(rpe_max) <= 10:
                raise PlanStoreError(f"训练 {uid} 的RPE上限必须在0-10")
        except (TypeError, ValueError) as exc:
            raise PlanStoreError(f"训练 {uid} 的RPE必须是数字") from exc
        if workout.get("steps") is not None and not isinstance(workout["steps"], list):
            raise PlanStoreError(f"训练 {uid} 的分段处方必须是数组")


def load_plan(path: Optional[Path] = None) -> Dict[str, Any]:
    plan_path = path or resolve_plan_path()
    if not plan_path.exists():
        raise PlanStoreError(f"结构化计划不存在: {plan_path}")
    try:
        document = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PlanStoreError(f"无法读取结构化计划: {exc}") from exc
    _validate_document(document)
    expected = document.get("metadata", {}).get("checksum")
    if expected and expected != _payload_checksum(document):
        raise PlanConflictError("计划文件校验值不匹配，已暂停同步")
    return document


def _completed_rows(conn) -> Dict[int, Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM planned_workouts WHERE compliance_status='completed'"
    ).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def _assert_completed_unchanged(
    conn, old: Dict[str, Any], new: Dict[str, Any], *, allow_prescription_backfill: bool = False
) -> None:
    locked_rows = _completed_rows(conn)
    if not locked_rows:
        return
    old_by_uid = {str(w["uid"]): w for w in old.get("workouts", [])}
    new_by_uid = {str(w["uid"]): w for w in new.get("workouts", [])}
    immutable_fields = (
        "date", "sport", "title", "description", "target_distance_km",
        "target_duration_min", "target_tss", "target_intensity", "target_hr_min",
        "target_hr_max", "target_rpe", "week_label", "phase", "workout_type",
        "is_key_workout", "safety_cutoff", "target_pace_text", "target_hr_text",
        "target_rpe_min", "target_rpe_max", "target_duration_source", "target_tss_source",
        "steps", "coach_note", "source_reference",
    )
    for workout_id, row in locked_rows.items():
        uid = row.get("source_uid")
        if not uid:
            continue
        before = old_by_uid.get(str(uid))
        after = new_by_uid.get(str(uid))
        if before is None:
            continue
        if after is None:
            raise CompletedWorkoutLockedError(f"已完成训练 {workout_id} 不能删除")
        changed = [field for field in immutable_fields if before.get(field) != after.get(field)]
        if allow_prescription_backfill and changed and all(
            field in PRESCRIPTION_BACKFILL_FIELDS
            and before.get(field) in (None, "", [])
            and after.get(field) not in (None, "", [])
            for field in changed
        ):
            continue
        if changed:
            raise CompletedWorkoutLockedError(f"已完成训练 {workout_id} 不能修改")


def _atomic_dump(document: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=120)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _replace_managed_block(path: Path, block_name: str, content: str) -> None:
    start = f"<!-- trainingedge:{block_name}:start -->"
    end = f"<!-- trainingedge:{block_name}:end -->"
    block = f"{start}\n{content.rstrip()}\n{end}"
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if start in original and end in original:
        prefix, remainder = original.split(start, 1)
        _, suffix = remainder.split(end, 1)
        updated = f"{prefix}{block}{suffix}"
    else:
        updated = f"{block}\n\n{original.lstrip()}"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def sync_markdown_mirrors(document: Dict[str, Any]) -> None:
    """Refresh compact generated headers while preserving detailed legacy notes."""
    goal_path = Path(os.environ.get("TRAININGEDGE_GOAL_MIRROR_PATH", DEFAULT_GOAL_MIRROR_PATH))
    plan_path = Path(os.environ.get("TRAININGEDGE_PLAN_MIRROR_PATH", DEFAULT_PLAN_MIRROR_PATH))
    goal = document["goal"]
    goal_content = (
        "> 由 TrainingEdge 结构化计划自动生成；请勿直接编辑本区块。\n\n"
        f"- Revision: `{document['revision']}`\n"
        f"- 主目标: **{goal.get('primary_race')}**（{goal.get('primary_race_date')}）\n"
        f"- A/B 目标: **{goal.get('a_target_time')} / {goal.get('b_target_time')}**\n"
        f"- M-Pace: **{goal.get('marathon_pace')}/km**\n"
        f"- 备选赛事: {goal.get('secondary_race')}（{goal.get('secondary_race_date')}）"
    )
    if goal.get("secondary_target_a"):
        goal_content += (
            f"\n- 次目标 A/B/C: **{goal.get('secondary_target_a')} / "
            f"{goal.get('secondary_target_b')} / {goal.get('secondary_target_c')}**"
        )
    if goal.get("cancelled_race"):
        goal_content += f"\n- 已取消: {goal.get('cancelled_race')}（{goal.get('cancelled_race_date')}）"
    weekly: Dict[str, Dict[str, Any]] = {}
    for workout in document.get("workouts", []):
        label = workout.get("week_label") or "未分周"
        bucket = weekly.setdefault(label, {"phase": workout.get("phase", ""), "km": 0.0, "count": 0})
        if workout.get("sport") == "running":
            bucket["km"] += float(workout.get("target_distance_km") or 0)
            bucket["count"] += 1
    lines = [
        "> 结构化主数据位于 `vault/plans/training_plan.yaml`；本区块自动生成，请勿直接编辑。",
        "",
        f"Revision `{document['revision']}` · 更新于 `{document.get('metadata', {}).get('updated_at', '')}`",
        "",
        "| 周 | 阶段 | 跑量 | 跑步课次 |",
        "|---|---|---:|---:|",
    ]
    lines.extend(
        f"| {label} | {values['phase']} | {values['km']:.1f} km | {values['count']} |"
        for label, values in weekly.items()
    )
    _replace_managed_block(goal_path, "goal", goal_content)
    _replace_managed_block(plan_path, "plan", "\n".join(lines))


def save_plan(
    conn,
    document: Dict[str, Any],
    *,
    expected_revision: int,
    source: str,
    path: Optional[Path] = None,
    allow_prescription_backfill: bool = False,
) -> Dict[str, Any]:
    """Validate, lock completed history, save atomically, then refresh SQLite."""
    plan_path = path or resolve_plan_path()
    current = load_plan(plan_path)
    if int(current["revision"]) != int(expected_revision):
        raise PlanConflictError(
            f"计划已从 revision {expected_revision} 更新到 {current['revision']}，已暂停同步"
        )

    candidate = copy.deepcopy(document)
    candidate["schema_version"] = 1
    candidate["revision"] = current["revision"] + 1
    metadata = candidate.setdefault("metadata", {})
    metadata["updated_at"] = _utc_now()
    metadata["updated_by"] = source
    metadata["checksum"] = ""
    _validate_document(candidate)
    _assert_completed_unchanged(
        conn, current, candidate, allow_prescription_backfill=allow_prescription_backfill
    )
    metadata["checksum"] = _payload_checksum(candidate)
    _atomic_dump(candidate, plan_path)
    sync_plan_to_db(conn, candidate)
    try:
        sync_markdown_mirrors(candidate)
    except OSError as exc:
        logger.warning("Markdown mirror refresh failed: %s", exc)
    return candidate


def sync_plan_to_db(conn, document: Dict[str, Any]) -> int:
    """Project canonical workouts into SQLite without rewriting completed rows."""
    count = 0
    canonical_uids = {str(workout["uid"]) for workout in document.get("workouts", [])}
    projected_rows = conn.execute(
        "SELECT id, source_uid, compliance_status FROM planned_workouts WHERE source_uid IS NOT NULL"
    ).fetchall()
    for row in projected_rows:
        if row["source_uid"] not in canonical_uids and row["compliance_status"] != "completed":
            conn.execute("DELETE FROM planned_workouts WHERE id=?", (row["id"],))
    for workout in document.get("workouts", []):
        payload = {
            "id": workout.get("db_id"),
            "date": workout["date"],
            "sport": workout["sport"],
            "title": workout.get("title"),
            "description": workout.get("description"),
            "target_distance_km": workout.get("target_distance_km"),
            "target_duration_min": workout.get("target_duration_min"),
            "target_tss": workout.get("target_tss"),
            "target_intensity": workout.get("target_intensity"),
            "target_pace_text": workout.get("target_pace_text"),
            "target_hr_min": workout.get("target_hr_min"),
            "target_hr_max": workout.get("target_hr_max"),
            "target_hr_text": workout.get("target_hr_text"),
            "target_rpe": workout.get("target_rpe"),
            "target_rpe_min": workout.get("target_rpe_min"),
            "target_rpe_max": workout.get("target_rpe_max"),
            "target_duration_source": workout.get("target_duration_source"),
            "target_tss_source": workout.get("target_tss_source"),
            "workout_steps_json": json.dumps(workout.get("steps"), ensure_ascii=False) if workout.get("steps") else None,
            "coach_note": workout.get("coach_note"),
            "source_reference": workout.get("source_reference"),
            "week_label": workout.get("week_label"),
            "phase": workout.get("phase"),
            "workout_type": workout.get("workout_type"),
            "is_key_workout": 1 if workout.get("is_key_workout") else 0,
            "safety_cutoff": workout.get("safety_cutoff"),
            "source_uid": workout["uid"],
            "plan_revision": document["revision"],
        }
        workout_id = _upsert_projected_workout(conn, payload)
        workout["db_id"] = workout_id
        count += 1
    return count


def _upsert_projected_workout(conn, payload: Dict[str, Any]) -> int:
    columns = [
        "date", "sport", "title", "description", "target_distance_km",
        "target_duration_min", "target_tss", "target_intensity", "target_pace_text",
        "target_hr_min", "target_hr_max", "target_hr_text", "target_rpe",
        "target_rpe_min", "target_rpe_max", "target_duration_source", "target_tss_source",
        "workout_steps_json", "coach_note", "source_reference", "week_label", "phase",
        "workout_type", "is_key_workout", "safety_cutoff", "source_uid", "plan_revision",
    ]
    existing = None
    if payload.get("id"):
        existing = conn.execute(
            "SELECT id, compliance_status FROM planned_workouts WHERE id=?", (payload["id"],)
        ).fetchone()
    if not existing:
        existing = conn.execute(
            "SELECT id, compliance_status FROM planned_workouts WHERE source_uid=?", (payload["source_uid"],)
        ).fetchone()
    if not existing:
        existing = conn.execute(
            """SELECT id, compliance_status FROM planned_workouts
               WHERE date=? AND sport=? AND title=? ORDER BY id LIMIT 1""",
            (payload["date"], payload["sport"], payload.get("title")),
        ).fetchone()
    values = [payload.get(column) for column in columns]
    if existing:
        if existing["compliance_status"] == "completed":
            # Completed execution is immutable, but canonical plan metadata and
            # fields introduced by later migrations must still be backfilled.
            conn.execute(
                """UPDATE planned_workouts SET
                   target_distance_km=coalesce(target_distance_km, ?),
                   target_duration_min=coalesce(target_duration_min, ?),
                   target_tss=coalesce(target_tss, ?),
                   target_intensity=coalesce(target_intensity, ?),
                   target_pace_text=coalesce(target_pace_text, ?),
                   target_hr_min=coalesce(target_hr_min, ?),
                   target_hr_max=coalesce(target_hr_max, ?),
                   target_hr_text=coalesce(target_hr_text, ?),
                   target_rpe=coalesce(target_rpe, ?),
                   target_rpe_min=coalesce(target_rpe_min, ?),
                   target_rpe_max=coalesce(target_rpe_max, ?),
                   target_duration_source=coalesce(target_duration_source, ?),
                   target_tss_source=coalesce(target_tss_source, ?),
                   workout_steps_json=coalesce(workout_steps_json, ?),
                   coach_note=coalesce(coach_note, ?),
                   source_reference=coalesce(source_reference, ?),
                   week_label=?, phase=?, workout_type=?, is_key_workout=?,
                   safety_cutoff=coalesce(safety_cutoff, ?),
                   source_uid=coalesce(source_uid, ?), plan_revision=?
                   WHERE id=?""",
                (
                    payload.get("target_distance_km"), payload.get("target_duration_min"),
                    payload.get("target_tss"), payload.get("target_intensity"),
                    payload.get("target_pace_text"), payload.get("target_hr_min"),
                    payload.get("target_hr_max"), payload.get("target_hr_text"),
                    payload.get("target_rpe"), payload.get("target_rpe_min"),
                    payload.get("target_rpe_max"), payload.get("target_duration_source"),
                    payload.get("target_tss_source"), payload.get("workout_steps_json"),
                    payload.get("coach_note"), payload.get("source_reference"), payload.get("week_label"),
                    payload.get("phase"), payload.get("workout_type"),
                    payload.get("is_key_workout"), payload.get("safety_cutoff"),
                    payload.get("source_uid"), payload.get("plan_revision"), existing["id"],
                ),
            )
            return int(existing["id"])
        assignments = ",".join(f"{column}=?" for column in columns)
        conn.execute(
            f"UPDATE planned_workouts SET {assignments} WHERE id=?",
            (*values, existing["id"]),
        )
        return int(existing["id"])
    placeholders = ",".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO planned_workouts ({','.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return int(cursor.lastrowid)


def workouts_by_uid(document: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(workout["uid"]): workout for workout in document.get("workouts", [])}


def apply_workout_changes(
    document: Dict[str, Any], changes: Iterable[Dict[str, Any]]
) -> Dict[str, Any]:
    """Apply selected structured patches in memory; persistence happens separately."""
    candidate = copy.deepcopy(document)
    by_uid = workouts_by_uid(candidate)
    allowed = {
        "date", "sport", "title", "description", "target_distance_km", "target_duration_min",
        "target_tss", "target_intensity", "target_pace_text", "target_hr_min",
        "target_hr_max", "target_hr_text", "target_rpe", "target_rpe_min",
        "target_rpe_max", "target_duration_source", "target_tss_source", "steps",
        "coach_note", "source_reference", "safety_cutoff", "is_key_workout", "workout_type",
        "week_label", "phase",
    }
    for change in changes:
        uid = str(change.get("uid") or "")
        workout = by_uid.get(uid)
        if workout is None:
            raise PlanStoreError(f"找不到训练: {uid}")
        patch = change.get("patch")
        if not isinstance(patch, dict) or not patch:
            raise PlanStoreError(f"训练 {uid} 的变更为空")
        unknown = set(patch) - allowed
        if unknown:
            raise PlanStoreError(f"训练 {uid} 包含不允许的字段: {', '.join(sorted(unknown))}")
        workout.update(patch)
    _validate_document(candidate)
    return candidate


def apply_plan_changes(document: Dict[str, Any], changes: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Apply confirmed goal, zone, phase, or workout changes in memory."""
    candidate = copy.deepcopy(document)
    for change in changes:
        kind = change.get("kind", "workout")
        if kind == "workout":
            candidate = apply_workout_changes(candidate, [change])
        elif kind == "workout_batch":
            candidate = apply_workout_changes(candidate, change.get("changes", []))
        elif kind == "goal":
            candidate.setdefault("goal", {}).update(change.get("patch", {}))
        elif kind == "plan":
            patch = change.get("patch")
            if not isinstance(patch, dict) or not patch:
                raise PlanStoreError("计划元数据变更为空")
            candidate.setdefault("plan", {}).update(copy.deepcopy(patch))
        elif kind == "decision_gate":
            value = change.get("value")
            if not isinstance(value, dict):
                raise PlanStoreError("决策门禁必须是对象")
            candidate["decision_gate"] = copy.deepcopy(value)
        elif kind == "alternate_scenarios":
            value = change.get("value")
            if not isinstance(value, dict):
                raise PlanStoreError("备选场景必须是对象")
            candidate["alternate_scenarios"] = copy.deepcopy(value)
        elif kind == "support_training":
            value = change.get("value")
            if not isinstance(value, dict):
                raise PlanStoreError("辅助训练配置必须是对象")
            candidate["support_training"] = copy.deepcopy(value)
        elif kind == "cross_training_rules":
            value = change.get("value")
            if not isinstance(value, dict):
                raise PlanStoreError("交叉训练规则必须是对象")
            candidate["cross_training_rules"] = copy.deepcopy(value)
        elif kind == "workouts_replace_from_date":
            from_date = str(change.get("from_date") or "")
            try:
                date.fromisoformat(from_date)
            except ValueError as exc:
                raise PlanStoreError("未来课表替换起始日必须为 YYYY-MM-DD") from exc
            workouts = change.get("workouts")
            if not isinstance(workouts, list):
                raise PlanStoreError("未来课表替换内容必须是数组")
            if any(str(workout.get("date") or "") < from_date for workout in workouts):
                raise PlanStoreError("未来课表替换不得写入起始日前训练")
            retained = [
                workout for workout in candidate.get("workouts", [])
                if str(workout.get("date") or "") < from_date
            ]
            candidate["workouts"] = retained + copy.deepcopy(workouts)
        elif kind == "future_workouts_replace":
            effective_date_text = str(change.get("effective_date") or "")
            try:
                effective_date = date.fromisoformat(effective_date_text)
            except ValueError as exc:
                raise PlanStoreError("未来课表替换生效日必须为 YYYY-MM-DD") from exc
            workouts = change.get("workouts")
            if not isinstance(workouts, list):
                raise PlanStoreError("未来课表替换内容必须是数组")
            for workout in workouts:
                if not isinstance(workout, dict):
                    raise PlanStoreError("未来课表替换内容必须是训练对象")
                try:
                    workout_date = date.fromisoformat(str(workout.get("date") or ""))
                except ValueError as exc:
                    raise PlanStoreError("未来课表替换训练日期必须为 YYYY-MM-DD") from exc
                if workout_date < effective_date:
                    raise PlanStoreError("未来课表替换不得写入生效日前训练")
            retained = []
            for workout in candidate.get("workouts", []):
                try:
                    workout_date = date.fromisoformat(str(workout.get("date") or ""))
                except ValueError as exc:
                    raise PlanStoreError("计划训练日期必须为 YYYY-MM-DD") from exc
                if workout_date < effective_date:
                    retained.append(workout)
            candidate["workouts"] = retained + copy.deepcopy(workouts)
        elif kind == "pace_zones":
            candidate["pace_zones"] = copy.deepcopy(change.get("value", {}))
        elif kind == "phase_partition":
            plan_patch = change.get("plan_patch", {})
            candidate.setdefault("plan", {}).update(plan_patch)
            phase_by_uid = change.get("phase_by_uid", {})
            for workout in candidate.get("workouts", []):
                if workout.get("uid") in phase_by_uid:
                    workout["phase"] = phase_by_uid[workout["uid"]]
        else:
            raise PlanStoreError(f"不支持的计划变更类型: {kind}")
    _validate_document(candidate)
    return candidate
