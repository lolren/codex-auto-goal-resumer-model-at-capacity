#!/usr/bin/python3
"""Installer tests use temporary homes and never start the user's services."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import install


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="goal-resumer-install-")
        self.home = Path(self.tmp.name) / "test home % $"
        self.script = Path(install.__file__).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def run_installer(self, *options):
        return subprocess.run([sys.executable, str(self.script), "--home", str(self.home),
                               "--no-start", *options], text=True, capture_output=True)

    def test_isolated_install_and_existing_files_are_preserved(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        app = self.home / ".local/share/codex-goal-resumer"
        unit = self.home / ".config/systemd/user/codex-goal-resumer.service"
        tray_unit = self.home / ".config/systemd/user/codex-goal-resumer-tray.service"
        bus = self.home / ".local/share/dbus-1/services/org.local.CodexGoalResumer.service"
        command = self.home / ".local/bin/codex-goal-resumer"
        self.assertTrue((app / "goal_resumer.py").is_file())
        self.assertTrue((app / "tray.py").is_file())
        self.assertTrue(tray_unit.is_file())
        self.assertEqual(command.resolve(), app / "goal_resumer.py")
        self.assertIn("test home %% $$", unit.read_text())
        self.assertIn("SystemdService=codex-goal-resumer.service", bus.read_text())
        original = unit.read_bytes()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("preserved", result.stderr)
        self.assertEqual(unit.read_bytes(), original)

    def test_conflict_aborts_before_installing_any_files(self):
        unit = self.home / ".config/systemd/user/codex-goal-resumer.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("existing user's service")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(unit.read_text(), "existing user's service")
        self.assertFalse((self.home / ".local/share/codex-goal-resumer/goal_resumer.py").exists())

    def test_headless_install_omits_tray(self):
        result = self.run_installer("--no-tray")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".config/systemd/user/codex-goal-resumer-tray.service").exists())
        self.assertTrue((self.home / ".local/share/codex-goal-resumer/goal_resumer.py").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
