#!/usr/bin/python3
"""Safety and protocol tests; all inference traffic is a local 503 fixture."""

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import websockets

import goal_resumer as gr
from test_support import find_test_codex


class ClassificationTests(unittest.TestCase):
    def test_capacity_detection_is_specific(self):
        def turn(info, message="selected model is at capacity. please try a different model."):
            return {"status": "failed", "error": {"codexErrorInfo": info, "message": message}}

        self.assertTrue(gr.is_capacity_failure(turn("serverOverloaded", "server overload")))
        self.assertTrue(gr.is_capacity_failure(turn({"httpConnectionFailed": {"httpStatusCode": 503}})))
        self.assertTrue(gr.is_capacity_failure(turn("other")))
        self.assertFalse(gr.is_capacity_failure(turn("usageLimitExceeded")))
        self.assertFalse(gr.is_capacity_failure(turn("rateLimitExceeded")))
        self.assertFalse(gr.is_capacity_failure(turn({"httpConnectionFailed": {"httpStatusCode": 429}})))
        self.assertFalse(gr.is_capacity_failure(turn("other", "connection failed")))
        t = turn("serverOverloaded")
        t["status"] = "completed"
        self.assertFalse(gr.is_capacity_failure(t))

    def test_retry_after_is_never_shortened(self):
        self.assertEqual(gr.retry_after_seconds({"additionalDetails": "Retry-After: 150"}, 0), 150)
        with tempfile.TemporaryDirectory() as d:
            sup = gr.Supervisor(gr.State(Path(d)), "unused", initial_delay=60, max_delay=300,
                                jitter=False)
            self.assertEqual(sup.delay(0, 150), 150)
            self.assertEqual(sup.delay(3, 0), 300)

    def test_state_is_private_and_does_not_store_objective(self):
        with tempfile.TemporaryDirectory() as d:
            state = gr.State(Path(d) / "state")
            state.data["goals"]["thread_1"] = {"goal_key": gr.goal_key(
                {"threadId": "thread_1", "createdAt": 3, "objective": "private objective"})}
            state.save()
            self.assertEqual(state.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(state.directory.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("private objective", state.path.read_text())

    def test_default_socket_respects_codex_home(self):
        with patch.dict(os.environ, {"CODEX_HOME": "/tmp/example-codex-home"}):
            self.assertEqual(gr.default_socket_path(),
                             "/tmp/example-codex-home/app-server-control/app-server-control.sock")


class SimulatedRpc:
    def __init__(self):
        now = int(time.time())
        self.thread_id = "thread_test"
        self.thread = {"id": self.thread_id, "ephemeral": False, "status": {"type": "active", "activeFlags": []}}
        self.goal = {"threadId": self.thread_id, "createdAt": now, "updatedAt": now,
                     "objective": "test objective", "status": "active", "tokenBudget": 50,
                     "tokensUsed": 3}
        self.turn = {"id": "turn_1", "status": "failed", "completedAt": now,
                     "error": {"codexErrorInfo": "serverOverloaded", "message": "capacity"}}
        self.writes = []

    async def call(self, method, params=None):
        params = params or {}
        if method == "thread/loaded/list":
            return {"data": [self.thread_id]}
        if method == "thread/read":
            return {"thread": dict(self.thread)}
        if method == "thread/goal/get":
            return {"goal": dict(self.goal)}
        if method == "thread/turns/list":
            return {"data": [dict(self.turn)]}
        if method == "thread/goal/set":
            self.writes.append(dict(params))
            self.goal["status"] = params["status"]
            return {"goal": dict(self.goal)}
        raise AssertionError(method)


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="goal-resumer-policy-")
        self.state = gr.State(Path(self.temp.name))
        self.sup = gr.Supervisor(self.state, "unused", initial_delay=0.02, max_delay=1,
                                 max_retries=2, jitter=False)
        self.rpc = SimulatedRpc()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_recheck_blocks_paused_and_exhausted_goals(self):
        await self.sup.scan(self.rpc)  # Observe an active goal before the failure.
        self.rpc.thread["status"] = {"type": "systemError"}
        self.rpc.goal["status"] = "blocked"
        await self.sup.scan(self.rpc)
        await asyncio.sleep(0.03)
        self.rpc.goal["status"] = "paused"
        await self.sup.scan(self.rpc)
        self.assertEqual(self.rpc.writes, [])
        self.rpc.goal["status"] = "blocked"
        self.rpc.goal["tokensUsed"] = 50
        self.state.allow_blocked(self.rpc.thread_id)
        await self.sup.scan(self.rpc)
        await asyncio.sleep(0.03)
        await self.sup.scan(self.rpc)
        self.assertEqual(self.rpc.writes, [])

    async def test_other_failures_and_stale_blocks_are_not_retried(self):
        await self.sup.scan(self.rpc)
        self.rpc.thread["status"] = {"type": "systemError"}
        self.rpc.goal["status"] = "blocked"
        self.rpc.turn["error"] = {"codexErrorInfo": "usageLimitExceeded", "message": "quota"}
        await self.sup.scan(self.rpc)
        await asyncio.sleep(0.03)
        await self.sup.scan(self.rpc)
        self.assertEqual(self.rpc.writes, [])
        self.rpc.goal["status"] = "active"
        self.rpc.thread["status"] = {"type": "active", "activeFlags": []}
        await self.sup.scan(self.rpc)
        self.rpc.goal["status"] = "blocked"
        self.rpc.thread["status"] = {"type": "systemError"}
        self.rpc.turn["error"] = {"codexErrorInfo": "serverOverloaded", "message": "capacity"}
        self.rpc.turn["completedAt"] = self.rpc.goal["updatedAt"] + 120
        await self.sup.scan(self.rpc)
        await asyncio.sleep(0.03)
        await self.sup.scan(self.rpc)
        self.assertEqual(self.rpc.writes, [])

    async def test_already_blocked_requires_explicit_recovery(self):
        self.rpc.thread["status"] = {"type": "systemError"}
        self.rpc.goal["status"] = "blocked"
        await self.sup.scan(self.rpc)
        await asyncio.sleep(0.03)
        await self.sup.scan(self.rpc)
        self.assertEqual(self.rpc.writes, [])
        self.state.allow_blocked(self.rpc.thread_id)
        await self.sup.scan(self.rpc)
        await asyncio.sleep(0.03)
        await self.sup.scan(self.rpc)
        self.assertEqual(len(self.rpc.writes), 1)
        self.assertEqual(self.rpc.writes[0], {"threadId": self.rpc.thread_id,
                                               "status": "active", "origin": "automatic"})

    async def test_explicit_recovery_rearms_exhausted_retry_budget(self):
        await self.sup.scan(self.rpc)
        rec = self.state.data["goals"][self.rpc.thread_id]
        rec.update({"attempts": self.sup.max_retries, "failure_id": "old", "next_retry_at": 5})
        self.state.allow_blocked(self.rpc.thread_id)
        self.assertEqual(rec["attempts"], 0)
        self.assertIsNone(rec["next_retry_at"])

    async def test_start_all_includes_recent_capacity_blocked_sessions(self):
        self.rpc.thread["status"] = {"type": "systemError"}
        self.rpc.goal["status"] = "blocked"
        await self.sup.scan(self.rpc)
        self.assertEqual(self.rpc.writes, [])
        self.state.set_paused(True)
        self.state.start_all()
        self.assertFalse(self.state.data["paused"])
        await self.sup.scan(self.rpc)
        await asyncio.sleep(0.03)
        await self.sup.scan(self.rpc)
        self.assertEqual(len(self.rpc.writes), 1)

    async def test_start_all_does_not_reactivate_an_unrelated_old_block(self):
        self.rpc.thread["status"] = {"type": "systemError"}
        self.rpc.goal["status"] = "blocked"
        self.rpc.turn["completedAt"] = self.rpc.goal["updatedAt"] + 120
        self.state.start_all()
        await self.sup.scan(self.rpc)
        await asyncio.sleep(0.03)
        await self.sup.scan(self.rpc)
        self.assertEqual(self.rpc.writes, [])


class LocalAppServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_app_server_goal_recovery_on_local_capacity_fixture(self):
        codex = find_test_codex()
        if codex is None:
            self.skipTest("Codex CLI >= 0.164.0 not found; set CODEX_TEST_BIN")

        started = threading.Event()
        release = threading.Event()
        request_models = []

        class Overloaded(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length", "0")))
                try:
                    request_models.append(json.loads(raw).get("model"))
                except (ValueError, UnicodeError):
                    request_models.append(None)
                if len(request_models) == 1:
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

        server = ThreadingHTTPServer(("127.0.0.1", 0), Overloaded)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        with tempfile.TemporaryDirectory(prefix="goal-resumer-appserver-") as d:
            sock = os.path.join(d, "app.sock")
            env = os.environ.copy()
            env.update({"CODEX_HOME": d, "CODEX_FIXTURE_KEY": "fixture"})
            config = ["-c", 'model="fixture-model"', "-c", 'model_provider="fixture"',
                      "-c", 'model_providers.fixture.name="fixture"',
                      "-c", f'model_providers.fixture.base_url="http://127.0.0.1:{server.server_port}/v1"',
                      "-c", 'model_providers.fixture.env_key="CODEX_FIXTURE_KEY"',
                      "-c", "model_providers.fixture.request_max_retries=0",
                      "-c", "model_providers.fixture.stream_max_retries=0",
                      "-c", "model_providers.fixture.supports_websockets=false"]
            proc = await asyncio.create_subprocess_exec(str(codex), "app-server", "--listen", "unix://" + sock,
                       *config, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            rpc = None
            ws = None
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
                thread = (await rpc.call("thread/start", {"cwd": d, "approvalPolicy": "never",
                          "sandbox": "read-only"}))["thread"]
                tid = thread["id"]
                await rpc.call("thread/goal/set", {"threadId": tid, "objective": "Fixture goal.",
                               "status": "active", "origin": "user", "tokenBudget": 10000})
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                state = gr.State(Path(d) / "state")
                sup = gr.Supervisor(state, sock, initial_delay=0.03, max_delay=1,
                                    max_retries=1, jitter=False)
                await sup.scan(rpc)  # Observe active before allowing the fixture to fail.
                self.assertTrue(state.data["goals"][tid]["seen_active"])
                release.set()

                async def wait_goal(expected):
                    for _ in range(100):
                        goal = (await rpc.call("thread/goal/get", {"threadId": tid}))["goal"]
                        if goal["status"] == expected:
                            return goal
                        await asyncio.sleep(0.02)
                    self.fail("goal did not reach " + expected)

                await wait_goal("blocked")
                await sup.scan(rpc)  # Schedules only; no immediate retry.
                state.set_paused(True)
                await asyncio.sleep(0.05)
                await sup.scan(rpc)
                self.assertEqual(len(request_models), 1)
                state.set_paused(False)
                await sup.scan(rpc)
                for _ in range(100):
                    if len(request_models) >= 2:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(request_models, ["fixture-model", "fixture-model"])
                after = await wait_goal("blocked")
                self.assertEqual(after["objective"], "Fixture goal.")
                self.assertEqual(after["tokenBudget"], 10000)
                await asyncio.sleep(0.1)
                await sup.scan(rpc)
                await asyncio.sleep(0.1)
                await sup.scan(rpc)
                self.assertEqual(len(request_models), 2, "retry limit must stop further requests")
                self.assertEqual(state.data["goals"][tid]["attempts"], 1)
                restored = gr.State(Path(d) / "state")
                self.assertEqual(restored.data["goals"][tid]["attempts"], 1)
            finally:
                release.set()
                if rpc is not None:
                    await rpc.close()
                if ws is not None:
                    await ws.close()
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 4)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
