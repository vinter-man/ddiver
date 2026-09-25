"""ddiver's settings: the core's, plus the user session and catch-up.

Everything the bot, delivery and matching need is ``ddiver_core.config.Settings``; this subclass
adds what only an app with a Telegram user session has. Environment variable names are the field
names, as they were before the split.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from ddiver_core.config import Settings as CoreSettings


class Settings(CoreSettings):
    catchup_hours: int = Field(default=24, ge=0, le=24 * 14)
    catchup_max_per_chat: int = Field(default=500, ge=0, le=5000)
    ignore_own_messages: bool = Field(default=True, description="set false only for tests")

    @property
    def user_session(self) -> Path:
        return self.data_dir / "user.session"
