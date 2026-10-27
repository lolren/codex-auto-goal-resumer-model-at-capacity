"""Shared helpers for optional local Codex app-server integration tests."""

import os
import re
import shutil
import subprocess
from pathlib import Path


def find_test_codex():
    """Return a supported CLI, or None when the integration fixture must skip."""
    candidate = os.environ.get("CODEX_TEST_BIN") or shutil.which("codex")
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    try:
        out = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                             timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", out)
    if not match or tuple(int(x) for x in match.groups()) < (0, 164, 0):
        return None
    return path
