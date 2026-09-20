#!/usr/bin/env python3
"""Apply an agent-authored structured plan patch with revision protection."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import database, plan_store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show-revision", action="store_true")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--changes", type=Path, help="JSON array using the plan proposal change schema")
    parser.add_argument("--source", default="agent")
    args = parser.parse_args()
    document = plan_store.load_plan()
    if args.show_revision:
        print(json.dumps({"revision": document["revision"], "updated_at": document.get("metadata", {}).get("updated_at")}, ensure_ascii=False))
        return
    if args.expected_revision is None or args.changes is None:
        parser.error("更新时必须同时提供 --expected-revision 和 --changes")
    changes = json.loads(args.changes.read_text(encoding="utf-8"))
    if not isinstance(changes, list):
        raise SystemExit("changes 文件必须包含 JSON 数组")
    candidate = plan_store.apply_plan_changes(document, changes)
    database.init_db()
    with database.get_db() as conn:
        saved = plan_store.save_plan(
            conn, candidate, expected_revision=args.expected_revision, source=args.source
        )
    print(json.dumps({"ok": True, "revision": saved["revision"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
