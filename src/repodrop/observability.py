"""Logfire setup and custom metrics.

Tokens must never be passed as span attributes; scrubbing is only a backstop.
"""

import logging
from collections.abc import Iterable
from typing import Any

import logfire

from repodrop.config import Settings

rate_limit_remaining = logfire.metric_gauge(
    "github.rate_limit.remaining",
    unit="1",
    description="Remaining GitHub API requests in the current window",
)
pending_deliveries = logfire.metric_gauge(
    "deliveries.pending", unit="1", description="Pending deliveries waiting to be announced"
)
poll_lag = logfire.metric_gauge(
    "poll.lag", unit="s", description="now() - next_poll_at of the oldest due watch"
)
delivery_failures = logfire.metric_counter(
    "deliveries.failures", unit="1", description="Delivery failures by reason"
)


def configure(settings: Settings) -> None:
    logfire.configure(
        service_name="repodrop",
        environment=settings.environment,
        token=settings.logfire_token.get_secret_value() if settings.logfire_token else None,
        send_to_logfire="if-token-present",
    )
    logfire.instrument_httpx()
    logfire.instrument_asyncpg()

    # Route stdlib logging (discord.py, alembic, ...) into Logfire.
    handler = logfire.LogfireLoggingHandler()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)


def audit(message: str, /, *, level: str = "info", **attributes: Any) -> None:
    """Log a change someone made (subscriptions, server settings, operator actions, joins).

    Tagged `audit` so every change can be pulled up with one Logfire filter. Attributes should
    identify who and where: `guild_id`, `channel_id`, `user_id`.
    """
    log = logfire.warn if level == "warn" else logfire.info
    log(message, _tags=["audit"], **attributes)


def option_values(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Slash-command options as loggable values: Discord objects become their IDs."""
    values: dict[str, Any] = {}
    for name, value in pairs:
        if isinstance(value, str | int | float | bool) or value is None:
            values[name] = value
        elif hasattr(value, "id"):
            values[name] = value.id
        else:
            values[name] = str(value)
    return values
