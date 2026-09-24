"""`/github status`: a per-subscription health report, paginated."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import discord

from repodrop.db.models import RepoWatch
from repodrop.db.queries import SubscriptionHealthRow, WatchKey
from repodrop.subscription_plan import SubscriptionState, describe_state, needed_watches

PAGE_ENTRIES = 8
PAGE_CHARS = 3500  # leaves room for the summary line under the 4096 description limit
VIEW_TIMEOUT = 300


@dataclass(slots=True)
class HealthEntry:
    repo_full_name: str
    channel_id: int
    what: str  # "releases, commits on `main`"
    active: bool
    disabled_reason: str | None = None
    disabled_at: datetime | None = None
    last_sent_at: datetime | None = None
    last_polled_at: datetime | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.active and not self.problems


def _is_stopped(watch: RepoWatch) -> bool:
    # Postgres 'infinity' comes back as datetime.max.
    return watch.next_poll_at.replace(tzinfo=None) == datetime.max


def build_entries(
    rows: list[SubscriptionHealthRow],
    watches_by_repo: dict[int, list[RepoWatch]],
    permission_problems: Callable[[int], list[str]],
) -> list[HealthEntry]:
    """`permission_problems(channel_id)` returns what's wrong with the bot's access there."""
    entries = []
    for row in rows:
        sub, repo = row.subscription, row.repo
        state = SubscriptionState(
            kinds=frozenset(sub.kinds),
            branches=tuple(sub.branches),
            include_prereleases=sub.include_prereleases,
        )
        needed = needed_watches(repo.id, state, repo.default_branch)
        watches = [
            w
            for w in watches_by_repo.get(repo.id, [])
            if WatchKey(w.repo_id, w.kind, w.branch) in needed
        ]
        entry = HealthEntry(
            repo_full_name=repo.full_name,
            channel_id=sub.channel_id,
            what=describe_state(state),
            active=sub.active,
            disabled_reason=sub.disabled_reason,
            disabled_at=sub.disabled_at,
            last_sent_at=row.last_sent_at,
        )
        polled = [w.last_polled_at for w in watches if w.last_polled_at is not None]
        entry.last_polled_at = min(polled) if polled else None
        if sub.active:
            entry.problems.extend(permission_problems(sub.channel_id))
            for w in sorted(watches, key=lambda w: (w.kind, w.branch)):
                if _is_stopped(w):
                    entry.problems.append(f"Stopped: {w.last_error or 'no longer polled'}")
                elif w.last_error:
                    entry.problems.append(f"Last check failed: {w.last_error}")
        entries.append(entry)
    return entries


def _ts(dt: datetime) -> str:
    return f"<t:{int(dt.astimezone(UTC).timestamp())}:R>"


def render_entry(e: HealthEntry) -> str:
    icon = "✅" if e.healthy else ("⛔" if not e.active else "⚠️")
    lines = [f"{icon} **{e.repo_full_name}** → <#{e.channel_id}> · {e.what}"]
    if not e.active:
        since = f" ({_ts(e.disabled_at)})" if e.disabled_at else ""
        lines.append(f"Disabled: {e.disabled_reason or 'paused'}{since}")
    posted = f"last posted {_ts(e.last_sent_at)}" if e.last_sent_at else "nothing posted yet"
    polled = f"checked {_ts(e.last_polled_at)}" if e.last_polled_at else "not checked yet"
    lines.append(f"{posted} · {polled}")
    lines.extend(f"⚠️ {p}" for p in e.problems)
    return "\n".join(lines)


def summary(entries: list[HealthEntry]) -> str:
    healthy = sum(e.healthy for e in entries)
    attention = len(entries) - healthy
    parts = [f"{healthy} healthy"]
    if attention:
        parts.append(f"{attention} need{'s' if attention == 1 else ''} attention")
    return " · ".join(parts)


def paginate(entries: list[HealthEntry]) -> list[str]:
    """Problems first, then by channel; pages capped by entry count and length."""
    ordered = sorted(entries, key=lambda e: (e.healthy, e.channel_id, e.repo_full_name.lower()))
    pages: list[str] = []
    current: list[str] = []
    size = 0
    for entry in ordered:
        block = render_entry(entry)
        if current and (len(current) >= PAGE_ENTRIES or size + len(block) + 2 > PAGE_CHARS):
            pages.append("\n\n".join(current))
            current, size = [], 0
        current.append(block[:PAGE_CHARS])
        size += len(block) + 2
    if current:
        pages.append("\n\n".join(current))
    return pages


def page_embed(title: str, summary_line: str, pages: list[str], index: int) -> discord.Embed:
    embed = discord.Embed(title=title, description=f"{summary_line}\n\n{pages[index]}")
    if len(pages) > 1:
        embed.set_footer(text=f"Page {index + 1}/{len(pages)}")
    return embed


class StatusView(discord.ui.View):
    """Previous/Next buttons for a multi-page report; only its requester can use them."""

    def __init__(self, title: str, summary_line: str, pages: list[str], owner_id: int) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.title, self.summary_line, self.pages = title, summary_line, pages
        self.owner_id = owner_id
        self.index = 0
        self._sync_buttons()

    def embed(self) -> discord.Embed:
        return page_embed(self.title, self.summary_line, self.pages, self.index)

    def _sync_buttons(self) -> None:
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index == len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def _show(self, interaction: discord.Interaction, delta: int) -> None:
        self.index = max(0, min(len(self.pages) - 1, self.index + delta))
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show(interaction, 1)
