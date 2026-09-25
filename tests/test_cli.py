import asyncio
import datetime as dt
import os
import stat

import pytest

from ddiver.__main__ import (
    InstanceLock,
    build_parser,
    cmd_check_config,
    cmd_health,
    describe_config_error,
    harden_data_dir,
    main,
    permission_hint,
    world_readable,
)
from ddiver.config import Settings
from ddiver_core.app import EXIT_ERROR, EXIT_OK
from ddiver_core.health import write_heartbeat
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


@pytest.fixture
def loose_umask():
    """Run with a permissive umask, so the assertions test the code and not the environment.

    `main()` sets `os.umask(0o077)` and never restores it, so any test that has called main()
    leaves the whole pytest process at 077 — under which every file is created 0600 anyway
    and these assertions pass whatever `harden_data_dir` does. That is how they survived
    being mutated out.
    """
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.usefixtures("loose_umask")
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
    def test_check_config_hardens_the_data_dir_to_700(self, tmp_path, monkeypatch, capsys):
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
        capsys.readouterr()
        # hardening runs first, so the directory is no longer world readable
        assert stat.S_IMODE(data.stat().st_mode) == 0o700

    def test_the_hint_asks_for_700_on_a_directory_and_600_on_a_file(self, tmp_path):
        """600 on a directory would take away the execute bit ddiver needs to open its files."""
        session = tmp_path / "user.session"
        session.write_text("x")
        assert "chmod 700" in permission_hint(tmp_path)
        assert "chmod 600" in permission_hint(session)
        assert str(session) in permission_hint(session)


class TestConfigErrorsAreRedacted:
    """The one control keeping a token out of stderr, the container log and Loki."""

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("BOT_TOKEN", "SENTINELTOKEN-not-a-valid-shape"),
            ("API_ID", "SENTINELAPIID"),
            ("OWNER_ID", "SENTINELOWNER"),
            ("PING_URL", "SENTINELPING-not-a-url"),
        ],
    )
    def test_the_bad_value_is_never_printed(self, tmp_path, capsys, monkeypatch, key, value):
        env = {**ENV, key: value}
        path = tmp_path / ".env"
        path.write_text("\n".join(f"{k}={v}" for k, v in env.items()), encoding="utf-8")
        for name in env:
            monkeypatch.delenv(name, raising=False)

        assert main(["--env-file", str(path), "health"]) == EXIT_ERROR
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "SENTINEL" not in combined, combined
        assert key in combined  # it still says which setting is wrong
        assert "Configuration error" in combined

    def test_a_pydantic_error_really_would_have_leaked(self):
        """Pin the reason this function exists, so nobody simplifies it back to str(exc)."""
        from ddiver.config import Settings

        with pytest.raises(Exception) as caught:
            Settings(
                api_id=1,
                api_hash="0" * 32,
                bot_token="SENTINELTOKEN-not-a-valid-shape",
                owner_id=1,
            )
        assert "SENTINELTOKEN" in str(caught.value)
        assert "SENTINELTOKEN" not in describe_config_error(caught.value)
