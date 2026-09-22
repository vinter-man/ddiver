import asyncio
import datetime as dt
import stat

import pytest

from ddiver.__main__ import (
    InstanceLock,
    build_parser,
    cmd_check_config,
    cmd_health,
    harden_data_dir,
    main,
    world_readable,
)
from ddiver.app import EXIT_ERROR, EXIT_OK
from ddiver.config import Settings
from ddiver.health import write_heartbeat
from tests.fakes import T0

ENV = {
    "API_ID": "12345",
    "API_HASH": "0123456789abcdef0123456789abcdef",
    "BOT_TOKEN": "123456789:AAExampleTokenWithEnoughLength_xyz",
    "OWNER_ID": "777",
}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key, value in {**ENV, "DATA_DIR": str(tmp_path / "data")}.items():
        monkeypatch.setenv(key, value)
    return Settings.load(None)


class TestParser:
    def test_commands(self):
        parser = build_parser()
        assert parser.parse_args(["run"]).command == "run"
        assert parser.parse_args(["--env-file", "x.env", "login"]).env_file == "x.env"

    def test_command_is_required(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])


class TestHealth:
    def test_missing_heartbeat(self, settings, capsys):
        assert cmd_health(settings) == EXIT_ERROR
        assert "no heartbeat" in capsys.readouterr().out

    def test_fresh_heartbeat(self, settings, capsys):
        write_heartbeat(settings.heartbeat_path)
        assert cmd_health(settings) == EXIT_OK
        assert "ok" in capsys.readouterr().out

    def test_stale_heartbeat(self, settings, capsys):
        write_heartbeat(settings.heartbeat_path, T0 - dt.timedelta(hours=1))
        assert cmd_health(settings) == EXIT_ERROR
        assert "old" in capsys.readouterr().out

    def test_health_via_main(self, tmp_path, monkeypatch, capsys):
        for key, value in {**ENV, "DATA_DIR": str(tmp_path)}.items():
            monkeypatch.setenv(key, value)
        write_heartbeat(tmp_path / "heartbeat")
        assert main(["--env-file", "", "health"]) == EXIT_OK

    def test_bad_configuration_is_reported(self, tmp_path, monkeypatch, capsys):
        for key in ENV:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("DDIVER_UNUSED", "1")
        assert main(["--env-file", "", "health"]) == EXIT_ERROR
        assert "Configuration error" in capsys.readouterr().err


class TestPermissions:
    def test_harden_sets_restrictive_modes(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        session = data / "user.session"
        session.write_text("secret")
        session.chmod(0o644)
        harden_data_dir(data)
        assert stat.S_IMODE(data.stat().st_mode) == 0o700
        assert stat.S_IMODE(session.stat().st_mode) == 0o600
        assert not world_readable(session)

    def test_world_readable_detection(self, tmp_path):
        path = tmp_path / "f"
        path.write_text("x")
        path.chmod(0o644)
        assert world_readable(path)
        path.chmod(0o600)
        assert not world_readable(path)
        assert not world_readable(tmp_path / "missing")


class TestInstanceLock:
    def test_second_acquire_fails_while_held(self, tmp_path):
        first = InstanceLock(tmp_path / "ddiver.lock")
        second = InstanceLock(tmp_path / "ddiver.lock")
        assert first.acquire() is True
        assert second.acquire() is False
        first.release()
        assert second.acquire() is True
        second.release()

    def test_release_is_idempotent(self, tmp_path):
        lock = InstanceLock(tmp_path / "ddiver.lock")
        lock.release()
        assert lock.acquire() is True
        lock.release()
        lock.release()


class TestCheckConfigPermissionHints:
    def test_directory_hint_uses_700(self, tmp_path, monkeypatch, capsys):
        """A directory needs the execute bit, so `chmod 600` on it would break ddiver."""
        data = tmp_path / "data"
        data.mkdir(mode=0o755)
        (data / "user.session").write_text("x")
        for key, value in {**ENV, "DATA_DIR": str(data)}.items():
            monkeypatch.setenv(key, value)
        settings = Settings.load(None)

        class DeadClient:
            async def start(self, **_kwargs):
                raise ConnectionError("offline")

            async def disconnect(self):
                return None

        monkeypatch.setattr("ddiver.clients.make_bot_client", lambda *a, **k: DeadClient())
        assert asyncio.run(cmd_check_config(settings)) == EXIT_ERROR
        out = capsys.readouterr().out
        assert "chmod 600" not in out or "chmod 700" in out
        # hardening runs first, so the directory is no longer world readable
        assert stat.S_IMODE(data.stat().st_mode) == 0o700
