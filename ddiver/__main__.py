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
from ddiver.health import check as health_check
from ddiver.health import heartbeat_age
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


def permission_hint(path: Path) -> str:
    """A directory needs its execute bit, so 600 on one would lock ddiver out of its own data."""
    mode = 700 if path.is_dir() else 600
    return f"{path} is readable by other users; run `chmod {mode} {path}`"


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


def read_password(args: argparse.Namespace) -> str | None:
    """The 2FA password, from the flag, a file, or the environment.

    A file or an environment variable keeps it out of shell history — which matters here,
    because this password is the last thing standing between a leaked session and the account.
    """
    if getattr(args, "password_file", None):
        return Path(args.password_file).read_text().strip()
    return args.password or os.environ.get("DDIVER_LOGIN_PASSWORD") or None


async def cmd_login(settings: Settings, args: argparse.Namespace) -> int:
    from telethon.errors import RPCError

    from ddiver.clients import make_user_client
    from ddiver.login import (
        LoginError,
        clear_state,
        request_code,
        sign_in_with_code,
        sign_in_with_password,
    )
    from ddiver.timeutil import utcnow

    harden_data_dir(settings.data_dir)
    age = heartbeat_age(settings.heartbeat_path)
    if age is not None and age < LOGIN_BLOCK_SECONDS:
        print("ddiver looks like it is running. Stop it first — two processes on one session")
        print("file make Telegram invalidate it.")
        return EXIT_LOCKED
    lock = InstanceLock(settings.data_dir / LOCK_NAME)
    if not lock.acquire():
        print("Another ddiver process holds the lock; stop it first.")
        return EXIT_LOCKED

    client = make_user_client(settings)
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Already logged in as {me.first_name} (id {me.id}).")
            clear_state(settings.data_dir)
            return await _finish_login(client, settings, me)

        if args.phone:
            try:
                await request_code(client, settings.data_dir, args.phone, utcnow())
            except RPCError as exc:
                print(f"Telegram refused the request: {type(exc).__name__}: {exc}")
                return EXIT_ERROR
            print(f"Telegram sent a code to {args.phone}.")
            print("Next: ddiver login --code 12345")
            print("(the code expires in a few minutes; 2FA accounts add --password afterwards)")
            return EXIT_OK

        try:
            password = read_password(args)
        except OSError as exc:
            print(f"Cannot read the password file: {exc}")
            return EXIT_ERROR

        if args.code or password:
            try:
                if args.code:
                    me = await sign_in_with_code(client, settings.data_dir, args.code, utcnow())
                else:
                    me = await sign_in_with_password(client, settings.data_dir, password or "")
            except LoginError as exc:
                print(str(exc))
                return EXIT_ERROR
            except RPCError as exc:
                print(f"Telegram refused the sign-in: {type(exc).__name__}: {exc}")
                return EXIT_ERROR
            print(f"Logged in as {me.first_name} (id {me.id}).")
            return await _finish_login(client, settings, me)

        if not sys.stdin.isatty():
            print("No terminal available, so use the step-by-step flow:")
            print("  ddiver login --phone +380…")
            print("  ddiver login --code 12345")
            print("  ddiver login --password '<2FA password>'   # only if you have 2FA")
            print("  (or --password-file /path/to/file, or DDIVER_LOGIN_PASSWORD=…)")
            return EXIT_ERROR

        await client.start()  # interactive: phone, code, 2FA password
        me = await client.get_me()
        print(f"Logged in as {me.first_name} (id {me.id}).")
        return await _finish_login(client, settings, me)
    finally:
        await client.disconnect()
        lock.release()
        harden_data_dir(settings.data_dir)


async def _finish_login(client: Any, settings: Settings, me: Any) -> int:
    """Warn about a mismatched OWNER_ID and cache the account's chats."""
    from ddiver.runtime import refresh_dialogs

    if me.id != settings.owner_id:
        print(f"⚠️  OWNER_ID is {settings.owner_id} but this account is {me.id}.")
        print("   The bot will answer OWNER_ID, not this account — fix .env if that is wrong.")
    storage = Storage(settings.db_path, owner_id=settings.owner_id)
    await storage.open()
    try:
        count = await refresh_dialogs(client, storage)
        print(f"Cached {count} chats. Now run: ddiver run")
    finally:
        await storage.close()
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
        try:
            await app.setup()
        except DatabaseCorruptError as exc:
            log.error("database is corrupt (%s); restore %s from a backup", exc, settings.db_path)
            return EXIT_DB_CORRUPT
        except Exception:
            # Anything from a read-only /data to a Telethon change: close up rather than leave
            # aiosqlite's non-daemon thread running, which would hang the process for ever and
            # keep the container "up" with no heartbeat.
            log.exception("startup failed")
            return EXIT_ERROR
        return await app.run()
    finally:
        with contextlib.suppress(Exception):
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
    harden_data_dir(settings.data_dir)
    problems.extend(
        permission_hint(path)
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
    report = health_check(settings.heartbeat_path)
    print(report.reason)
    return EXIT_OK if report.healthy else EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ddiver", description=__doc__)
    parser.add_argument("--env-file", default=".env", help="path to the .env file")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="log the listener account in")
    login.add_argument("--phone", help="send the login code to this number, e.g. +380501112233")
    login.add_argument("--code", help="the code Telegram sent you")
    login.add_argument("--password", help="2FA password, if the account has one")
    login.add_argument(
        "--password-file",
        help="read the 2FA password from this file instead, so it stays out of shell history",
    )
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
        return asyncio.run(cmd_login(settings, args))
    if args.command == "check-config":
        return asyncio.run(cmd_check_config(settings))
    code = asyncio.run(cmd_run(settings))
    if code == EXIT_NOT_AUTHORIZED:
        log.error("run `ddiver login` first")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
