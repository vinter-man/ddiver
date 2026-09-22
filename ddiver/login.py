"""Login that works without a terminal.

Telethon's own ``client.start()`` prompts with ``input()``, which fails wherever there is no
TTY (a `!` shell, a detached container, CI). The flow here is split into separate commands and
keeps the one piece of state Telethon holds only in memory — ``phone_code_hash`` — in a file
next to the session, so each step can run as its own process:

    ddiver login --phone +380…      → Telegram sends the code
    ddiver login --code 12345       → signs in, or asks for the 2FA password
    ddiver login --password secret  → finishes a 2FA account

An interactive terminal still gets the classic prompt flow.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import errors

log = logging.getLogger(__name__)

STATE_NAME = "login_state.json"
STATE_TTL = dt.timedelta(minutes=30)


class LoginError(Exception):
    """Something the operator has to fix (wrong code, expired request, missing step)."""


@dataclass(frozen=True, slots=True)
class LoginState:
    phone: str
    phone_code_hash: str
    created_at: dt.datetime

    def expired(self, now: dt.datetime) -> bool:
        return now - self.created_at > STATE_TTL


def state_path(data_dir: Path) -> Path:
    return data_dir / STATE_NAME


def save_state(data_dir: Path, state: LoginState) -> None:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "phone": state.phone,
                "phone_code_hash": state.phone_code_hash,
                "created_at": state.created_at.isoformat(),
            }
        )
    )
    path.chmod(0o600)


def load_state(data_dir: Path) -> LoginState | None:
    try:
        raw = json.loads(state_path(data_dir).read_text())
        created = dt.datetime.fromisoformat(raw["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.UTC)
        return LoginState(raw["phone"], raw["phone_code_hash"], created)
    except (OSError, ValueError, KeyError):
        return None


def clear_state(data_dir: Path) -> None:
    state_path(data_dir).unlink(missing_ok=True)


async def request_code(client: Any, data_dir: Path, phone: str, now: dt.datetime) -> str:
    """Ask Telegram to send the login code and remember the hash for the next step."""
    sent = await client.send_code_request(phone)
    save_state(data_dir, LoginState(phone, sent.phone_code_hash, now))
    return str(getattr(getattr(sent, "type", None), "__class__", type(sent)).__name__)


async def sign_in_with_code(client: Any, data_dir: Path, code: str, now: dt.datetime) -> Any:
    """Second step. Raises :class:`LoginError` with an actionable message."""
    state = load_state(data_dir)
    if state is None:
        msg = "No pending login. Start with: ddiver login --phone +380…"
        raise LoginError(msg)
    if state.expired(now):
        clear_state(data_dir)
        msg = "That login request expired. Start again with: ddiver login --phone +380…"
        raise LoginError(msg)
    try:
        user = await client.sign_in(
            phone=state.phone, code=code.strip(), phone_code_hash=state.phone_code_hash
        )
    except errors.SessionPasswordNeededError:
        msg = "This account has 2FA. Finish with: ddiver login --password '<your password>'"
        raise LoginError(msg) from None
    except errors.PhoneCodeInvalidError:
        msg = "Wrong code. Check the message from Telegram and try again."
        raise LoginError(msg) from None
    except errors.PhoneCodeExpiredError:
        clear_state(data_dir)
        msg = "That code expired. Request a new one: ddiver login --phone +380…"
        raise LoginError(msg) from None
    clear_state(data_dir)
    return user


async def sign_in_with_password(client: Any, data_dir: Path, password: str) -> Any:
    try:
        user = await client.sign_in(password=password)
    except errors.PasswordHashInvalidError:
        msg = "Wrong 2FA password."
        raise LoginError(msg) from None
    clear_state(data_dir)
    return user
