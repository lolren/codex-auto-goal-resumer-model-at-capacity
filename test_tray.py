#!/usr/bin/python3
"""Tray menu and Qt-to-D-Bus tests; run inside dbus-run-session."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import dbus
from PyQt5.QtWidgets import QApplication

import goal_resumer as gr
import tray


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.data = {"connected": True, "paused": False, "loadedThreads": 7,
                     "goals": [{"nextRetryAt": None},
                               {"threadId": "thread_test", "goalStatus": "blocked",
                                "attempts": 1, "reason": "model at capacity",
                                "nextRetryAt": time.time() + 30}]}

    def call(self, method, args, callback):
        self.calls.append((method, args))
        if method == "Status":
            callback(json.dumps(self.data), None)
        elif method == "Pause":
            self.data["paused"] = True
            callback("paused", None)
        elif method == "StartAll":
            self.data["paused"] = False
            callback("started", None)
        else:
            callback("ok", None)


class TrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_menu_start_pause_and_visible_status(self):
        transport = FakeTransport()
        monitor = tray.MonitorTray(self.app, transport, show_icon=False)
        try:
            self.assertEqual(monitor.summary.text(), "Monitoring all CLI sessions")
            self.assertIn("7 loaded threads", monitor.counts.text())
            self.assertIn("1 waiting", monitor.counts.text())
            self.assertIn("next in", monitor.counts.text())
            self.assertFalse(monitor.tray.icon().isNull())
            monitor.pause_action.trigger()
            self.assertTrue(transport.data["paused"])
            self.assertEqual(monitor.summary.text(), "Automatic retries paused")
            monitor.start_action.trigger()
            self.assertFalse(transport.data["paused"])
            self.assertIn(("StartAll", []), transport.calls)
            self.assertEqual(monitor.summary.text(), "Monitoring all CLI sessions")
        finally:
            monitor.timer.stop()
            monitor.tray.hide()
            monitor.menu.close()

    def test_retry_countdown_ignores_inflight_or_non_capacity_goals(self):
        now = time.time()
        goals = [
            {"goalStatus": "blocked", "reason": "model at capacity", "nextRetryAt": now + 75},
            {"goalStatus": "active", "reason": "retry submitted", "nextRetryAt": now + 90},
            {"goalStatus": "blocked", "reason": "blocked for another reason", "nextRetryAt": now + 30},
        ]
        self.assertEqual(len(tray.pending_retries(goals)), 1)
        self.assertEqual(tray.describe_delay(75), "1m 15s")

    def test_disconnected_state_is_clear(self):
        transport = FakeTransport()
        monitor = tray.MonitorTray(self.app, transport, show_icon=False)
        try:
            transport.data["connected"] = False
            monitor.refresh()
            self.assertIn("Waiting for the Codex", monitor.summary.text())
            self.assertTrue(monitor.start_action.isEnabled())
            monitor._status_result(None, "service unavailable")
            self.assertEqual(monitor.summary.text(), "Goal monitor unavailable")
            self.assertFalse(monitor.start_action.isEnabled())
        finally:
            monitor.timer.stop()
            monitor.tray.hide()
            monitor.menu.close()

    def test_qt_dbus_transport_with_user_service(self):
        with tempfile.TemporaryDirectory(prefix="goal-resumer-tray-") as d:
            p = subprocess.Popen([sys.executable, str(Path(gr.__file__).resolve()), "serve",
                                  "--socket", d + "/missing.sock", "--state-dir", d + "/state",
                                  "--interval", "0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                bus = dbus.SessionBus()
                for _ in range(100):
                    if bus.name_has_owner(gr.BUS_NAME):
                        break
                    if p.poll() is not None:
                        self.fail("service exited")
                    time.sleep(0.03)
                self.assertTrue(bus.name_has_owner(gr.BUS_NAME))
                transport = tray.DbusTransport()

                def call(method):
                    result = []
                    transport.call(method, [], lambda value, error: result.append((value, error)))
                    deadline = time.monotonic() + 4
                    while not result and time.monotonic() < deadline:
                        self.app.processEvents()
                        time.sleep(0.01)
                    self.assertTrue(result, "Qt D-Bus request timed out")
                    self.assertIsNone(result[0][1])
                    return result[0][0]

                self.assertFalse(json.loads(str(call("Status")))["paused"])
                self.assertIn("paused", str(call("Pause")))
                self.assertIn("all sessions", str(call("StartAll")))
                self.assertFalse(json.loads(str(call("Status")))["paused"])
            finally:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
