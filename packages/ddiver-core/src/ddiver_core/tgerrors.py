"""Telethon errors after which restarting cannot help.

The supervisor (``ddiver_core.app``) parks itself in degraded mode instead of restarting when
one of these ends a run: a dead session or a locked session file only gets worse with retries.
They are Telethon's error classes and nothing else, so they sit apart from the code that builds
the listener's client.
"""

from __future__ import annotations

import sqlite3

from telethon import errors
from telethon.errors import common as common_errors


def is_fatal_auth_error(exc: BaseException) -> bool:
    """Errors that mean the session is dead and only ``ddiver login`` can fix it.

    Classified by base class rather than by a list of names: ``UnauthorizedError`` (401) covers
    session revoked/expired and auth key unregistered, ``AuthKeyError`` (406) covers the
    duplicated key you get when two processes share one session file, and ``AuthKeyNotFound``
    is a plain ``Exception`` raised by the transport, not an ``RPCError``.
    """
    if isinstance(exc, errors.SessionPasswordNeededError):
        return False  # 401, but it only means "2FA password required" during login
    return isinstance(
        exc,
        (
            errors.UnauthorizedError,
            errors.AuthKeyError,
            common_errors.AuthKeyNotFound,
            errors.PhoneNumberBannedError,
        ),
    )


def is_session_file_error(exc: BaseException) -> bool:
    """A locked or corrupt Telethon session file — usually a second instance."""
    return isinstance(exc, sqlite3.OperationalError | sqlite3.DatabaseError)
