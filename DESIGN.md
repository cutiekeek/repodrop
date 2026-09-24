# RepoWatch — Design Document

> Working name. A public Discord bot that watches public GitHub repositories and announces updates in subscribed channels.

## 1. Overview

RepoWatch lets any Discord server subscribe channels to public GitHub repositories. When a watched repo publishes a release, pushes a tag, or (optionally) gets new commits, the bot posts an announcement to every subscribed channel.

The bot is multi-tenant: each server manages its own subscription list, and all servers share a single polling pipeline so each repo is checked once regardless of how many servers follow it.

## 2. Goals

- **Public and multi-server.** Any server can invite the bot, and each server's subscriptions are isolated by guild ID.
- **Self-service management.** Server admins can add, remove, and list repo subscriptions with slash commands.
- **Reliable delivery.** No missed announcements after a crash, and no duplicate posts on retry.
- **Efficient GitHub usage.** Each repo is polled once for all subscribers, and conditional requests (ETags) keep the rate-limit cost low.
- **Observable.** Every poll and delivery is traceable end to end in Logfire.
- **Simple to operate.** v1 runs as a single Python process with Postgres as the only dependency.

## 3. Non-goals (v1)

- Private repositories.
- Push-based updates via GitHub webhooks (see §11, Future Work).
- A web dashboard.
- Issues, pull requests, discussions, or stars.
- Watching non-GitHub hosts (GitLab, Codeberg).

## 4. Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Update detection | Polling the GitHub REST API | Webhooks require admin access to the repo, so they can't be used on repos we don't own. |
| API efficiency | ETag conditional requests | Authenticated `304 Not Modified` responses don't count against the rate limit. |
| Repo identity | GitHub numeric repo ID | Survives renames and transfers; `full_name` is display-only. |
| Queue | Postgres outbox (`deliveries` table) with `FOR UPDATE SKIP LOCKED` | Durable retries and deduplication without extra infrastructure. |
| Process model | One asyncio process: bot + poller + announcer | Nothing needs a separate service yet. An `asyncio.Event` wakes the announcer. |
| Web framework | None in v1 | No webhooks or dashboard yet. Deferred to Future Work. |
| Redis | Not used | Postgres covers queueing, dedup, and locking at this scale. |
| Commands | Slash commands only | Avoids the privileged message content intent, which simplifies verification. |
| Logging/tracing | Pydantic Logfire | Structured spans across GitHub calls, DB queries, and Discord sends. |

## 5. Tech Stack

- **Python 3.12+**
- **discord.py** for the bot (slash commands via `app_commands`, `AutoShardedBot` when needed)
- **PostgreSQL** for all persistent state
- **SQLAlchemy 2.0 (async) + asyncpg** for data access
- **Alembic** for migrations
- **httpx** (async) for GitHub API calls
- **Pydantic** for GitHub API response models
- **pydantic-settings** for configuration (env vars / `.env`)
- **Logfire** for logging, tracing, and metrics

## 6. Architecture

```
┌──────────────────────── single asyncio process ────────────────────────┐
│                                                                        │
│   ┌──────────────┐     ┌──────────────┐     ┌──────────────────────┐   │
│   │  Bot / cogs  │     │    Poller    │     │      Announcer       │   │
│   │ slash cmds   │     │ due repos →  │     │ claims pending       │   │
│   │ sub/unsub/   │     │ GitHub API → │────▶│ deliveries → posts   │   │
│   │ list         │     │ new events   │ evt │ to Discord → marks   │   │
│   └──────┬───────┘     └──────┬───────┘     └──────────┬───────────┘   │
│          │                    │                        │               │
└──────────┼────────────────────┼────────────────────────┼───────────────┘
           ▼                    ▼                        ▼
      ┌──────────────────────── PostgreSQL ─────────────────────────┐
      │ repos · repo_watches · subscriptions · events · deliveries  │
      └─────────────────────────────────────────────────────────────┘
```

### Components

- **Bot:** registers slash commands, validates input, reads and writes `subscriptions`, and handles guild join/leave events.
- **Poller:** a background task that selects repo watches whose `next_poll_at` has passed, fetches from GitHub, detects new items, and writes `events` and `deliveries` in one transaction.
- **Announcer:** a background task that claims pending deliveries, builds embeds, sends them, and records the result. It wakes on an `asyncio.Event` set by the poller, with a periodic sweep as a fallback.

### Layering rule

`github/`, `db/`, and `poller/` must not import `discord`. Only `bot/` and `announcer/` touch Discord. That keeps the core reusable if a web service or separate worker process is added later.

## 7. Data Model

```sql
-- One row per GitHub repository anyone is watching.
CREATE TABLE repos (
    id             BIGSERIAL PRIMARY KEY,
    github_id      BIGINT NOT NULL UNIQUE,
    full_name      TEXT   NOT NULL,          -- display only; refreshed on poll
    default_branch TEXT   NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Poll state per (repo, event kind). Only kinds with at least one
-- active subscription are polled.
CREATE TABLE repo_watches (
    repo_id        BIGINT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    kind           TEXT   NOT NULL,           -- 'release' | 'tag' | 'commit'
    branch         TEXT   NOT NULL DEFAULT '', -- used for 'commit' only
    etag           TEXT,
    last_seen_id   TEXT,                       -- release ID, tag name, or commit SHA
    poll_interval  INTERVAL NOT NULL DEFAULT '10 minutes',
    next_poll_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, kind, branch)
);

-- A channel's subscription to a repo.
CREATE TABLE subscriptions (
    id                  BIGSERIAL PRIMARY KEY,
    guild_id            BIGINT NOT NULL,
    channel_id          BIGINT NOT NULL,
    repo_id             BIGINT NOT NULL REFERENCES repos(id),
    kinds               TEXT[] NOT NULL DEFAULT '{release}',
    branch              TEXT,                  -- NULL = default branch
    include_prereleases BOOLEAN NOT NULL DEFAULT false,
    active              BOOLEAN NOT NULL DEFAULT true,
    created_by          BIGINT NOT NULL,       -- Discord user ID
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, channel_id, repo_id)
);
CREATE INDEX ON subscriptions (repo_id) WHERE active;
CREATE INDEX ON subscriptions (guild_id);

-- A detected update. The unique key makes detection idempotent.
CREATE TABLE events (
    id          BIGSERIAL PRIMARY KEY,
    repo_id     BIGINT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    kind        TEXT   NOT NULL,
    external_id TEXT   NOT NULL,               -- release ID / tag name / push range
    payload     JSONB  NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repo_id, kind, external_id)
);

-- Outbox: one row per (event, subscription).
CREATE TABLE deliveries (
    id              BIGSERIAL PRIMARY KEY,
    event_id        BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    subscription_id BIGINT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    status          TEXT   NOT NULL DEFAULT 'pending', -- pending | sent | failed | skipped
    attempts        INT    NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    message_id      BIGINT,
    last_error      TEXT,
    UNIQUE (event_id, subscription_id)
);
CREATE INDEX ON deliveries (next_attempt_at) WHERE status = 'pending';
```

When a guild removes the bot, its subscriptions are deleted, and the cascades clean up their deliveries. Orphaned repos (no subscriptions left) can be pruned by a periodic cleanup task.

## 8. Core Flows

### 8.1 Subscribe

1. An admin runs `/github subscribe`.
2. The bot resolves `owner/name` via `GET /repos/{owner}/{repo}` and rejects private or missing repos with a clear message.
3. It upserts into `repos` by `github_id` and inserts the subscription (a unique violation means "already subscribed").
4. It ensures a `repo_watches` row exists for each requested kind.
5. **Baseline:** if the watch is new, the bot fetches the current latest item and stores it as `last_seen_id` *without* creating an event, so new subscribers don't get flooded with old releases.
6. It confirms with an ephemeral reply showing the latest release as a preview.

### 8.2 Poll

1. Select due watches: `WHERE next_poll_at <= now() ORDER BY next_poll_at LIMIT N`.
2. For each watch, with bounded concurrency (a semaphore of roughly 5–10), send a conditional GET with `If-None-Match: <etag>`.
   - On `304`, nothing changed; reschedule the watch.
   - On `200`, parse with Pydantic, collect items newer than `last_seen_id`, and update the etag.
3. In one transaction:
   - Insert `events` with `ON CONFLICT DO NOTHING`.
   - Insert `deliveries` for every active subscription whose `kinds` (and prerelease setting) match.
   - Advance `last_seen_id` and `next_poll_at`.
4. Set the announcer's `asyncio.Event`.

**Endpoints by kind:**

- Releases use `GET /repos/{o}/{r}/releases?per_page=10`, skipping drafts and skipping prereleases unless the subscription opts in.
- Tags use `GET /repos/{o}/{r}/tags?per_page=10`.
- Commits use `GET /repos/{o}/{r}/commits?sha={branch}&per_page=20`, and all new commits from one poll become a single batched event.

**Adaptive intervals:** repos that recently changed are polled more often (around 5 minutes), and quiet repos back off gradually (up to about 60 minutes).

### 8.3 Announce

1. Claim a batch:
   ```sql
   SELECT ... FROM deliveries
   WHERE status = 'pending' AND next_attempt_at <= now()
   ORDER BY next_attempt_at
   LIMIT 50
   FOR UPDATE SKIP LOCKED;
   ```
2. Build the embed from the event payload and send it with `allowed_mentions=AllowedMentions.none()`.
3. On success, set `status='sent'` and record `message_id`.
4. On failure:
   - A Discord `Forbidden` or `NotFound` error (channel deleted or permissions lost) deactivates the subscription and marks the delivery `skipped`.
   - Transient errors increment `attempts` and schedule an exponential backoff on `next_attempt_at`.
   - After 5 failed attempts, the delivery is marked `failed`.

## 9. Slash Commands

All commands live under a `/github` group, gated with `default_member_permissions=manage_guild`, and replies are ephemeral.

| Command | Options | Behavior |
|---|---|---|
| `/github subscribe` | `repo` (owner/name or URL), `events` (releases/tags/commits), `channel` (default: current), `prereleases` (bool) | Validates the repo and creates the subscription. |
| `/github unsubscribe` | `repo` (autocomplete from this server's subs), `channel` | Removes the subscription. |
| `/github list` | `channel` (optional) | Lists the server's or channel's subscriptions. |
| `/github test` | `repo` | Posts the latest release to check formatting and permissions. |

**Limits:** 25 subscriptions per server by default, stored as a config value so it can be raised for specific servers later.

## 10. Operational Concerns

### GitHub rate limits

- Authenticate with a token for 5,000 requests per hour. Unauthenticated access is limited to 60 per hour and is not viable.
- Track the `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers. When remaining drops below a threshold, stretch poll intervals until the reset.
- Respect `Retry-After` on secondary rate limits, and keep request concurrency modest.

### Discord

- Enforce embed limits: a 4096-character description (truncate release notes and link to the full release) and a 256-character title.
- Disable all mentions so release notes can never ping anyone.
- Use `on_guild_remove` to delete that guild's subscriptions.
- Switch to `AutoShardedBot` as the bot grows. Discord requires verification at 100 servers.

### Configuration (`pydantic-settings`)

- `DISCORD_TOKEN`
- `GITHUB_TOKEN`
- `DATABASE_URL`
- `LOGFIRE_TOKEN`
- `MAX_SUBS_PER_GUILD`
- `POLL_MIN_INTERVAL`
- `POLL_MAX_INTERVAL`
- `POLL_CONCURRENCY`

### Observability (Logfire)

- Call `logfire.configure(service_name="repowatch")` at startup.
- Call `logfire.instrument_httpx()` so every GitHub request gets a span with status and latency.
- Call `logfire.instrument_asyncpg()` or `logfire.instrument_sqlalchemy()` for query spans.
- Route discord.py's stdlib logging into Logfire with `logfire.LogfireLoggingHandler`.
- Add custom spans:
  - `poll_cycle`, with the count of due watches.
  - `poll_repo`, with `repo`, `kind`, and a 304-or-200 result.
  - `deliver`, with `guild_id`, `channel_id`, `event_kind`, and the outcome.
- Track these metrics:
  - GitHub rate limit remaining.
  - Pending delivery backlog.
  - Poll lag (`now() - next_poll_at` of the oldest due watch).
  - Delivery failures by reason.
- Make sure tokens never appear in span attributes; Logfire's scrubbing is a backstop, not the plan.

## 11. Project Layout

```
repowatch/
├── pyproject.toml
├── alembic.ini
├── alembic/
│   └── versions/
└── src/repowatch/
    ├── __main__.py          # entrypoint: configure logfire, start bot + tasks
    ├── config.py            # pydantic-settings
    ├── observability.py     # logfire setup, custom metrics
    ├── db/
    │   ├── models.py        # SQLAlchemy models
    │   ├── session.py       # engine / async session factory
    │   └── queries.py       # subscription, watch, delivery queries
    ├── github/
    │   ├── client.py        # httpx client, ETag handling, rate-limit tracking
    │   └── schemas.py       # Pydantic models for API responses
    ├── poller/
    │   ├── scheduler.py     # due-watch selection, adaptive intervals
    │   └── detectors.py     # release / tag / commit diffing
    ├── announcer/
    │   ├── dispatcher.py    # claim + send + retry
    │   └── embeds.py        # embed builders per event kind
    └── bot/
        ├── client.py        # bot subclass, setup_hook starts tasks
        └── cogs/
            └── subscriptions.py
```

## 12. Build Phases

1. **Foundation:** config, Logfire setup, SQLAlchemy models, first Alembic migration, and bot skeleton.
2. **Subscriptions:** `/github subscribe`, `/github unsubscribe`, `/github list`, repo validation, per-guild limits, and guild-leave cleanup.
3. **Release polling:** GitHub client with ETags, the release detector, baselining, and event and delivery creation.
4. **Announcer:** the outbox dispatcher, release embeds, retries, and handling of lost permissions.
5. **More event kinds:** tags, commits (batched), and the prerelease option.
6. **Hardening:** adaptive intervals, rate-limit backoff, orphan repo cleanup, metrics, and `/github test`.
7. **Public launch:** bot listing, an invite link with minimal permissions (View Channel, Send Messages, Embed Links), and verification.

## 13. Future Work

- **GitHub App + FastAPI webhook service:** maintainers who install the app get instant, push-based updates, and their repos skip polling. This service can be added as a new entrypoint because the core is Discord-independent.
- **Web dashboard:** Discord OAuth login for managing subscriptions outside Discord, served by the same FastAPI service.
- **Per-subscription filters:** tag name patterns, commit path filters, and custom message templates.
- **Role pings:** an opt-in role mention per subscription.
- **Redis:** only if the bot splits into multiple shard processes that need a shared GitHub rate-limit budget or cross-process caching.
- **Multiple pollers:** use Postgres advisory locks or `SKIP LOCKED` on `repo_watches` so poller instances never double-poll a repo.
