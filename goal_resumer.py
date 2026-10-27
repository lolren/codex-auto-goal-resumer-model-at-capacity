#!/usr/bin/python3
"""Retry Codex goals blocked by model-capacity errors on the local daemon."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import email.utils
import hashlib
import json
import logging
import os
import random
import re
import signal
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
import websockets


BUS_NAME = "org.local.CodexGoalResumer"
BUS_PATH = "/org/local/CodexGoalResumer"
BUS_INTERFACE = "org.local.CodexGoalResumer1"
STATE_VERSION = 1
LOG = logging.getLogger("codex-goal-resumer")
DENIED_ERRORS = {
    "contextWindowExceeded", "sessionBudgetExceeded", "usageLimitExceeded",
    "rateLimitExceeded", "flexUnavailable", "cyberPolicy", "misalignmentPolicyViolation",
    "tooManyDenials", "unauthorized", "badRequest", "sandboxError",
}


def goal_key(goal: dict[str, Any]) -> str:
    # The objective is needed to distinguish a replacement goal, but is never stored.
    raw = json.dumps([goal.get("threadId"), goal.get("createdAt"), goal.get("objective")],
                     ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_capacity_failure(turn: dict[str, Any]) -> bool:
    """Accept only a failed turn with the model-overload code or exact UI wording."""
    if turn.get("status") != "failed" or not isinstance(turn.get("error"), dict):
        return False
    err = turn["error"]
    info = err.get("codexErrorInfo")
    if isinstance(info, str) and info in DENIED_ERRORS:
        return False
    if info == "serverOverloaded":
        return True
    message = str(err.get("message") or "").casefold()
    if "selected model is at capacity" not in message:
        return False
    if info is None or info == "other":
        return True
    if isinstance(info, dict) and len(info) == 1:
        variant, details = next(iter(info.items()))
        return (variant in {"httpConnectionFailed", "responseStreamConnectionFailed",
                            "responseStreamDisconnected", "responseTooManyFailedAttempts"}
                and isinstance(details, dict) and details.get("httpStatusCode") in (None, 503))
    return False


def retry_after_seconds(error: dict[str, Any], now: float) -> float:
    """Honor a Retry-After value if the app-server happens to expose one."""
    details = str(error.get("additionalDetails") or "")
    match = re.search(r"(?im)\bretry-after\s*[:=]\s*([^\r\n,]+(?:,\s*\d{2}\s+\w+\s+\d{4}[^\r\n]*)?)", details)
    if not match:
        return 0.0
    value = match.group(1).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            date = email.utils.parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=dt.timezone.utc)
            return max(0.0, date.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return 0.0


def valid_thread_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value))


class State:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "state.json"
        self.lock = threading.RLock()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        if self.path.exists():
            with self.path.open(encoding="utf-8") as f:
                self.data = json.load(f)
            if self.data.get("version") != STATE_VERSION or not isinstance(self.data.get("goals"), dict):
                raise ValueError("unsupported or invalid goal-resumer state")
        else:
            self.data = {"version": STATE_VERSION, "paused": False, "goals": {}}
            self.save()
        self.connected = False
        self.loaded = 0
        self.last_error = None
        self.last_scan = None

    def save(self) -> None:
        """Persist retry limits before acting, including across service restarts."""
        with self.lock:
            fd, temp = tempfile.mkstemp(prefix=".state-", dir=self.directory)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, separators=(",", ":"), sort_keys=True)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp, self.path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)

    def set_paused(self, value: bool) -> str:
        with self.lock:
            self.data["paused"] = value
            self.save()
        return "Automatic retries paused." if value else "Automatic retries enabled."

    def start_all(self) -> str:
        """Start monitoring, including capacity blocks already present at start."""
        with self.lock:
            self.data["paused"] = False
            self.data["start_all_generation"] = int(self.data.get("start_all_generation", 0)) + 1
            # Only a short window is needed to inspect currently loaded threads.
            self.data["start_all_until"] = time.time() + 120
            self.save()
        return "Monitoring all sessions on the shared Codex daemon."

    def allow_blocked(self, thread_id: str) -> str:
        if not valid_thread_id(thread_id):
            raise ValueError("Invalid thread ID")
        with self.lock:
            rec = self.data["goals"].setdefault(thread_id, {})
            # Explicit one-hour authorization to consider an already-blocked thread;
            # normal capacity, goal, budget and liveness checks still apply.
            rec["manual_until"] = time.time() + 3600
            rec["attempts"] = 0
            rec["failure_id"] = None
            rec["next_retry_at"] = None
            rec["reason"] = "explicit recovery requested"
            self.save()
        return "The thread will be checked for a recoverable capacity failure."

    def status(self) -> dict[str, Any]:
        with self.lock:
            goals = []
            for tid, rec in self.data["goals"].items():
                if rec.get("last_loaded"):
                    goals.append({"threadId": tid, "goalStatus": rec.get("goal_status"),
                                  "attempts": rec.get("attempts", 0),
                                  "nextRetryAt": rec.get("next_retry_at"),
                                  "reason": rec.get("reason")})
            return {"paused": self.data["paused"], "connected": self.connected,
                    "loadedThreads": self.loaded, "lastScan": self.last_scan,
                    "lastError": self.last_error, "goals": goals}


class RpcError(Exception):
    pass


class Rpc:
    def __init__(self, ws):
        self.ws = ws
        self.next_id = 1
        self.pending: dict[int, asyncio.Future] = {}
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                obj = json.loads(raw)
                # Ignore unrelated thread events and server requests. This client
                # never subscribes to or answers another client's approvals.
                if "method" not in obj and isinstance(obj.get("id"), int):
                    future = self.pending.get(obj["id"])
                    if future and not future.done():
                        future.set_result(obj)
        except (OSError, ValueError, websockets.WebSocketException) as exc:
            LOG.info("Codex connection closed: %s", type(exc).__name__)
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RpcError("Codex connection closed"))

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        req_id = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[req_id] = future
        try:
            await self.ws.send(json.dumps({"id": req_id, "method": method, "params": params or {}}))
            response = await asyncio.wait_for(future, timeout=10)
            if "error" in response:
                raise RpcError(str(response["error"].get("message", "RPC error")))
            result = response.get("result")
            if not isinstance(result, dict):
                raise RpcError("Unexpected Codex response")
            return result
        finally:
            self.pending.pop(req_id, None)

    async def initialize(self) -> None:
        await self.call("initialize", {"clientInfo": {"name": "codex_goal_resumer",
                         "title": "Codex Goal Resumer", "version": "1.0.0"}})
        await self.ws.send(json.dumps({"method": "initialized"}))

    async def close(self) -> None:
        self.reader.cancel()
        try:
            await self.reader
        except asyncio.CancelledError:
            pass


class Supervisor:
    def __init__(self, state: State, socket: str, interval: float = 5.0,
                 initial_delay: float = 60.0, max_delay: float = 1800.0,
                 max_retries: int = 6, jitter: bool = True):
        self.state = state
        self.socket = socket
        self.interval = interval
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.jitter = jitter
        self.stopping = threading.Event()
        self.semaphore: asyncio.Semaphore | None = None

    def delay(self, attempts: int, server_wait: float = 0.0) -> float:
        base = min(self.max_delay, self.initial_delay * (2 ** min(attempts, 20)))
        if self.jitter:
            base *= random.uniform(1.0, 1.15)
        return max(base, server_wait)

    @staticmethod
    def _eligible_goal(goal: dict[str, Any]) -> bool:
        budget = goal.get("tokenBudget")
        used = goal.get("tokensUsed")
        return (goal.get("status") == "blocked" and
                not (isinstance(budget, int) and isinstance(used, int) and used >= budget))

    async def inspect(self, rpc: Rpc, thread_id: str) -> None:
        assert self.semaphore is not None
        async with self.semaphore:
            try:
                th = (await rpc.call("thread/read", {"threadId": thread_id})).get("thread")
                if not isinstance(th, dict):
                    return
                # Ephemeral or unloaded threads cannot safely be recovered here.
                if th.get("ephemeral"):
                    return
                goal = (await rpc.call("thread/goal/get", {"threadId": thread_id})).get("goal")
                if not isinstance(goal, dict):
                    return
                runtime = (th.get("status") or {}).get("type")
                now = time.time()
                signature = goal_key(goal)
                with self.state.lock:
                    rec = self.state.data["goals"].get(thread_id, {})
                    if rec.get("goal_key") != signature:
                        manual = rec.get("manual_until", 0)
                        rec = {"goal_key": signature, "attempts": 0,
                               "seen_active": False, "manual_until": manual}
                        self.state.data["goals"][thread_id] = rec
                    rec.update({"last_loaded": True, "last_seen": now,
                                "goal_status": goal.get("status")})
                    if goal.get("status") == "active":
                        rec["seen_active"] = True
                    if goal.get("status") in {"paused", "complete", "budgetLimited", "usageLimited"}:
                        rec.update({"seen_active": False, "next_retry_at": None,
                                    "failure_id": None, "attempts": 0, "reason": goal.get("status")})

                # Active threads may be working, waiting for input, or pending approval.
                if runtime not in {"idle", "systemError"}:
                    return
                result = await rpc.call("thread/turns/list", {"threadId": thread_id,
                                        "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"})
                turns = result.get("data") or []
                turn = turns[0] if turns and isinstance(turns[0], dict) else {}
                if not self._eligible_goal(goal):
                    if goal.get("status") == "active" and turn.get("status") == "completed":
                        with self.state.lock:
                            rec.update({"attempts": 0, "failure_id": None,
                                        "next_retry_at": None, "reason": None})
                    return
                if not is_capacity_failure(turn) or not turn.get("id"):
                    with self.state.lock:
                        rec.update({"seen_active": False, "failure_id": None,
                                    "next_retry_at": None, "attempts": 0,
                                    "reason": "blocked for another reason"})
                    return
                # Do not reactivate an older block merely because a later manual
                # turn happened to hit capacity. The ordinary automatic case has
                # a goal update and a failure completion at essentially the same time.
                blocked_at, completed_at = goal.get("updatedAt"), turn.get("completedAt")
                with self.state.lock:
                    explicitly_requested = rec.get("manual_until", 0) > now
                if (not explicitly_requested and isinstance(blocked_at, int)
                        and isinstance(completed_at, int) and abs(blocked_at - completed_at) > 30):
                    return
                with self.state.lock:
                    generation = int(self.state.data.get("start_all_generation", 0))
                    recent_failure = (isinstance(blocked_at, int) and isinstance(completed_at, int)
                                      and abs(blocked_at - completed_at) <= 30)
                    if (generation and self.state.data.get("start_all_until", 0) > now
                            and rec.get("start_all_generation") != generation and recent_failure):
                        # This is an explicit user request to include sessions that
                        # were already blocked when monitoring began. A matching
                        # block timestamp is still required for this broad action.
                        rec.update({"start_all_generation": generation, "seen_active": True,
                                    "attempts": 0, "failure_id": None, "next_retry_at": None})
                    permitted = rec.get("seen_active") or rec.get("manual_until", 0) > now
                    if not permitted:
                        rec.update({"reason": "already blocked before monitoring", "next_retry_at": None})
                        return
                    attempts = int(rec.get("attempts", 0))
                    if attempts >= self.max_retries:
                        rec.update({"reason": "retry limit reached", "next_retry_at": None})
                        return
                    if rec.get("failure_id") != turn["id"] or not rec.get("next_retry_at"):
                        wait = retry_after_seconds(turn.get("error") or {}, now)
                        rec.update({"failure_id": turn["id"],
                                    "next_retry_at": now + self.delay(attempts, wait),
                                    "reason": "model at capacity"})
                    due = rec["next_retry_at"]
                    paused = self.state.data["paused"]
                if paused or now < due:
                    return
                await self.retry(rpc, thread_id, signature, turn["id"])
            except (RpcError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
                # Do not emit session contents or error details from the RPC.
                LOG.debug("Could not inspect thread %s: %s", thread_id, type(exc).__name__)

    async def retry(self, rpc: Rpc, thread_id: str, signature: str, failure_id: str) -> None:
        # Re-read all conditions immediately before the write. No model, sandbox,
        # approval, budget, objective, or working-directory setting is overridden.
        th = (await rpc.call("thread/read", {"threadId": thread_id})).get("thread") or {}
        goal = (await rpc.call("thread/goal/get", {"threadId": thread_id})).get("goal") or {}
        turns = (await rpc.call("thread/turns/list", {"threadId": thread_id, "limit": 1,
                      "sortDirection": "desc", "itemsView": "notLoaded"})).get("data") or []
        turn = turns[0] if turns else {}
        if ((th.get("status") or {}).get("type") not in {"idle", "systemError"}
                or not self._eligible_goal(goal) or goal_key(goal) != signature
                or turn.get("id") != failure_id or not is_capacity_failure(turn)):
            return
        with self.state.lock:
            rec = self.state.data["goals"].get(thread_id, {})
            now = time.time()
            if (self.state.data["paused"] or rec.get("goal_key") != signature
                    or rec.get("failure_id") != failure_id
                    or not rec.get("next_retry_at") or now < rec["next_retry_at"]
                    or not (rec.get("seen_active") or rec.get("manual_until", 0) > now)
                    or int(rec.get("attempts", 0)) >= self.max_retries):
                return
            # Persist before the RPC. If the connection drops at an uncertain
            # outcome, a later retry still consumes the bounded retry allowance.
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            rec["next_retry_at"] = now + self.delay(rec["attempts"])
            rec["reason"] = "retry submitted"
            rec["manual_until"] = 0
            rec["seen_active"] = True
            self.state.save()
            # Holding the lock until this RPC returns makes Pause wait for an
            # already-in-flight request rather than report a premature pause.
            await rpc.call("thread/goal/set", {"threadId": thread_id,
                           "status": "active", "origin": "automatic"})
        LOG.info("Retried blocked goal %s (attempt %s/%s)",
                 thread_id, rec["attempts"], self.max_retries)

    async def scan(self, rpc: Rpc) -> None:
        ids = (await rpc.call("thread/loaded/list")).get("data") or []
        ids = [x for x in ids if isinstance(x, str) and valid_thread_id(x)]
        with self.state.lock:
            self.state.loaded = len(ids)
            for rec in self.state.data["goals"].values():
                rec["last_loaded"] = False
        self.semaphore = asyncio.Semaphore(8)
        await asyncio.gather(*(self.inspect(rpc, tid) for tid in ids))
        with self.state.lock:
            self.state.last_scan = time.time()
            self.state.last_error = None
            # Retain retry history for a week, then discard inactive metadata.
            cutoff = time.time() - 7 * 86400
            for tid, rec in list(self.state.data["goals"].items()):
                if not rec.get("last_loaded") and rec.get("last_seen", 0) < cutoff and rec.get("manual_until", 0) < time.time():
                    del self.state.data["goals"][tid]
            self.state.save()

    async def run(self) -> None:
        while not self.stopping.is_set():
            try:
                async with websockets.unix_connect(self.socket, uri="ws://localhost/",
                                                   open_timeout=5, close_timeout=2,
                                                   max_size=16 * 1024 * 1024) as ws:
                    rpc = Rpc(ws)
                    try:
                        await rpc.initialize()
                        with self.state.lock:
                            self.state.connected = True
                            self.state.last_error = None
                        LOG.info("Connected to the existing Codex app-server")
                        while not self.stopping.is_set():
                            await self.scan(rpc)
                            await asyncio.sleep(self.interval)
                    finally:
                        await rpc.close()
            except (OSError, RpcError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
                with self.state.lock:
                    self.state.connected = False
                    self.state.last_error = type(exc).__name__
                LOG.info("Codex app-server unavailable; will reconnect (%s)", type(exc).__name__)
                await asyncio.sleep(min(15.0, self.interval * 2))
        with self.state.lock:
            self.state.connected = False


class BusService(dbus.service.Object):
    def __init__(self, bus, state: State):
        self.name = dbus.service.BusName(BUS_NAME, bus=bus, do_not_queue=True)
        super().__init__(self.name, BUS_PATH)
        self.state = state

    @dbus.service.method(BUS_INTERFACE, in_signature="", out_signature="s")
    def Status(self):
        return json.dumps(self.state.status(), sort_keys=True)

    @dbus.service.method(BUS_INTERFACE, in_signature="", out_signature="s")
    def Pause(self):
        return self.state.set_paused(True)

    @dbus.service.method(BUS_INTERFACE, in_signature="", out_signature="s")
    def Resume(self):
        return self.state.set_paused(False)

    @dbus.service.method(BUS_INTERFACE, in_signature="", out_signature="s")
    def StartAll(self):
        return self.state.start_all()

    @dbus.service.method(BUS_INTERFACE, in_signature="s", out_signature="s")
    def Recover(self, thread_id):
        return self.state.allow_blocked(str(thread_id))


def serve(args) -> None:
    DBusGMainLoop(set_as_default=True)
    state = State(Path(args.state_dir).expanduser())
    bus = dbus.SessionBus()
    service = BusService(bus, state)
    supervisor = Supervisor(state, os.path.expanduser(args.socket), args.interval,
                            args.initial_delay, args.max_delay, args.max_retries,
                            not args.no_jitter)
    mainloop = GLib.MainLoop()
    failed = threading.Event()

    def worker_main():
        try:
            asyncio.run(supervisor.run())
        except Exception as exc:
            # Make systemd restart the service if the monitor dies unexpectedly.
            LOG.error("Goal monitor stopped unexpectedly: %s", type(exc).__name__)
            failed.set()
            GLib.idle_add(mainloop.quit)

    worker = threading.Thread(target=worker_main, name="codex-goal-monitor", daemon=True)
    worker.start()
    signal.signal(signal.SIGTERM, lambda *_: mainloop.quit())
    signal.signal(signal.SIGINT, lambda *_: mainloop.quit())
    try:
        mainloop.run()
    finally:
        supervisor.stopping.set()
        worker.join(timeout=12)
        service.remove_from_connection()
    if failed.is_set():
        raise SystemExit(1)


def positive_float(value: str) -> float:
    number = float(value)
    if not 0 < number < 86400:
        raise argparse.ArgumentTypeError("must be between 0 and 86400 seconds")
    return number


def default_socket_path() -> str:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    return str(codex_home / "app-server-control" / "app-server-control.sock")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("serve")
    run.add_argument("--socket", default=default_socket_path())
    run.add_argument("--state-dir", default=os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "codex-goal-resumer"))
    run.add_argument("--interval", type=positive_float, default=5.0)
    run.add_argument("--initial-delay", type=positive_float, default=60.0)
    run.add_argument("--max-delay", type=positive_float, default=1800.0)
    run.add_argument("--max-retries", type=int, choices=range(1, 21), default=6)
    run.add_argument("--no-jitter", action="store_true", help=argparse.SUPPRESS)
    for cmd in ("status", "pause", "resume", "start-all"):
        sub.add_parser(cmd)
    rec = sub.add_parser("recover")
    rec.add_argument("thread_id")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "serve":
        serve(args)
    else:
        try:
            bus = dbus.SessionBus()
            obj = bus.get_object(BUS_NAME, BUS_PATH)
            iface = dbus.Interface(obj, BUS_INTERFACE)
            if args.command == "status":
                print(json.dumps(json.loads(str(iface.Status())), indent=2))
            elif args.command == "pause":
                print(iface.Pause())
            elif args.command == "resume":
                print(iface.Resume())
            elif args.command == "start-all":
                print(iface.StartAll())
            else:
                if not valid_thread_id(args.thread_id):
                    parser.error("Invalid thread ID")
                print(iface.Recover(args.thread_id))
        except dbus.DBusException as exc:
            parser.exit(1, f"Goal-resumer D-Bus service unavailable: {exc.get_dbus_name()}\n")


if __name__ == "__main__":
    main()
