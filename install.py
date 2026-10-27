#!/usr/bin/python3
"""Install the goal resumer as user-level systemd and D-Bus services."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PYTHON = Path("/usr/bin/python3")


def systemd_quote(value: Path) -> str:
    text = str(value)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError("installation paths may not contain control characters")
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{text}"'


def service_unit(script: Path) -> str:
    return f"""[Unit]
Description=Resume Codex goals after model-capacity failures
After=dbus.service

[Service]
Type=dbus
BusName=org.local.CodexGoalResumer
ExecStart=/usr/bin/python3 {systemd_quote(script)} serve
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
"""


def tray_unit(script: Path) -> str:
    return f"""[Unit]
Description=Codex goal monitor tray icon
After=graphical-session-pre.target codex-goal-resumer.service
Wants=codex-goal-resumer.service
PartOf=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 {systemd_quote(script)}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
"""


DBUS_SERVICE = """[D-BUS Service]
Name=org.local.CodexGoalResumer
Exec=/usr/bin/systemctl --user start codex-goal-resumer.service
SystemdService=codex-goal-resumer.service
"""


def install(home: Path, config_home: Path, data_home: Path, tray: bool, start: bool) -> None:
    if not PYTHON.is_file():
        raise RuntimeError("/usr/bin/python3 is required")
    imports = "import dbus, dbus.service, gi, websockets"
    if tray:
        imports += "; import PyQt5.QtWidgets, PyQt5.QtDBus"
    check = subprocess.run([str(PYTHON), "-c", imports], capture_output=True, text=True)
    if check.returncode:
        raise RuntimeError("System Python dependencies are missing. See the README installation requirements.")
    if start and not shutil.which("systemctl"):
        raise RuntimeError("systemctl is required to start the user service")

    app = data_home / "codex-goal-resumer"
    units = config_home / "systemd" / "user"
    bus_services = data_home / "dbus-1" / "services"
    bin_dir = home / ".local" / "bin"
    script = app / "goal_resumer.py"
    tray_script = app / "tray.py"
    target_data = {
        script: (ROOT / "goal_resumer.py").read_bytes(),
        units / "codex-goal-resumer.service": service_unit(script).encode(),
        bus_services / "org.local.CodexGoalResumer.service": DBUS_SERVICE.encode(),
    }
    if tray:
        target_data[tray_script] = (ROOT / "tray.py").read_bytes()
        target_data[units / "codex-goal-resumer-tray.service"] = tray_unit(tray_script).encode()
    link = bin_dir / "codex-goal-resumer"
    conflicts = [p for p in (*target_data, link) if os.path.lexists(p)]
    if conflicts:
        raise FileExistsError("Existing installation files were preserved: " + ", ".join(map(str, conflicts)))

    created = []
    try:
        for path, content in target_data.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation keeps an unrelated file from being replaced.
            with path.open("xb") as f:
                created.append(path)
                f.write(content)
            if path in (script, tray_script):
                path.chmod(0o755)
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(script, link)
        created.append(link)
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise

    print(f"Installed application in {app}")
    if start:
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", "codex-goal-resumer.service"], check=True)
            if tray:
                subprocess.run(["systemctl", "--user", "enable", "--now", "codex-goal-resumer-tray.service"], check=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("Files are installed, but systemd could not start a service; inspect systemctl --user status.") from exc
        print("Monitoring started. Use ~/.local/bin/codex-goal-resumer status to check it.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-tray", action="store_true", help="install the monitor without a tray icon")
    parser.add_argument("--no-start", action="store_true", help="install files without starting or enabling services")
    parser.add_argument("--home", type=Path, help="install under an alternate home; requires --no-start")
    args = parser.parse_args()
    if args.home and not args.no_start:
        parser.error("--home requires --no-start")
    home = (args.home or Path.home()).expanduser().resolve()
    config_home = home / ".config" if args.home else Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    data_home = home / ".local" / "share" if args.home else Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    if not config_home.is_absolute() or not data_home.is_absolute():
        parser.error("XDG_CONFIG_HOME and XDG_DATA_HOME must be absolute paths")
    try:
        install(home, config_home, data_home, tray=not args.no_tray, start=not args.no_start)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Installation failed: {exc}\n")


if __name__ == "__main__":
    main()
