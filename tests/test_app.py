import asyncio
import datetime as dt
import random
import sqlite3

import pytest
from telethon import errors
from telethon.errors import common as common_errors

from ddiver.app import (
    BOOT_STORM_DELAY,
    EXIT_ERROR,
    EXIT_OK,
    PeriodicTask,
    Supervisor,
    boot_delay,
    format_uptime,
)
from ddiver.clients import is_fatal_auth_error, is_session_file_error
from tests.fakes import T0, FakeClock


class Sleeps:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.tick(seconds)
        await asyncio.sleep(0)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sleeps(clock):
    return Sleeps(clock)


def make_supervisor(runs, clock, sleeps, *, notify=None, max_attempts=5):
    calls = iter(runs)

    async def run_listener():
        outcome = next(calls)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return Supervisor(
        run_listener=run_listener,
        notify_owner=notify,
        clock=clock,
        sleep=sleeps,
        rng=random.Random(1),
        max_attempts=max_attempts,
    )


class TestFatalClassification:
    @pytest.mark.parametrize(
        "exc",
        [
            errors.SessionRevokedError(request=None),
            errors.AuthKeyUnregisteredError(request=None),
            errors.UserDeactivatedBanError(request=None),
            errors.AuthKeyDuplicatedError(request=None),  # two processes on one session
            common_errors.AuthKeyNotFound(),  # not an RPCError at all
            errors.PhoneNumberBannedError(request=None),
        ],
    )
    def test_fatal(self, exc):
        assert is_fatal_auth_error(exc)

    @pytest.mark.parametrize(
        "exc",
        [
            errors.SessionPasswordNeededError(request=None),  # only means "2FA needed"
            errors.FloodWaitError(request=None, capture=5),
            ConnectionError("reset"),
            TimeoutError(),
        ],
    )
    def test_not_fatal(self, exc):
        assert not is_fatal_auth_error(exc)

    def test_session_file_errors(self):
        assert is_session_file_error(sqlite3.OperationalError("database is locked"))
        assert is_session_file_error(sqlite3.DatabaseError("malformed"))
        assert not is_session_file_error(ValueError("nope"))


class TestSupervisor:
    async def test_fatal_auth_parks_in_degraded_mode_without_exiting(self, clock, sleeps):
        notes = []

        async def notify(reason):
            notes.append(reason)

        sup = make_supervisor(
            [errors.SessionRevokedError(request=None)], clock, sleeps, notify=notify
        )
        assert await sup.run() == EXIT_OK  # never a restart loop under `restart: unless-stopped`
        assert sup.state.degraded and sup.state.degraded_reason == "SessionRevokedError"
        assert notes == ["SessionRevokedError"]
        assert sleeps.calls == []  # no backoff, no reconnect attempts

    async def test_owner_reminder_is_rate_limited(self, clock, sleeps):
        notes = []

        async def notify(reason):
            notes.append(reason)

        sup = make_supervisor(
            [errors.AuthKeyDuplicatedError(request=None)], clock, sleeps, notify=notify
        )
        await sup.run()
        assert await sup.notify_owner_if_due() is False
        clock.tick(6 * 3600 + 1)
        assert await sup.notify_owner_if_due() is True
        assert len(notes) == 2

    async def test_notification_failure_does_not_break_degraded_mode(self, clock, sleeps):
        async def notify(_reason):
            raise ConnectionError("bot is down too")

        sup = make_supervisor(
            [errors.SessionRevokedError(request=None)], clock, sleeps, notify=notify
        )
        assert await sup.run() == EXIT_OK
        assert sup.state.degraded

    async def test_session_file_error_is_treated_as_fatal(self, clock, sleeps):
        sup = make_supervisor([sqlite3.OperationalError("database is locked")], clock, sleeps)
        assert await sup.run() == EXIT_OK
        assert sup.state.degraded
        assert "session file" in sup.state.degraded_reason

    async def test_transient_errors_back_off_with_jitter(self, clock, sleeps):
        sup = make_supervisor(
            [ConnectionError("a"), ConnectionError("b"), ConnectionError("c")],
            clock,
            sleeps,
            max_attempts=3,
        )
        assert await sup.run() == EXIT_ERROR
        assert len(sleeps.calls) == 2
        assert 2.0 <= sleeps.calls[0] < 2.5  # base 2s plus up to 25% jitter
        assert 4.0 <= sleeps.calls[1] < 5.0
        assert sup.state.attempts == 3

    async def test_backoff_is_capped(self, clock, sleeps):
        sup = make_supervisor([ConnectionError("x")] * 20, clock, sleeps, max_attempts=20)
        await sup.run()
        assert max(sleeps.calls) <= 300 * 1.25

    async def test_clean_return_reconnects(self, clock, sleeps):
        sup = make_supervisor([None, None, None], clock, sleeps, max_attempts=3)
        assert await sup.run() == EXIT_ERROR
        assert sup.state.attempts == 3

    async def test_stop_request_ends_the_loop(self, clock, sleeps):
        sup = None

        async def run_listener():
            assert sup is not None
            sup.request_stop()

        sup = Supervisor(run_listener=run_listener, clock=clock, sleep=sleeps)
        assert await sup.run() == EXIT_OK
        assert sup.stopped
        assert sleeps.calls == []

    async def test_cancellation_propagates(self, clock, sleeps):
        async def run_listener():
            raise asyncio.CancelledError

        sup = Supervisor(run_listener=run_listener, clock=clock, sleep=sleeps)
        with pytest.raises(asyncio.CancelledError):
            await sup.run()


class TestBootDelay:
    async def test_quiet_start_is_not_delayed(self, sleeps):
        assert await boot_delay(1, sleeps) == 0.0
        assert sleeps.calls == []

    async def test_crash_loop_is_slowed_down(self, sleeps):
        assert await boot_delay(3, sleeps) == BOOT_STORM_DELAY
        assert sleeps.calls == [BOOT_STORM_DELAY]


class TestUptime:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "0m"), (90, "1m"), (3700, "1h 1m"), (200000, "2d 7h"), (-5, "0m")],
    )
    def test_format(self, seconds, expected):
        assert format_uptime(T0, T0 + dt.timedelta(seconds=seconds)) == expected

    def test_not_started(self):
        assert format_uptime(None, T0) == "—"


class TestPeriodicTask:
    async def test_runs_and_survives_failures(self):
        calls = []
        sleeps = []

        async def work():
            calls.append(len(calls))
            if len(calls) == 2:
                raise RuntimeError("boom")

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 4:
                raise asyncio.CancelledError
            await asyncio.sleep(0)

        task = PeriodicTask("test", work, 5.0, sleep=sleep)
        task.start()
        await asyncio.sleep(0.05)
        await task.stop()
        assert len(calls) >= 3  # kept going after the exception
        assert task.failures == 1
        assert task.runs >= 2
        assert sleeps[:2] == [5.0, 5.0]

    async def test_stop_is_idempotent(self):
        async def work():
            await asyncio.sleep(0)

        task = PeriodicTask("test", work, 0.01)
        await task.stop()
        task.start()
        await task.stop()
        await task.stop()
