"""Runtime settings, loaded from environment variables / ``.env``."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    api_id: int = Field(description="my.telegram.org → API development tools")
    api_hash: SecretStr
    bot_token: SecretStr = Field(description="@BotFather token of the delivery/config bot")
    owner_id: int = Field(description="Telegram user id of the owner (auto-allowed)")

    data_dir: Path = Field(default=Path("data"), description="session files, SQLite, heartbeat")
    log_level: str = "INFO"

    catchup_hours: int = Field(default=24, ge=0, le=24 * 14)
    catchup_max_per_chat: int = Field(default=500, ge=0, le=5000)
    digest_threshold: int = Field(default=20, ge=2, le=500)
    regex_timeout_ms: int = Field(default=20, ge=5, le=5000)
    ignore_own_messages: bool = Field(default=True, description="set false only for tests")

    @field_validator("bot_token")
    @classmethod
    def _bot_token_shape(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        digits, sep, rest = raw.partition(":")
        if not sep or not digits.isdigit() or len(rest) < 20:
            msg = "BOT_TOKEN must look like 123456789:AA... (from @BotFather)"
            raise ValueError(msg)
        return value

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            msg = "LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR"
            raise ValueError(msg)
        return upper

    @classmethod
    def load(cls, env_file: Path | str | None = ".env") -> Settings:
        """Build settings; ``env_file=None`` reads the process environment only (tests)."""
        return cls(_env_file=env_file)  # type: ignore[call-arg]

    @property
    def user_session(self) -> Path:
        return self.data_dir / "user.session"

    @property
    def bot_session(self) -> Path:
        return self.data_dir / "bot.session"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ddiver.sqlite3"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"

    @property
    def regex_timeout(self) -> float:
        return self.regex_timeout_ms / 1000

    @property
    def bot_id(self) -> int:
        return int(self.bot_token.get_secret_value().partition(":")[0])
