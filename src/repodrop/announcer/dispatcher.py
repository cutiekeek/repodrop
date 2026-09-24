"""Outbox dispatcher: claim pending deliveries, post them to Discord, record the result."""

import asyncio
from datetime import timedelta

import aiohttp
import discord
import logfire

from repodrop import observability
from repodrop.announcer.embeds import build_embed
from repodrop.config import Settings
from repodrop.db import queries
from repodrop.db.queries import ClaimedDelivery
from repodrop.db.session import SessionFactory

# Claimed deliveries are hidden from other claims for this long; a crash mid-send retries after it.
CLAIM_LEASE = timedelta(minutes=5)
RETRY_BASE = timedelta(seconds=30)
RETRY_CAP = timedelta(hours=1)


def retry_delay(attempts: int) -> timedelta:
    return min(RETRY_BASE * 2 ** (attempts - 1), RETRY_CAP)


class Dispatcher:
    def __init__(
        self,
        settings: Settings,
        sessions: SessionFactory,
        client: discord.Client,
        wake: asyncio.Event,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._client = client
        self._wake = wake

    async def run(self) -> None:
        await self._client.wait_until_ready()
        sweep = self._settings.announcer_sweep_interval.total_seconds()
        while True:
            # Clear before draining so a wake-up that arrives mid-drain isn't lost.
            self._wake.clear()
            try:
                await self.drain()
            except Exception:
                logfire.exception("announcer drain failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=sweep)
            except TimeoutError:
                pass

    async def drain(self) -> None:
        while True:
            async with self._sessions.begin() as session:
                batch = await queries.claim_deliveries(
                    session, limit=self._settings.announcer_batch_size, lease=CLAIM_LEASE
                )
            for delivery in batch:
                await self._deliver(delivery)
            if len(batch) < self._settings.announcer_batch_size:
                break
        async with self._sessions() as session:
            observability.pending_deliveries.set(await queries.pending_delivery_count(session))

    async def _deliver(self, d: ClaimedDelivery) -> None:
        with logfire.span(
            "deliver",
            delivery_id=d.id,
            guild_id=d.guild_id,
            channel_id=d.channel_id,
            event_kind=d.event_kind,
            attempt=d.attempts,
        ) as span:
            outcome = await self._send(d)
            span.set_attribute("outcome", outcome)

    async def _send(self, d: ClaimedDelivery) -> str:
        if not d.subscription_active:
            await self._skip(d, "subscription inactive", deactivate=False)
            return "skipped"

        try:
            channel = self._client.get_channel(d.channel_id) or await self._client.fetch_channel(
                d.channel_id
            )
            if not isinstance(channel, discord.abc.Messageable):
                await self._skip(d, "channel is not messageable", deactivate=True)
                return "skipped"
            embed = build_embed(d.event_kind, d.payload)
            message = await channel.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
        except (discord.Forbidden, discord.NotFound) as exc:
            # Channel deleted, or the bot lost access: stop delivering to this subscription.
            reason = "forbidden" if isinstance(exc, discord.Forbidden) else "not_found"
            observability.delivery_failures.add(1, {"reason": reason})
            await self._skip(d, f"{reason}: {exc.text}", deactivate=True)
            return "skipped"
        except discord.HTTPException as exc:
            if 400 <= exc.status < 500 and exc.status != 429:
                # Malformed request; retrying won't help.
                observability.delivery_failures.add(1, {"reason": f"http_{exc.status}"})
                await self._fail(d, f"HTTP {exc.status}: {exc.text}")
                return "failed"
            await self._retry_or_fail(
                d, f"HTTP {exc.status}: {exc.text}", reason=f"http_{exc.status}"
            )
            return "retry"
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            await self._retry_or_fail(d, f"{type(exc).__name__}: {exc}", reason="network")
            return "retry"
        except (KeyError, ValueError, TypeError) as exc:
            # Bad payload; retrying won't help.
            logfire.exception("could not build embed for delivery {id}", id=d.id)
            observability.delivery_failures.add(1, {"reason": "bad_payload"})
            await self._fail(d, f"bad payload: {exc!r}")
            return "failed"

        async with self._sessions.begin() as session:
            await queries.mark_delivery_sent(session, d.id, message.id)
        return "sent"

    async def _skip(self, d: ClaimedDelivery, reason: str, *, deactivate: bool) -> None:
        async with self._sessions.begin() as session:
            await queries.mark_delivery_skipped(session, d.id, reason)
            if deactivate:
                await queries.deactivate_subscription(session, d.subscription_id)
        if deactivate:
            logfire.warn(
                "deactivated subscription {subscription_id}: {reason}",
                subscription_id=d.subscription_id,
                reason=reason,
            )

    async def _fail(self, d: ClaimedDelivery, error: str) -> None:
        async with self._sessions.begin() as session:
            await queries.mark_delivery_failed(session, d.id, error)

    async def _retry_or_fail(self, d: ClaimedDelivery, error: str, *, reason: str) -> None:
        observability.delivery_failures.add(1, {"reason": reason})
        if d.attempts >= self._settings.delivery_max_attempts:
            await self._fail(d, error)
            return
        async with self._sessions.begin() as session:
            await queries.schedule_delivery_retry(
                session, d.id, error=error, delay=retry_delay(d.attempts)
            )
