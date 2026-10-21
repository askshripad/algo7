"""
Start/stop the algo process from the API server (for /niftychart UI button).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_started_at: datetime | None = None
_log_buffer: list[str] = []


def _project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()


def _read_stdout(proc: subprocess.Popen) -> None:
    global _log_buffer
    if proc.stdout is None:
        return
    for line in proc.stdout:
        text = line.rstrip()
        _log_buffer.append(text)
        if len(_log_buffer) > 200:
            _log_buffer = _log_buffer[-200:]
        logger.info("[algo] %s", text)


def get_algo_status() -> dict[str, Any]:
    global _proc, _started_at
    with _lock:
        running = _proc is not None and _proc.poll() is None
        pid = _proc.pid if running and _proc else None
        return {
            "running": running,
            "pid": pid,
            "started_at": _started_at.isoformat() if _started_at else None,
            "exit_code": _proc.poll() if _proc and not running else None,
            "embedded": False,
        }


def start_algo(*, dry_run: bool | None = None, extra_args: list[str] | None = None) -> dict[str, Any]:
    """Spawn ``python -m nifty_algo.main`` as a child process."""
    global _proc, _started_at, _log_buffer

    with _lock:
        if _proc is not None and _proc.poll() is None:
            return {"status": "already_running", **get_algo_status()}

        root = _project_root()
        cmd = [sys.executable, "-m", "nifty_algo.main"]
        if dry_run is True:
            cmd.append("--dry-run")
        elif dry_run is False:
            cmd.append("--live")
        if extra_args:
            cmd.extend(extra_args)

        try:
            _proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            _started_at = datetime.now()
            _log_buffer = []
            threading.Thread(target=_read_stdout, args=(_proc,), daemon=True).start()
            logger.info("Algo subprocess started pid=%s cmd=%s", _proc.pid, cmd)
            return {"status": "started", **get_algo_status()}
        except Exception as e:
            logger.exception("Failed to start algo: %s", e)
            return {"status": "error", "message": str(e)}


def stop_algo() -> dict[str, Any]:
    global _proc, _started_at
    with _lock:
        if _proc is None or _proc.poll() is not None:
            _proc = None
            _started_at = None
            return {"status": "not_running"}
        try:
            _proc.terminate()
            try:
                _proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                _proc.kill()
                _proc.wait(timeout=3)
            code = _proc.returncode
            _proc = None
            _started_at = None
            return {"status": "stopped", "exit_code": code}
        except Exception as e:
            return {"status": "error", "message": str(e)}


def get_algo_logs(tail: int = 50) -> list[str]:
    with _lock:
        n = max(1, min(tail, 200))
        return _log_buffer[-n:]
