"""Process hygiene every ddiver-core app needs before it opens anything.

One process per data directory (two would poison a Telethon session file), a data directory
only its owner can read (the session file is the account), log lines a shipper can index, and
configuration errors that name the setting without echoing its value.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    """One JSON object per line, which is what a log shipper wants to index.

    Deliberately small: a level, a logger, a message and — for an exception — the traceback.
    Anything richer would mean structured logging calls all over the codebase for a project
    whose log volume is a line a minute.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def harden_data_dir(data_dir: Path) -> None:
    """The session file is the account: keep the data directory to the owner only."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        data_dir.chmod(0o700)
    for path in data_dir.glob("*"):
        if path.is_file() and path.suffix in {".session", ".sqlite3", ".sqlite", ".env"}:
            with contextlib.suppress(OSError):
                path.chmod(0o600)


class InstanceLock:
    """One process per data directory; two would poison the Telethon session."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any = None

    def acquire(self) -> bool:
        import fcntl

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("w")
        try:
            fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._handle.close()
            self._handle = None
            return False
        self._handle.write(str(os.getpid()))
        self._handle.flush()
        return True

    def release(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()
            self._handle = None


def describe_config_error(exc: Exception) -> str:
    """Say which setting is wrong without quoting what was in it.

    pydantic's ValidationError renders ``input_value=...`` with the raw value, so printing the
    exception puts a malformed BOT_TOKEN or API_HASH straight into stderr — and from there into
    the container log and Loki. The field name and the reason are what a person needs anyway.
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    try:
        problems = errors()
    except Exception:  # a broken error object must not replace the error we are reporting
        return type(exc).__name__
    lines = []
    for problem in problems:
        field = ".".join(str(part) for part in problem.get("loc", ())) or "settings"
        lines.append(f"{field.upper()}: {problem.get('msg', 'is invalid')}")
    return "; ".join(lines) or str(exc)
