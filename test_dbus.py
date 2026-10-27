#!/usr/bin/python3
"""Run under dbus-run-session to exercise the real user-bus interface."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import dbus
import websockets

import goal_resumer as gr
from test_support import find_test_codex


class DbusTests(unittest.TestCase):
    def test_console_status_pause_resume_and_explicit_recover(self):
        with tempfile.TemporaryDirectory(prefix="goal-resumer-dbus-") as d:
            script = str(Path(gr.__file__).resolve())
            p = subprocess.Popen([sys.executable, script, "serve", "--socket", d + "/missing.sock",
                                  "--state-dir", d + "/state", "--interval", "0.1"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                bus = dbus.SessionBus()
                for _ in range(100):
                    if bus.name_has_owner(gr.BUS_NAME):
                        break
                    if p.poll() is not None:
                        self.fail("D-Bus service exited during startup")
                    time.sleep(0.05)
                self.assertTrue(bus.name_has_owner(gr.BUS_NAME))
                cli = lambda *a: subprocess.run([sys.executable, script, *a], text=True,
                                                  capture_output=True, timeout=5, check=True).stdout
                self.assertFalse(json.loads(cli("status"))["paused"])
                self.assertIn("paused", cli("pause"))
                self.assertTrue(json.loads(cli("status"))["paused"])
                self.assertIn("checked", cli("recover", "thread_test"))
                self.assertIn("enabled", cli("resume"))
                self.assertFalse(json.loads(cli("status"))["paused"])
                cli("pause")
                self.assertIn("all sessions", cli("start-all"))
                self.assertFalse(json.loads(cli("status"))["paused"])
                via_busctl = subprocess.run(["busctl", "--user", "call", gr.BUS_NAME, gr.BUS_PATH,
                                             gr.BUS_INTERFACE, "Status"], capture_output=True, text=True,
                                             timeout=5, check=True)
                self.assertIn("paused", via_busctl.stdout)
                saved = json.loads(Path(d, "state", "state.json").read_text())
                self.assertGreater(saved["goals"]["thread_test"]["manual_until"], time.time())
            finally:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()

    def test_dbus_service_recovers_real_local_appserver_fixture(self):
        asyncio.run(self._local_appserver_fixture())

    async def _local_appserver_fixture(self):
        codex = find_test_codex()
        if codex is None:
            self.skipTest("Codex CLI >= 0.164.0 not found; set CODEX_TEST_BIN")
        started = threading.Event()
        release = threading.Event()
        models = []

        class Overloaded(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length", "0")))
                models.append(json.loads(raw).get("model"))
                if len(models) == 1:
                    started.set()
                    release.wait(5)
                data = json.dumps({"error": {"code": "server_overloaded",
                                  "message": "Selected model is at capacity. Please try a different model."}}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    pass

            def log_message(self, *_):
                pass

        http = ThreadingHTTPServer(("127.0.0.1", 0), Overloaded)
        http_thread = threading.Thread(target=http.serve_forever, daemon=True)
        http_thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="goal-resumer-e2e-") as d:
                sock = d + "/app.sock"
                env = os.environ.copy()
                env.update({"CODEX_HOME": d, "CODEX_FIXTURE_KEY": "fixture"})
                config = ["-c", 'model="fixture-model"', "-c", 'model_provider="fixture"',
                          "-c", 'model_providers.fixture.name="fixture"',
                          "-c", f'model_providers.fixture.base_url="http://127.0.0.1:{http.server_port}/v1"',
                          "-c", 'model_providers.fixture.env_key="CODEX_FIXTURE_KEY"',
                          "-c", "model_providers.fixture.request_max_retries=0",
                          "-c", "model_providers.fixture.stream_max_retries=0",
                          "-c", "model_providers.fixture.supports_websockets=false"]
                app = await asyncio.create_subprocess_exec(str(codex), "app-server", "--listen", "unix://" + sock,
                           *config, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                monitor = None
                ws = None
                rpc = None
                try:
                    for _ in range(100):
                        try:
                            ws = await websockets.unix_connect(sock, uri="ws://localhost/")
                            break
                        except (FileNotFoundError, ConnectionRefusedError):
                            await asyncio.sleep(0.02)
                    self.assertIsNotNone(ws)
                    rpc = gr.Rpc(ws)
                    await rpc.initialize()
                    tid = (await rpc.call("thread/start", {"cwd": d, "approvalPolicy": "never",
                            "sandbox": "read-only"}))["thread"]["id"]
                    await rpc.call("thread/goal/set", {"threadId": tid, "objective": "Fixture goal.",
                                   "status": "active", "origin": "user", "tokenBudget": 10000})
                    self.assertTrue(await asyncio.to_thread(started.wait, 5))
                    monitor = subprocess.Popen([sys.executable, str(Path(gr.__file__).resolve()), "serve",
                              "--socket", sock, "--state-dir", d + "/state", "--interval", "0.05",
                              "--initial-delay", "0.05", "--max-delay", "0.1", "--max-retries", "1",
                              "--no-jitter"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    bus = dbus.SessionBus()
                    for _ in range(100):
                        if bus.name_has_owner(gr.BUS_NAME):
                            break
                        if monitor.poll() is not None:
                            self.fail("monitor exited")
                        await asyncio.sleep(0.03)
                    iface = dbus.Interface(bus.get_object(gr.BUS_NAME, gr.BUS_PATH), gr.BUS_INTERFACE)
                    for _ in range(100):
                        status = json.loads(str(iface.Status()))
                        if any(g["threadId"] == tid and g["goalStatus"] == "active" for g in status["goals"]):
                            break
                        await asyncio.sleep(0.03)
                    else:
                        self.fail("monitor never observed the active fixture goal")
                    iface.Pause()
                    release.set()
                    for _ in range(100):
                        if (await rpc.call("thread/goal/get", {"threadId": tid}))["goal"]["status"] == "blocked":
                            break
                        await asyncio.sleep(0.02)
                    else:
                        self.fail("fixture goal did not block")
                    await asyncio.sleep(0.3)
                    self.assertEqual(models, ["fixture-model"])
                    iface.StartAll()
                    for _ in range(100):
                        if len(models) >= 2:
                            break
                        await asyncio.sleep(0.03)
                    self.assertEqual(models, ["fixture-model", "fixture-model"])
                    await asyncio.sleep(0.3)
                    self.assertEqual(len(models), 2)
                finally:
                    release.set()
                    if monitor is not None:
                        monitor.terminate()
                        try:
                            monitor.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            monitor.kill()
                            monitor.wait()
                    if rpc is not None:
                        await rpc.close()
                    if ws is not None:
                        await ws.close()
                    app.terminate()
                    try:
                        await asyncio.wait_for(app.wait(), 4)
                    except asyncio.TimeoutError:
                        app.kill()
                        await app.wait()
        finally:
            http.shutdown()
            http.server_close()
            http_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
