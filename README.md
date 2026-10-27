# Codex Auto Goal Resumer

A Linux user service that resumes a Codex CLI goal after the selected model
returns a capacity error. It keeps the same thread and model and can be
controlled from the terminal, D-Bus, or a desktop tray icon.

## How it works

The service watches currently loaded threads on the shared local Codex
app-server. If an active goal becomes blocked after a recognized model
overload, the service waits and reactivates that goal. Codex then starts its
normal goal continuation, equivalent to resuming the goal in the CLI.

- The first retry is scheduled after about one minute. Later retries use
  exponential backoff with jitter, up to six attempts. A `Retry-After` value
  is honored when the app-server exposes it.
- It does not switch models, bypass approvals, change the goal's token budget,
  or reactivate goals paused by the user. Quota, authentication, and other
  unrelated failures are not treated as model-capacity errors.
- **Start monitoring all CLI sessions** also checks goals that were already
  blocked when monitoring started, provided their block and failure timestamps
  match. An individual blocked thread can be checked with `recover THREAD_ID`.
- A running goal can continue editing files and running commands according to
  that session's existing permissions. The service checks goal and thread state
  immediately before retrying; the Codex protocol does not provide an atomic
  compare-and-set against a concurrent goal edit.

Only threads loaded in the shared local CLI daemon are monitored. Saved but
unloaded sessions, remote sessions, and desktop app sessions using a separate
app-server are outside its scope. The monitor reconnects when the shared server
is available; it does not start or restart Codex itself. The app-server protocol
is experimental and may change across Codex versions.

## Requirements

- Linux with a running user systemd instance and D-Bus session.
- Codex CLI with Goals and the shared app-server. The integration tests were
  exercised with Codex CLI 0.164.0.
- System Python 3.10 or later with `dbus`, PyGObject (`gi`), and `websockets`.
- For the tray icon: PyQt5 and a desktop with a system tray. Some GNOME
  desktops need an extension that supports status notifier icons.

On Debian or Ubuntu, the dependencies can be installed with:

```bash
sudo apt install python3-dbus python3-gi python3-websockets python3-pyqt5
```

## Install

```bash
git clone https://github.com/lolren/codex-auto-goal-resumer-model-at-capacity.git
cd codex-auto-goal-resumer-model-at-capacity
/usr/bin/python3 install.py
```

The installer places the program under your XDG data directory, installs user
systemd and D-Bus service files, creates `~/.local/bin/codex-goal-resumer`, and
starts the monitor and tray icon. It refuses to overwrite an existing file or
installation. Use `--no-tray` on a headless system, or `--no-start` to install
the files without enabling or starting services.

The monitor uses `$CODEX_HOME/app-server-control/app-server-control.sock`, or
the equivalent location under `~/.codex` when `CODEX_HOME` is unset. A custom
socket can be passed to `goal_resumer.py serve --socket PATH`; a systemd user
service receives its own environment, which may differ from your shell.

## Controls

Click the **G** tray icon to start monitoring all CLI sessions, pause automatic
retries, see status, or recover an individual blocked goal. The dot is green
when monitoring, blue while a retry is waiting, amber when paused, and gray
when disconnected. The menu and tooltip show the next retry countdown. Closing
the icon leaves the monitor running.

```bash
codex-goal-resumer status
codex-goal-resumer start-all
codex-goal-resumer pause
codex-goal-resumer resume
codex-goal-resumer recover THREAD_ID
systemctl --user status codex-goal-resumer.service
journalctl --user -u codex-goal-resumer.service -n 50 --no-pager
```

If `~/.local/bin` is not on your PATH, use the full path to the command.
`pause` stops automatic retries while the monitor continues reading status;
`resume` enables them again. `start-all` also considers recently blocked
threads and resets the retry allowance for eligible capacity failures.
`recover` resets the allowance only for the specified thread.

The service exposes `Status`, `Pause`, `Resume`, `StartAll`, and `Recover` on
the current user's D-Bus session:

```bash
busctl --user call org.local.CodexGoalResumer /org/local/CodexGoalResumer org.local.CodexGoalResumer1 Status
```

Retry metadata is stored with private permissions in
`${XDG_STATE_HOME:-~/.local/state}/codex-goal-resumer/state.json`. It contains
thread IDs, retry state, and a digest of each goal, not goal text or credentials.

To pause retries and stop automatic startup:

```bash
codex-goal-resumer pause
systemctl --user disable --now codex-goal-resumer-tray.service
systemctl --user disable --now codex-goal-resumer.service
```

A later D-Bus call can activate the monitor again, but the pause persists.

## Test

```bash
/usr/bin/python3 -m unittest -v test_goal_resumer test_install
dbus-run-session -- /usr/bin/python3 -m unittest -v test_dbus
dbus-run-session -- env QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m unittest -v test_tray
```

The app-server integration tests use a temporary Codex home and a loopback HTTP
server that returns a simulated 503 error. They do not contact the model service
or modify a real Codex thread. They look for a Codex CLI on PATH; set
`CODEX_TEST_BIN` to the CLI binary when needed. If a suitable binary is absent,
the app-server integration tests are skipped.
