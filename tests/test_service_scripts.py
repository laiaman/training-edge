from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parent
INSTALL = PROJECT / "scripts" / "install_service.sh"
BACKUP = PROJECT / "scripts" / "backup_plan_to_onedrive.sh"
ALLOWLIST = (
    Path("vault/plans/training_plan.yaml"),
    Path("vault/goals/current_goal.md"),
    Path("vault/plans/2026_Marathon_Plan.md"),
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ServiceScriptTests(unittest.TestCase):
    def test_installer_rejects_onedrive_checkout_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / "Library/CloudStorage/OneDrive-Test/project/training-edge/scripts"
            scripts.mkdir(parents=True)
            copied = scripts / INSTALL.name
            shutil.copy2(INSTALL, copied)
            result = subprocess.run(
                ["bash", str(copied), "--preflight"],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("拒绝从云盘副本安装", result.stderr)

    def test_plan_backup_dry_run_has_exact_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["bash", str(BACKUP), "--dry-run", "--target", tmp],
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertIn("仅白名单 3/3", result.stdout)
            for relative in ALLOWLIST:
                self.assertIn(str(relative), result.stdout)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_plan_backup_copies_only_allowlist_and_verifies_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            result = subprocess.run(
                ["bash", str(BACKUP), "--target", str(target)],
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertIn("白名单备份完成（3/3）", result.stdout)
            copied = {
                path.relative_to(target)
                for path in target.rglob("*")
                if path.is_file()
            }
            self.assertEqual(copied, set(ALLOWLIST))
            for relative in ALLOWLIST:
                self.assertEqual(digest(WORKSPACE / relative), digest(target / relative))
            forbidden = (".db", ".db-wal", ".db-shm", ".fit", ".log", ".env")
            self.assertFalse(any(path.name.endswith(forbidden) for path in target.rglob("*") if path.is_file()))


if __name__ == "__main__":
    unittest.main()
