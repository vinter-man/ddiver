import datetime as dt

import pytest
from telethon import errors

from ddiver.login import (
    LoginError,
    LoginState,
    clear_state,
    load_state,
    request_code,
    save_state,
    sign_in_with_code,
    sign_in_with_password,
    state_path,
)
from ddiver_core_testing import T0


class FakeAuthClient:
    """Enough of a Telethon client for the login steps."""

    def __init__(self, *, sign_in_error: BaseException | None = None) -> None:
        self.sent: list[str] = []
        self.sign_ins: list[dict[str, object]] = []
        self.sign_in_error = sign_in_error

    async def send_code_request(self, phone: str):
        self.sent.append(phone)
        return type("SentCode", (), {"phone_code_hash": "hash-123", "type": object()})()

    async def sign_in(self, phone=None, code=None, *, password=None, phone_code_hash=None):
        self.sign_ins.append(
            {"phone": phone, "code": code, "password": password, "hash": phone_code_hash}
        )
        if self.sign_in_error is not None:
            error, self.sign_in_error = self.sign_in_error, None
            raise error
        return type("User", (), {"id": 42, "first_name": "Иван"})()


class TestState:
    def test_roundtrip_and_permissions(self, tmp_path):
        state = LoginState("+380501112233", "hash-123", T0)
        save_state(tmp_path, state)
        assert load_state(tmp_path) == state
        assert state_path(tmp_path).stat().st_mode & 0o777 == 0o600
        clear_state(tmp_path)
        assert load_state(tmp_path) is None
        clear_state(tmp_path)  # idempotent

    def test_garbage_state_is_ignored(self, tmp_path):
        state_path(tmp_path).write_text("not json")
        assert load_state(tmp_path) is None

    def test_expiry(self):
        state = LoginState("+1", "h", T0)
        assert not state.expired(T0 + dt.timedelta(minutes=29))
        assert state.expired(T0 + dt.timedelta(minutes=31))


class TestFlow:
    async def test_request_then_sign_in(self, tmp_path):
        client = FakeAuthClient()
        await request_code(client, tmp_path, "+380501112233", T0)
        assert client.sent == ["+380501112233"]
        assert load_state(tmp_path).phone_code_hash == "hash-123"

        user = await sign_in_with_code(client, tmp_path, " 12345 ", T0)
        assert user.id == 42
        assert client.sign_ins[0] == {
            "phone": "+380501112233",
            "code": "12345",
            "password": None,
            "hash": "hash-123",
        }
        assert load_state(tmp_path) is None  # consumed

    async def test_code_without_a_pending_request(self, tmp_path):
        with pytest.raises(LoginError, match="--phone"):
            await sign_in_with_code(FakeAuthClient(), tmp_path, "12345", T0)

    async def test_expired_request_is_cleared(self, tmp_path):
        save_state(tmp_path, LoginState("+1", "h", T0))
        with pytest.raises(LoginError, match="expired"):
            await sign_in_with_code(FakeAuthClient(), tmp_path, "12345", T0 + dt.timedelta(hours=1))
        assert load_state(tmp_path) is None

    async def test_two_factor_tells_the_next_step(self, tmp_path):
        client = FakeAuthClient(sign_in_error=errors.SessionPasswordNeededError(request=None))
        await request_code(client, tmp_path, "+1", T0)
        with pytest.raises(LoginError, match="--password"):
            await sign_in_with_code(client, tmp_path, "12345", T0)
        # the state survives so the password step can still find the account
        assert load_state(tmp_path) is not None
        user = await sign_in_with_password(client, tmp_path, "secret")
        assert user.id == 42
        assert client.sign_ins[-1]["password"] == "secret"
        assert load_state(tmp_path) is None

    async def test_wrong_code_keeps_the_request(self, tmp_path):
        client = FakeAuthClient(sign_in_error=errors.PhoneCodeInvalidError(request=None))
        await request_code(client, tmp_path, "+1", T0)
        with pytest.raises(LoginError, match="Wrong code"):
            await sign_in_with_code(client, tmp_path, "00000", T0)
        assert load_state(tmp_path) is not None  # retry without a new code request

    async def test_expired_code_clears_the_request(self, tmp_path):
        client = FakeAuthClient(sign_in_error=errors.PhoneCodeExpiredError(request=None))
        await request_code(client, tmp_path, "+1", T0)
        with pytest.raises(LoginError, match="expired"):
            await sign_in_with_code(client, tmp_path, "12345", T0)
        assert load_state(tmp_path) is None

    async def test_wrong_password(self, tmp_path):
        client = FakeAuthClient(sign_in_error=errors.PasswordHashInvalidError(request=None))
        with pytest.raises(LoginError, match="Wrong 2FA password"):
            await sign_in_with_password(client, tmp_path, "nope")
