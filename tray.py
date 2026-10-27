#!/usr/bin/python3
"""KDE system-tray controls for the Codex goal-resumer user service."""

from __future__ import annotations

import json
import sys
import time

from PyQt5.QtCore import QObject, QRectF, Qt, QTimer
from PyQt5.QtDBus import (QDBusConnection, QDBusInterface, QDBusMessage,
                          QDBusPendingCallWatcher, QDBusPendingReply)
from PyQt5.QtGui import QColor, QCursor, QFont, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (QApplication, QInputDialog, QMenu, QMessageBox,
                             QSystemTrayIcon)


BUS_NAME = "org.local.CodexGoalResumer"
BUS_PATH = "/org/local/CodexGoalResumer"
BUS_INTERFACE = "org.local.CodexGoalResumer1"
TRAY_BUS_NAME = "org.local.CodexGoalResumer.Tray"


def pending_retries(goals):
    return [g for g in goals if isinstance(g, dict) and g.get("goalStatus") == "blocked"
            and g.get("reason") == "model at capacity" and g.get("nextRetryAt")]


def describe_delay(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def make_icon(state: str) -> QIcon:
    color = {"active": "#38d596", "pending": "#60a5fa",
             "paused": "#f4b84a", "offline": "#94a3b8"}[state]
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64):
        canvas = QPixmap(size, size)
        canvas.fill(Qt.transparent)
        p = QPainter(canvas)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(size / 64.0, size / 64.0)
        p.setPen(QPen(QColor("#64748b"), 1.5))
        p.setBrush(QColor("#1e293b"))
        p.drawRoundedRect(QRectF(3, 3, 58, 58), 17, 17)
        font = QFont("Sans Serif")
        font.setPixelSize(37)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("#f8fafc"))
        p.drawText(QRectF(6, 6, 46, 51), Qt.AlignCenter, "G")
        p.setPen(QPen(QColor("#1e293b"), 4))
        p.setBrush(QColor(color))
        p.drawEllipse(QRectF(43, 42, 18, 18))
        p.end()
        icon.addPixmap(canvas)
    return icon


class DbusTransport(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.connection = QDBusConnection.sessionBus()
        self.pending = set()

    def call(self, method, args, callback):
        interface = QDBusInterface(BUS_NAME, BUS_PATH, BUS_INTERFACE, self.connection, self)
        watcher = QDBusPendingCallWatcher(interface.asyncCall(method, *args), self)
        self.pending.add(watcher)

        def done(w):
            try:
                message = QDBusPendingReply(w).reply()
                if message.type() == QDBusMessage.ErrorMessage:
                    callback(None, message.errorMessage() or "D-Bus request failed")
                else:
                    values = message.arguments()
                    callback(values[0] if values else None, None)
            finally:
                self.pending.discard(w)
                w.deleteLater()
                interface.deleteLater()

        watcher.finished.connect(done)


class MonitorTray(QObject):
    def __init__(self, app, transport=None, show_icon=True):
        super().__init__()
        self.app = app
        self.transport = transport or DbusTransport(self)
        self.snapshot = None
        self.poll_in_flight = False
        self.show_icon = show_icon
        self.announced_retries = set()
        self.icons = {name: make_icon(name) for name in ("active", "pending", "paused", "offline")}
        self.menu = QMenu()
        self.summary = self.menu.addAction("Connecting to goal monitor…")
        self.summary.setEnabled(False)
        self.counts = self.menu.addAction("Waiting for status")
        self.counts.setEnabled(False)
        self.menu.addSeparator()
        self.start_action = self.menu.addAction(QIcon.fromTheme("media-playback-start"),
                                                "Start monitoring all CLI sessions")
        self.start_action.triggered.connect(self.start_all)
        self.pause_action = self.menu.addAction(QIcon.fromTheme("media-playback-pause"), "Pause automatic retries")
        self.pause_action.triggered.connect(self.pause)
        self.menu.addSeparator()
        self.details_action = self.menu.addAction("Show status…")
        self.details_action.triggered.connect(self.show_status)
        self.recover_action = self.menu.addAction("Recover a blocked goal…")
        self.recover_action.triggered.connect(self.recover)
        self.menu.addSeparator()
        self.close_action = self.menu.addAction("Close tray icon")
        self.close_action.triggered.connect(self.close)

        self.tray = QSystemTrayIcon(self.icons["offline"], self)
        self.tray.setContextMenu(self.menu)
        self.tray.setToolTip("Codex Goal Monitor — connecting")
        self.tray.activated.connect(self.activated)
        if show_icon:
            self.tray.show()
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self):
        if self.poll_in_flight:
            return
        self.poll_in_flight = True
        self.transport.call("Status", [], self._status_result)

    def _status_result(self, value, error):
        self.poll_in_flight = False
        if error:
            self.snapshot = None
            self.summary.setText("Goal monitor unavailable")
            self.counts.setText("The user service is not responding")
            self.start_action.setEnabled(False)
            self.pause_action.setEnabled(False)
            self.recover_action.setEnabled(False)
            self.tray.setIcon(self.icons["offline"])
            self.tray.setToolTip("Codex Goal Monitor — service unavailable")
            return
        try:
            data = json.loads(str(value))
            if not isinstance(data, dict) or not isinstance(data.get("goals"), list):
                raise ValueError("invalid status")
        except (TypeError, ValueError):
            self._status_result(None, "Invalid service status")
            return
        self.snapshot = data
        online = bool(data.get("connected"))
        paused = bool(data.get("paused"))
        goals = data["goals"]
        pending = pending_retries(goals)
        pending_count = len(pending)
        next_wait = (describe_delay(min(float(g["nextRetryAt"]) for g in pending) - time.time())
                     if pending else None)
        total = int(data.get("loadedThreads") or 0)
        self.start_action.setEnabled(True)
        self.pause_action.setEnabled(not paused)
        self.recover_action.setEnabled(online)
        if not online:
            label, icon = "Waiting for the Codex CLI daemon", "offline"
        elif paused:
            label, icon = "Automatic retries paused", "paused"
        elif pending_count:
            label, icon = "Monitoring all CLI sessions", "pending"
        else:
            label, icon = "Monitoring all CLI sessions", "active"
        self.summary.setText(label)
        retry_text = (f"{pending_count} waiting; next in {next_wait}" if pending_count else "0 waiting")
        self.counts.setText(f"{total} loaded threads  •  {len(goals)} goals  •  {retry_text}")
        self.tray.setIcon(self.icons[icon])
        self.tray.setToolTip(f"Codex Goal Monitor — {label}\n{len(goals)} goals; {retry_text}")
        if online and not paused and self.show_icon:
            keys = {(g.get("threadId"), g.get("attempts"), g.get("nextRetryAt")) for g in pending}
            if keys - self.announced_retries:
                self.tray.showMessage("Codex model at capacity",
                                      f"A goal will retry in about {next_wait}.",
                                      QSystemTrayIcon.Information, 5000)
            self.announced_retries = keys

    def command(self, name, args=None):
        def done(value, error):
            if error:
                self.tray.showMessage("Codex Goal Monitor", "The request could not be completed.",
                                      QSystemTrayIcon.Warning, 5000)
            self.refresh()
        self.transport.call(name, args or [], done)

    def start_all(self):
        self.command("StartAll")

    def pause(self):
        self.command("Pause")

    def recover(self):
        thread_id, ok = QInputDialog.getText(None, "Recover a blocked goal", "Codex thread ID:")
        if ok and thread_id.strip():
            self.command("Recover", [thread_id.strip()])

    def show_status(self):
        data = self.snapshot
        if not data:
            QMessageBox.information(None, "Codex Goal Monitor", "The monitor service is unavailable.")
            return
        pending = pending_retries(data["goals"])
        if not data.get("connected"):
            first = "Waiting for the shared Codex CLI daemon."
        elif data.get("paused"):
            first = "Automatic retries are paused."
        else:
            first = "Monitoring all sessions on the shared Codex CLI daemon."
        message = (f"{first}\n\n{data.get('loadedThreads', 0)} loaded threads · "
                   f"{len(data['goals'])} goals · {len(pending)} retries waiting.\n\n"
                   "Separate desktop app sessions are not connected to this monitor.")
        if pending:
            next_delay = describe_delay(min(float(g["nextRetryAt"]) for g in pending) - time.time())
            message += f"\n\nNext retry in about {next_delay}."
        QMessageBox.information(None, "Codex Goal Monitor", message)

    def activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.menu.popup(QCursor.pos())

    def close(self):
        self.tray.hide()
        self.app.quit()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Codex Goal Monitor")
    app.setDesktopFileName("codex-goal-resumer-tray")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray is available", file=sys.stderr)
        return 1
    bus = QDBusConnection.sessionBus()
    if not bus.registerService(TRAY_BUS_NAME):
        # Keep a single icon if a second launcher is used.
        return 0
    monitor = MonitorTray(app)
    result = app.exec_()
    bus.unregisterService(TRAY_BUS_NAME)
    return result


if __name__ == "__main__":
    sys.exit(main())
