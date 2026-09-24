"""Logfire setup and custom metrics.

Tokens must never be passed as span attributes; scrubbing is only a backstop.
"""

import logging

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
