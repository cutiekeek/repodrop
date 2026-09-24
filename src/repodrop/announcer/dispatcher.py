"""Outbox dispatcher: claim pending deliveries, post them to Discord, record the result."""

import asyncio
from datetime import timedelta

import aiohttp
import discord
import logfire

from repodrop import observability
from repodrop.announcer.embeds import DEFAULT_PRESENTATION, Presentation, build_message
from repodrop.config import Settings
from repodrop.db import queries
from repodrop.db.queries import ClaimedDelivery
from repodrop.db.session import SessionFactory
from repodrop.guild_settings import GuildSettingsCache

# Claimed deliveries are hidden from other claims for this long; a crash mid-send retries after it.
CLAIM_LEASE = timedelta(minutes=5)
RETRY_BASE = timedelta(seconds=30)
RETRY_CAP = timedelta(hours=1)
MESSAGE_LIMIT = 2000

LOST_PERMISSION = "I lost permission to post in the channel"
CHANNEL_DELETED = "the channel was deleted"
NOT_MESSAGEABLE = "the channel can't receive messages"


def retry_delay(attempts: int) -> timedelta:
    return min(RETRY_BASE * 2 ** (attempts - 1), RETRY_CAP)


class Dispatcher:
    def __init__(
        self,
        settings: Settings,
        sessions: SessionFactory,
        client: discord.Client,
        wake: asyncio.Event,
        guild_settings: GuildSettingsCache | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._client = client
        self._wake = wake
        self._guild_settings = guild_settings

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
                await self.send_notices()
            except Exception:
                logfire.exception("sending admin notices failed")
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
            await self._skip(d, "subscription inactive")
            return "skipped"

        try:
            channel = self._client.get_channel(d.channel_id) or await self._client.fetch_channel(
                d.channel_id
            )
            if not isinstance(channel, discord.abc.Messageable):
                await self._skip(d, NOT_MESSAGEABLE, disable_reason=NOT_MESSAGEABLE)
                return "skipped"
            embed, view = build_message(
                d.event_kind, d.payload, await self._presentation(d.guild_id)
            )
            message = await channel.send(
                embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except (discord.Forbidden, discord.NotFound) as exc:
            # Channel deleted, or the bot lost access: stop delivering to this subscription.
            forbidden = isinstance(exc, discord.Forbidden)
            reason = "forbidden" if forbidden else "not_found"
            observability.delivery_failures.add(1, {"reason": reason})
            await self._skip(
                d,
                f"{reason}: {exc.text}",
                disable_reason=LOST_PERMISSION if forbidden else CHANNEL_DELETED,
            )
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

    async def _presentation(self, guild_id: int) -> Presentation:
        if self._guild_settings is None:
            return DEFAULT_PRESENTATION
        settings = await self._guild_settings.get(guild_id)
        return Presentation(settings.embed_style)

    async def _skip(
        self, d: ClaimedDelivery, error: str, *, disable_reason: str | None = None
    ) -> None:
        """Skip the delivery; with `disable_reason`, also auto-disable its subscription."""
        async with self._sessions.begin() as session:
            await queries.mark_delivery_skipped(session, d.id, error)
            if disable_reason:
                await queries.deactivate_subscription(session, d.subscription_id, disable_reason)
        if disable_reason:
            logfire.warn(
                "disabled subscription {subscription_id}: {reason}",
                subscription_id=d.subscription_id,
                guild_id=d.guild_id,
                channel_id=d.channel_id,
                reason=disable_reason,
                error=error,
            )

    async def _fail(self, d: ClaimedDelivery, error: str) -> None:
        async with self._sessions.begin() as session:
            await queries.mark_delivery_failed(session, d.id, error)
        logfire.warn(
            "delivery {delivery_id} failed permanently after {attempts} attempts: {error}",
            delivery_id=d.id,
            attempts=d.attempts,
            error=error,
            subscription_id=d.subscription_id,
            guild_id=d.guild_id,
            channel_id=d.channel_id,
            event_kind=d.event_kind,
        )

    async def _retry_or_fail(self, d: ClaimedDelivery, error: str, *, reason: str) -> None:
        observability.delivery_failures.add(1, {"reason": reason})
        if d.attempts >= self._settings.delivery_max_attempts:
            await self._fail(d, error)
            return
        delay = retry_delay(d.attempts)
        async with self._sessions.begin() as session:
            await queries.schedule_delivery_retry(session, d.id, error=error, delay=delay)
        logfire.info(
            "delivery {delivery_id} will retry in {delay_seconds}s: {error}",
            delivery_id=d.id,
            delay_seconds=int(delay.total_seconds()),
            attempt=d.attempts,
            error=error,
            guild_id=d.guild_id,
            channel_id=d.channel_id,
        )

    # ------------------------------------------------------------------ admin notices

    async def send_notices(self) -> int:
        """Tell servers about auto-disabled subscriptions and deleted branches, once each.

        Notices are built from the database (the poller can't post to Discord), grouped into
        one message per server, and posted in the server's system channel when there is one
        the bot can post in. Either way they're marked sent, so nothing is retried forever;
        `/repodrop status` and `/repodrop list` still show the problem.
        """
        async with self._sessions() as session:
            disables = await queries.pending_disable_notices(session)
            branches = await queries.pending_branch_notices(session)
        if not disables and not branches:
            return 0

        lines: dict[int, list[str]] = {}
        for n in disables:
            lines.setdefault(n.guild_id, []).append(
                f"- **{n.repo_full_name}** in <#{n.channel_id}>: {n.reason}."
            )
        channels: dict[tuple[int, queries.WatchKey], list[int]] = {}
        details: dict[queries.WatchKey, tuple[str, str]] = {}
        for n in branches:
            channels.setdefault((n.guild_id, n.key), []).append(n.channel_id)
            details[n.key] = (n.repo_full_name, n.error)
        for (guild_id, key), channel_ids in channels.items():
            repo, error = details[key]
            where = ", ".join(f"<#{c}>" for c in channel_ids)
            lines.setdefault(guild_id, []).append(
                f"- **{repo}**: {error}, so its commits in {where} aren't being announced."
            )

        for guild_id, guild_lines in lines.items():
            posted = await self._post_notice(guild_id, guild_lines)
            logfire.info(
                "admin notice for guild {guild_id}: {outcome}",
                guild_id=guild_id,
                outcome="posted" if posted else "no usable system channel",
                items=len(guild_lines),
            )

        async with self._sessions.begin() as session:
            await queries.mark_subscriptions_notified(
                session, [n.subscription_id for n in disables]
            )
            await queries.mark_watches_notified(session, [n.key for n in branches])
        return len(lines)

    async def _post_notice(self, guild_id: int, lines: list[str]) -> bool:
        guild = self._client.get_guild(guild_id)
        channel = guild.system_channel if guild is not None else None
        if guild is None or channel is None:
            return False
        have = channel.permissions_for(guild.me)
        if not (have.view_channel and have.send_messages):
            return False
        header = "**RepoDrop** stopped announcing some updates in this server:"
        footer = (
            "Once the cause is fixed, run `/repodrop subscribe` for the repo and channel to "
            "resume. `/repodrop status` has details."
        )
        try:
            for message in _chunk([header, *lines, footer], MESSAGE_LIMIT):
                await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            logfire.warn(
                "couldn't post notice in guild {guild_id}: {error}",
                guild_id=guild_id,
                error=str(exc),
            )
            return False
        return True


def _chunk(lines: list[str], limit: int) -> list[str]:
    """Join lines into messages of at most `limit` characters."""
    messages: list[str] = []
    current = ""
    for line in lines:
        line = line[:limit]
        if current and len(current) + 1 + len(line) > limit:
            messages.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        messages.append(current)
    return messages
