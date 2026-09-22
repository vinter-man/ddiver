from pathlib import Path

import pytest
from pydantic import ValidationError

from ddiver.config import Settings

BASE = {
    "API_ID": "12345",
    "API_HASH": "0123456789abcdef0123456789abcdef",
    "BOT_TOKEN": "123456789:AAExampleTokenWithEnoughLength_xyz",
    "OWNER_ID": "777",
}


def make(monkeypatch, **overrides):
    for key, value in {**BASE, **overrides}.items():
        monkeypatch.setenv(key, value)
    return Settings.load(None)


def test_defaults_and_paths(monkeypatch):
    s = make(monkeypatch, DATA_DIR="/srv/dd")
    assert s.api_id == 12345
    assert s.owner_id == 777
    assert s.bot_id == 123456789
    assert s.user_session == Path("/srv/dd/user.session")
    assert s.db_path == Path("/srv/dd/ddiver.sqlite3")
    assert s.catchup_hours == 24
    assert s.regex_timeout == pytest.approx(0.02)
    assert s.ignore_own_messages is True
    assert "AAExample" not in repr(s)  # secrets are masked


def test_bad_bot_token(monkeypatch):
    with pytest.raises(ValidationError):
        make(monkeypatch, BOT_TOKEN="not-a-token")


def test_log_level_validation(monkeypatch):
    assert make(monkeypatch, LOG_LEVEL="debug").log_level == "DEBUG"
    with pytest.raises(ValidationError):
        make(monkeypatch, LOG_LEVEL="verbose")


def test_bounds(monkeypatch):
    with pytest.raises(ValidationError):
        make(monkeypatch, DIGEST_THRESHOLD="1")
    with pytest.raises(ValidationError):
        make(monkeypatch, REGEX_TIMEOUT_MS="1")


def test_missing_required(monkeypatch):
    for key in BASE:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Settings.load(None)
