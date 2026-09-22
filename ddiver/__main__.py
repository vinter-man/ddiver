"""Command line: ``ddiver login | run | health | check-config``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import stat
import sys
from pathlib import Path
from typing import Any

from ddiver.app import (
    EXIT_DB_CORRUPT,
    EXIT_ERROR,
    EXIT_LOCKED,
    EXIT_NOT_AUTHORIZED,
    EXIT_OK,
)
from ddiver.config import Settings
from ddiver.health import MAX_AGE_SECONDS, heartbeat_age, is_healthy
from ddiver.storage import DatabaseCorruptError, Storage

log = logging.getLogger("ddiver")

LOCK_NAME = "ddiver.lock"
LOGIN_BLOCK_SECONDS = 120


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    # Telethon logs its automatic flood sleeps here at INFO; without this they are invisible.
    logging.getLogger("telethon.client.users").setLevel(logging.INFO)


def harden_data_dir(data_dir: Path) -> None:
    """The session file is the account: keep the data directory to the owner only."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        data_dir.chmod(0o700)
    for path in data_dir.glob("*"):
        if path.is_file() and path.suffix in {".session", ".sqlite3", ".sqlite", ".env"}:
            with contextlib.suppress(OSError):
                path.chmod(0o600)


def world_readable(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IRGRP | stat.S_IROTH))


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


async def cmd_login(settings: Settings) -> int:
    from ddiver.clients import make_user_client
    from ddiver.runtime import refresh_dialogs

    harden_data_dir(settings.data_dir)
    age = heartbeat_age(settings.heartbeat_path)
    if age is not None and age < LOGIN_BLOCK_SECONDS:
        print("ddiver looks like it is running. Stop it first: two processes on one session")
        print("file make Telegram invalidate it.")
        return EXIT_LOCKED
    lock = InstanceLock(settings.data_dir / LOCK_NAME)
    if not lock.acquire():
        print("Another ddiver process holds the lock; stop it first.")
        return EXIT_LOCKED
    client = make_user_client(settings)
    try:
        await client.start()  # interactive: phone, code, 2FA password
        me = await client.get_me()
        print(f"Logged in as {me.first_name} (id {me.id}).")
        if me.id != settings.owner_id:
            print(f"⚠️  OWNER_ID is {settings.owner_id} but this account is {me.id}.")
        storage = Storage(settings.db_path, owner_id=settings.owner_id)
        await storage.open()
        try:
            count = await refresh_dialogs(client, storage)
            print(f"Cached {count} chats.")
        finally:
            await storage.close()
    finally:
        await client.disconnect()
        lock.release()
        harden_data_dir(settings.data_dir)
    return EXIT_OK


async def cmd_run(settings: Settings) -> int:
    from ddiver.runtime import Application

    harden_data_dir(settings.data_dir)
    lock = InstanceLock(settings.data_dir / LOCK_NAME)
    if not lock.acquire():
        log.error("another ddiver process is already running for %s", settings.data_dir)
        return EXIT_LOCKED

    app = Application(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, app.request_stop)
    try:
        await app.setup()
    except DatabaseCorruptError as exc:
        log.error("database is corrupt (%s); restore %s from a backup", exc, settings.db_path)
        lock.release()
        return EXIT_DB_CORRUPT
    try:
        return await app.run()
    finally:
        await app.close()
        lock.release()


async def cmd_check_config(settings: Settings) -> int:
    from ddiver.clients import make_bot_client

    problems: list[str] = []
    print(f"Data directory: {settings.data_dir}")
    print(f"Owner id: {settings.owner_id}")
    print(f"Bot id: {settings.bot_id}")
    if not settings.user_session.exists():
        problems.append("no user session yet — run `ddiver login`")
    problems.extend(
        f"{path} is readable by other users; run `chmod 600 {path}`"
        for path in (settings.data_dir, settings.user_session, settings.db_path, Path(".env"))
        if path.exists() and world_readable(path)
    )

    client = make_bot_client(settings)
    try:
        await client.start(bot_token=settings.bot_token.get_secret_value())
        me = await client.get_me()
        if me.id != settings.bot_id:
            problems.append(
                f"bot.session belongs to bot {me.id}, but BOT_TOKEN is for {settings.bot_id}; "
                f"delete {settings.bot_session}"
            )
        else:
            print(f"Bot: @{me.username}")
        try:
            await client.get_input_entity(settings.owner_id)
        except Exception:  # Telethon raises ValueError or an RPCError here
            problems.append(
                f"the owner has not started the bot yet — open t.me/{me.username} and press Start"
            )
    except Exception as exc:  # this command reports problems, it never raises
        problems.append(f"cannot use BOT_TOKEN: {type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()

    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  ✖ {problem}")
        return EXIT_ERROR
    print("\nEverything looks fine.")
    return EXIT_OK


def cmd_health(settings: Settings) -> int:
    age = heartbeat_age(settings.heartbeat_path)
    if age is None:
        print("no heartbeat file")
        return EXIT_ERROR
    if not is_healthy(settings.heartbeat_path, MAX_AGE_SECONDS):
        print(f"heartbeat is {age:.0f}s old")
        return EXIT_ERROR
    print(f"ok ({age:.0f}s)")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ddiver", description=__doc__)
    parser.add_argument("--env-file", default=".env", help="path to the .env file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="log the listener account in (interactive)")
    sub.add_parser("run", help="run the monitor")
    sub.add_parser("health", help="check the heartbeat (for Docker HEALTHCHECK)")
    sub.add_parser("check-config", help="validate configuration and permissions")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # before anything creates a session, a database or a heartbeat
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.load(args.env_file)
    except Exception as exc:  # this command reports problems, it never raises
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    configure_logging(settings.log_level)

    if args.command == "health":
        return cmd_health(settings)
    if args.command == "login":
        return asyncio.run(cmd_login(settings))
    if args.command == "check-config":
        return asyncio.run(cmd_check_config(settings))
    code = asyncio.run(cmd_run(settings))
    if code == EXIT_NOT_AUTHORIZED:
        log.error("run `ddiver login` first")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
