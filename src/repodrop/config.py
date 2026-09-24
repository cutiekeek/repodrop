import json
from datetime import timedelta
from functools import lru_cache
from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Just the database URL, so Alembic can run without the bot's secrets."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore"
    )

    database_url: str = "postgresql+asyncpg://repodrop:repodrop@localhost:5432/repodrop"

    @field_validator("database_url")
    @classmethod
    def _use_asyncpg(cls, v: str) -> str:
        # Accept the common `postgres://` / `postgresql://` forms from hosting providers.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v.removeprefix(prefix)
        return v


class Settings(DatabaseSettings):
    discord_token: SecretStr
    github_token: SecretStr
    logfire_token: SecretStr | None = None
    environment: str = "development"

    # If set, slash commands are synced to this guild only (instant updates while developing).
    dev_guild_id: int | None = None

    # Default per-server caps; the operator can override them per server (guild_settings).
    max_repos_per_guild: int = 25  # distinct repos
    max_subs_per_guild: int = 100  # (channel, repo) pairs
    max_branches_per_sub: int = 5  # commit branches per subscription

    # Discord user IDs allowed to run owner commands: "123,456" or a JSON list.
    owner_ids: Annotated[list[int], NoDecode] = []
    # Optional link shown in the notice a blocked server sees.
    block_contact_url: str | None = None

    # Effective per-server settings are cached this long; writes through the bot invalidate
    # immediately, so this only bounds how long a direct database edit takes to be seen.
    guild_settings_cache_ttl: timedelta = timedelta(seconds=60)

    # Durations accept seconds ("300") or ISO 8601 ("PT5M").
    poll_min_interval: timedelta = timedelta(minutes=5)
    poll_default_interval: timedelta = timedelta(minutes=10)
    poll_max_interval: timedelta = timedelta(minutes=60)
    poll_concurrency: int = 8
    poll_batch_size: int = 50
    poll_tick: timedelta = timedelta(seconds=15)

    # Below this many remaining GitHub requests, polls are pushed past the rate-limit reset.
    github_rate_limit_floor: int = 500

    announcer_batch_size: int = 50
    announcer_sweep_interval: timedelta = timedelta(seconds=30)
    delivery_max_attempts: int = 5

    maintenance_interval: timedelta = timedelta(hours=1)
    # How often each repo's metadata (name, default branch) is re-checked.
    metadata_refresh_interval: timedelta = timedelta(days=1)

    @field_validator(
        "poll_min_interval",
        "poll_default_interval",
        "poll_max_interval",
        "poll_tick",
        "announcer_sweep_interval",
        "maintenance_interval",
        "guild_settings_cache_ttl",
        "metadata_refresh_interval",
        mode="before",
    )
    @classmethod
    def _seconds(cls, v: object) -> object:
        # Env vars arrive as strings, and pydantic only treats numbers (not "300") as seconds.
        if isinstance(v, str) and v.strip().replace(".", "", 1).isdigit():
            return float(v)
        return v

    @field_validator("owner_ids", mode="before")
    @classmethod
    def _id_list(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [part.strip() for part in v.split(",") if part.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # populated from env
