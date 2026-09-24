# RepoDrop — Design Document

> Working name. A public Discord bot that watches public GitHub repositories and announces updates in subscribed channels.

## 1. Overview

RepoDrop lets any Discord server subscribe channels to public GitHub repositories. When a watched repo publishes a release, pushes a tag, or (optionally) gets new commits, the bot posts an announcement to every subscribed channel.

The bot is multi-tenant: each server manages its own subscription list, and all servers share a single polling pipeline so each repo is checked once regardless of how many servers follow it.

## 2. Goals

- **Public and multi-server.** Any server can invite the bot, and each server's subscriptions are isolated by guild ID.
- **Self-service management.** Any member can add repo subscriptions by default, and servers can limit that to a subscriber role. Server admins, and members with a manager role they choose, can manage every subscription and adjust server settings with slash commands.
- **Operator control.** The bot owner can raise or lower limits, restrict commit subscriptions, or block an abusive server entirely.
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
| Commit branches | A list of branches per subscription, one poll watch per branch | Lets one subscription follow several branches. Branches shared across subscriptions are polled once, and unchanged branches return free `304`s. |
| Per-server settings | `guild_settings` table, `NULL` = global default | One row per server holds both operator limits and admin preferences. Settings are cached in memory per server. |
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
    metadata_etag  TEXT,                      -- for the daily repo metadata refresh
    metadata_checked_at TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Poll state per (repo, event kind, branch). Only watches that at least
-- one active subscription needs are polled.
CREATE TABLE repo_watches (
    repo_id        BIGINT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    kind           TEXT   NOT NULL,           -- 'release' | 'tag' | 'commit'
    branch         TEXT   NOT NULL DEFAULT '', -- concrete branch name for 'commit'; '' otherwise
    etag           TEXT,
    last_seen_id   TEXT,                       -- watermark: newest release's published_at, tag name, or commit SHA
    poll_interval  INTERVAL NOT NULL DEFAULT '10 minutes',
    next_poll_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_polled_at TIMESTAMPTZ,                -- shown by /repodrop status
    last_error     TEXT,                       -- e.g. repo deleted or made private
    notified_at    TIMESTAMPTZ,                -- admin notice sent for a deleted branch (§8.6)
    PRIMARY KEY (repo_id, kind, branch)
);

-- A channel's subscription to a repo.
CREATE TABLE subscriptions (
    id                  BIGSERIAL PRIMARY KEY,
    guild_id            BIGINT NOT NULL,
    channel_id          BIGINT NOT NULL,
    repo_id             BIGINT NOT NULL REFERENCES repos(id),
    kinds               TEXT[] NOT NULL DEFAULT '{release}',
    branches            TEXT[] NOT NULL DEFAULT '{}', -- commit branches; empty = follow the default branch
    include_prereleases BOOLEAN NOT NULL DEFAULT false,
    active              BOOLEAN NOT NULL DEFAULT true,
    disabled_reason     TEXT,                  -- set when auto-disabled
    disabled_at         TIMESTAMPTZ,
    notified_at         TIMESTAMPTZ,           -- admin notice sent for the current disable (§8.6)
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
    payload     JSONB  NOT NULL,               -- includes html_url, previous_tag (§8.4)
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
    sent_at         TIMESTAMPTZ,
    last_error      TEXT,
    UNIQUE (event_id, subscription_id)
);
CREATE INDEX ON deliveries (next_attempt_at) WHERE status = 'pending';
CREATE INDEX ON deliveries (subscription_id, sent_at DESC) WHERE status = 'sent';
```

```sql
-- Per-server settings. A missing row means every setting uses its default.
-- Nullable columns fall back to the global config value.
CREATE TABLE guild_settings (
    guild_id            BIGINT PRIMARY KEY,

    -- Operator-controlled (owner commands only, §8.8)
    max_repos           INT,                   -- NULL = MAX_REPOS_PER_GUILD
    max_subscriptions   INT,                   -- NULL = MAX_SUBS_PER_GUILD
    commits_allowed     BOOLEAN NOT NULL DEFAULT true,
    blocked             BOOLEAN NOT NULL DEFAULT false,
    blocked_reason      TEXT,                  -- shown to the server on any /repodrop command
    blocked_at          TIMESTAMPTZ,

    -- Admin-controlled (/repodrop settings, §8.7)
    manager_role_id     BIGINT,                -- NULL = Manage Server only
    subscriber_role_id  BIGINT,                -- NULL = everyone can subscribe
    default_channel_id  BIGINT,                -- NULL = channel the command was run in
    embed_style         TEXT NOT NULL DEFAULT 'full',     -- 'full' | 'compact'
    latest_access       TEXT NOT NULL DEFAULT 'everyone', -- 'everyone' | 'managers'
    latest_allow_public BOOLEAN NOT NULL DEFAULT true,

    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

When a guild removes the bot, its subscriptions are deleted, and the cascades clean up their deliveries. Its `guild_settings` row is deleted too, **unless the server is blocked**: blocked rows are kept so that re-inviting the bot doesn't escape the block. Orphaned repos (no subscriptions left) and watches that no subscription needs anymore (a removed kind or branch) are pruned by a periodic cleanup task.

## 8. Core Flows

### 8.1 Subscribe (create or update)

`/repodrop subscribe` both creates subscriptions and modifies existing ones. A subscription is identified by (server, channel, repo), so running the command again for the same repo and channel updates it instead of failing.

**Options:** `repo`, `channel`, `releases`, `tags` and `commits` (booleans), `branches` (comma-separated names, or `default`), and `prereleases` (boolean). Event kinds are separate booleans rather than one choice option because Discord slash options can't multi-select, and separate booleans let an update switch one kind on or off without touching the others.

1. A member with subscribe access (§9) runs `/repodrop subscribe`. If the server is blocked, the bot replies with the block notice instead (§8.8). A per-user cooldown (for example, 5 subscribes per minute) keeps anyone from filling the server's caps in one burst.
2. The target channel is the `channel` option if given, otherwise the server's `default_channel_id`, otherwise the current channel. Before saving, the bot checks two sets of permissions in that channel:
   - **The bot's own:** View Channel, Send Messages and Embed Links, so the subscription can actually post.
   - **The member's:** View Channel and Send Messages. Without this, a member could use the bot to post into a channel they can't post in themselves, such as a read-only announcements channel. Managers skip this check.
3. The bot resolves `owner/name` via `GET /repos/{owner}/{repo}` and rejects private or missing repos with a clear message.
4. The rest runs in one transaction holding `pg_advisory_xact_lock(guild_id)`, so concurrent subscribes in the same server can't race each other. The bot looks up an existing subscription for (server, channel, repo) and takes the create or update path below.
5. **Checks on both paths:**
   - If commits would be on and `commits_allowed` is false for the server, the request is rejected with a short explanation.
   - Each listed branch is validated with `GET /repos/{o}/{r}/branches/{branch}`, and unknown branches are rejected by name. A subscription can list at most `MAX_BRANCHES_PER_SUB` branches (default 5).
6. The bot ensures a `repo_watches` row exists for every (kind, branch) the subscription now needs. For commits, that's one watch per listed branch, or the repo's current default branch when the list is empty.
7. **Baseline:** each newly created watch fetches the current latest item and stores it as `last_seen_id` *without* creating an event, so new subscribers (or newly added branches) don't get flooded with old items.
8. It confirms with an ephemeral reply: a preview of the latest release plus current usage for a new subscription (for example, "Repos 7/25 · Subscriptions 9/100"), or a summary of what changed for an update (for example, "Added commits on `main`, `dev` · Removed tags").

**Creating a subscription:**

- Omitted options use defaults: releases on, tags and commits off, commits following the default branch, prereleases off.
- **Limits:** the bot checks two caps before inserting.
  - **Distinct repos** (`max_repos`, default 25): `COUNT(DISTINCT repo_id)` across the server's subscriptions. Following a repo the server already watches, in another channel, doesn't count again. Polling is per repo, so extra channels add no GitHub API calls.
  - **Total subscriptions** (`max_subscriptions`, default 100): every (channel, repo) pair counts. This bounds Discord posting volume rather than API usage.
  - Disabled subscriptions still count toward both caps, so reactivating one never fails on a limit.
- The bot upserts into `repos` by `github_id` and inserts the subscription with `created_by` set to the member.

**Updating a subscription:**

- Only the subscription's creator (`created_by`) or a manager can change it. Anyone else gets a reply saying the subscription already exists and who can modify it.
- Only the options passed are changed; omitted options keep their current values. For example, `commits:true` adds commits, `tags:false` removes tags, `branches:main,dev` replaces the branch list, and `branches:default` goes back to following the default branch.
- An update that would leave no event kinds on is rejected, with a pointer to `/repodrop unsubscribe`.
- `branches` is rejected when commits would be off, rather than stored with no effect. A branch named explicitly stays that branch even if it's the current default; only `branches:default` (or an empty list) follows the default branch when it changes.
- A disabled subscription is reactivated and its `disabled_reason` cleared.
- Updates don't change the server's repo or subscription counts, so those caps aren't rechecked. The branch cap still applies.
- Pending deliveries for kinds or branches that were just removed are marked `skipped`.
- Moving a subscription to a different channel isn't an update, since the channel is part of its identity. That's an unsubscribe in the old channel and a subscribe in the new one.

### 8.2 Poll

1. Select due watches: `WHERE next_poll_at <= now() ORDER BY next_poll_at LIMIT N`.
2. For each watch, with bounded concurrency (a semaphore of roughly 5–10), send a conditional GET with `If-None-Match: <etag>`.
   - On `304`, nothing changed; reschedule the watch.
   - On `200`, parse with Pydantic, collect items newer than `last_seen_id`, and update the etag.
3. In one transaction:
   - Insert `events` with `ON CONFLICT DO NOTHING`.
   - Insert `deliveries` for every active subscription whose `kinds` (and prerelease setting) match, joined against `guild_settings` to skip blocked servers and, for commit events, servers with `commits_allowed = false`. Skipped subscriptions keep their rows, so unblocking or re-allowing commits resumes delivery.
   - Advance `last_seen_id` and `next_poll_at`.
4. Set the announcer's `asyncio.Event`.

**Endpoints by kind:**

- Releases use `GET /repos/{o}/{r}/releases?per_page=10`, skipping drafts and skipping prereleases unless the subscription opts in. Each new release's payload records the tag of the next-older release in the same response as `previous_tag`, for the Compare button (§8.4).
- Tags use `GET /repos/{o}/{r}/tags?per_page=10`. Each new tag's payload records the next tag in the same response as `previous_tag`, for the Compare button.
- Commits use `GET /repos/{o}/{r}/commits?sha={branch}&per_page=20`, polled separately for each watched branch. All new commits on one branch from one poll become a single batched event whose payload includes the branch name, with `external_id` set to `{branch}:{before_sha}..{after_sha}`.

**Multiple branches:**

- Each watched branch is its own `repo_watches` row with its own ETag and last-seen SHA. The watch key already includes the branch, so the only schema change for multi-branch support is storing a list on subscriptions.
- Watches are shared. If one subscription lists `main` explicitly and another follows the default branch, which is `main`, both use the same watch, so there are no duplicate API calls or duplicate events.
- A commit event on branch B is delivered to every active subscription with commits on where either B is in `branches`, or `branches` is empty and B is the repo's current `default_branch`.
- Each extra branch adds one request per poll, but branches with no new commits return `304` responses, which don't count against the rate limit. The per-subscription branch cap keeps the worst case bounded.
- **Default branch changes:** the poller refreshes repo metadata (`GET /repos/{o}/{r}` with an ETag) about once a day. When `default_branch` changes, subscriptions following the default automatically move to the new branch: a baselined watch is created for it, and the cleanup task removes the old watch if nothing else uses it.
- **Deleted branches:** if a watched branch stops existing, the poller records the error on that watch and stops polling it. The subscription stays active for its other kinds and branches, `/repodrop status` flags the missing branch, and a one-time admin notice is sent (§8.6).

**Missing repos:** a `404` means the repo was deleted or made private. The poller records it in `last_error` and auto-disables every subscription to that repo with a reason (see §8.6 for how admins are told). Once every subscription is disabled, the cleanup task removes the repo's watches, so polling stops; if the repo comes back, re-running `/repodrop subscribe` reactivates it.

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
2. Build the embed and its link buttons (§8.4) from the event payload, using the server's `embed_style` setting, and send it with `allowed_mentions=AllowedMentions.none()`.
3. On success, set `status='sent'` and record `message_id` and `sent_at`.
4. On failure:
   - A Discord `Forbidden` or `NotFound` error (channel deleted or permissions lost) deactivates the subscription with a `disabled_reason`, marks the delivery `skipped`, and sends the admin notice described in §8.6.
   - Transient errors increment `attempts` and schedule an exponential backoff on `next_attempt_at`.
   - After 5 failed attempts, the delivery is marked `failed`.

### 8.4 Release embed buttons

Announcements carry **link buttons** (`discord.ui.Button(style=ButtonStyle.link, url=...)`). Link buttons open a URL directly, so they need no interaction handlers or persistent views and keep working across bot restarts.

| Event kind | Buttons |
|---|---|
| Release | **View release** (`html_url`), and **Compare** (`/compare/{previous_tag}...{tag}`, omitted when there's no previous tag) |
| Tag | **View tag**, **Compare** |
| Commits (batched, per branch) | **View commits** (`/compare/{before_sha}...{after_sha}`). The embed title names the branch. |

- There are no download buttons, to keep announcements clean: downloads are one click away on the release page.
- The event payload stores only what the buttons need: `html_url`, `tag_name`, and `previous_tag`.
- The same builder renders `/repodrop latest` replies, so both look identical and respect the same settings.

**Embed styles:**

- **Full** (default): repo, version, title, publish time, and the release notes (truncated to Discord's limit with a link to the rest).
- **Compact:** repo, version, title and publish time only, with no release notes body. Buttons are unchanged.

### 8.5 `/repodrop latest`

A quick lookup of a repo's newest release that doesn't require a subscription.

1. A member runs `/repodrop latest repo:owner/name`, with autocomplete from the server's subscribed repos.
   - If `latest_access` is `managers`, non-managers get an ephemeral "only managers can use this here" reply.
   - If `public:true` is passed but `latest_allow_public` is false, the bot answers ephemerally and says public posts are turned off in this server.
2. It calls `GET /repos/{o}/{r}/releases/latest`, which already excludes drafts and prereleases, and falls back to the newest tag if the repo has no releases. (Stored data isn't used: baselining records only a watermark, so the bot has no release details for a repo until it announces one.)
3. It replies with the standard release embed and buttons. The reply is ephemeral by default, and a `public:true` option posts it visibly in the channel.

To keep this from eating the GitHub rate limit:

- Cache lookup results in memory for about 5 minutes, keyed by repo.
- Apply a per-user cooldown with `app_commands.checks.cooldown` (for example, 3 uses per 30 seconds).

### 8.6 `/repodrop status` and admin notices

`/repodrop status [channel]` gives managers an ephemeral health report for the server's subscriptions.

- A summary line at the top, such as "4 healthy · 1 needs attention".
- One entry per subscription showing:
  - The repo, channel, event kinds, and commit branches.
  - Its state: active, or disabled with its `disabled_reason`.
  - When it last posted, rendered as a Discord relative timestamp (`<t:unix:R>`).
  - When its repo was last polled, and any poll error, including branches that no longer exist.
  - A live permission check using `channel.permissions_for(guild.me)` for View Channel, Send Messages and Embed Links, so problems show up before a post fails.
- Discord limits an embed to 6,000 characters in total, so the report is paginated with Previous/Next buttons on a short-lived view.

**Proactive notices:** when a subscription is auto-disabled (lost permissions, deleted channel, or missing repo), or a watched branch disappears, the bot posts a short notice in the server's system channel, if one is set and the bot can post there. The notice names the repo and the reason, and says that running `/repodrop subscribe` again reactivates it. At most one notice is sent per disable.

The poller can't post to Discord (§6 layering rule), so notices go through the database: disabling a subscription sets `disabled_at` and leaves `notified_at` empty, and the announcer's periodic sweep sends a notice for every disabled subscription (and every errored branch watch) whose `notified_at` is empty, then sets it. That survives restarts and guarantees one notice per disable; reactivating a subscription clears both columns.

### 8.7 `/repodrop settings`

`/repodrop settings` opens an ephemeral panel (a `discord.ui.View`) for managers. Discord allows 5 component rows per message, so the two `/repodrop latest` settings share one select with four combined choices. Each change saves immediately and re-renders the panel. The view times out after about 5 minutes.

| Control | Component | Setting |
|---|---|---|
| Manager role | Role select (with a "clear" option) | `manager_role_id`. Only members with Manage Server can change this, so a manager can't grant or remove manager access. |
| Subscriber role | Role select (with a "clear" option) | `subscriber_role_id`. When set, only members with this role (plus managers) can subscribe. Clearing it opens subscribing to everyone again. |
| Default channel | Channel select (text and announcement channels) | `default_channel_id` |
| Embed style | Select: Full / Compact | `embed_style` |
| `/repodrop latest` | Select: Anyone or Managers only, each with public posts allowed or private replies only | `latest_access`, `latest_allow_public` |

The panel also shows the operator-controlled values read-only: repo and subscription usage against their caps, and whether commit subscriptions are allowed.

**Settings cache:** effective settings (`guild_value ?? config_default`) are cached in memory per server and invalidated on every write, since the announcer and commands read them constantly. If the bot is ever split into multiple processes, invalidate through Postgres `LISTEN/NOTIFY`.

### 8.8 Operator controls

Owner-only commands let the bot operator manage any server by ID. They are registered **only in the operator's private dev server** (a guild-scoped command sync to `DEV_GUILD_ID`, separate from the global `/repodrop` sync) hidden there from members without Administrator (`default_member_permissions`, which works because `/repodrop-owner` is its own top-level command), and also checked in code against `OWNER_IDS`, so nobody else ever sees or runs them. It's a separate command rather than a `/repodrop owner` subcommand because subcommands can't be registered separately from their parent, so it would appear in every server.

- **Limits:** set or clear a server's `max_repos` and `max_subscriptions` overrides. Lowering a limit below current usage doesn't delete anything; it only blocks new subscriptions until usage drops.
- **Commit subscriptions:** set `commits_allowed` per server. It defaults to true. Turning it off stops commit deliveries and blocks new commit subscriptions, but keeps existing ones so turning it back on resumes them.
- **Block / unblock:** set `blocked` with a required `blocked_reason`.
  - While blocked, every `/repodrop` command in that server replies with an ephemeral notice such as "RepoDrop has been disabled for this server: {reason}", along with a contact link if you provide one.
  - No new deliveries are created for the server's subscriptions, and any pending ones are marked `skipped`.
  - Subscriptions are kept, so unblocking restores the server's setup as it was.
  - The block survives the bot being removed and re-invited (§7).
- **Inspect:** show a server's settings, usage, and recent delivery failures.

The blocked check runs as a group-level `interaction_check` on `/repodrop`, so it applies to every subcommand without repeating the logic.

## 9. Slash Commands

All commands live under a `/repodrop` group. Discord only applies `default_member_permissions` to top-level commands, so subcommands can't be gated individually. The group is left visible to everyone, and management subcommands are enforced in code with a custom `is_manager` check.

Two access levels are checked in code:

- A **manager** is a member with the Manage Server permission, or a member with the server's `manager_role_id` role if one is set.
- **Subscribe access** belongs to everyone by default. When the server sets `subscriber_role_id`, it's limited to members with that role. Managers always have subscribe access, whether or not they hold the subscriber role.

Members can remove subscriptions they created (matched on `created_by`), and managers can remove any subscription. Replies are ephemeral unless noted.

| Command | Options | Behavior |
|---|---|---|
| `/repodrop subscribe` | `repo` (owner/name or URL), `channel` (default: server default, then current), `releases` / `tags` / `commits` (bool), `branches` (comma-separated or `default`), `prereleases` (bool) | Creates a subscription, or updates the existing one for that repo and channel (§8.1). Everyone by default, or the subscriber role if set; managers always. Updates are limited to the creator and managers. |
| `/repodrop unsubscribe` | `repo` (autocomplete: the member's own subscriptions, or all of them for managers), `channel` | Removes the subscription. Members can remove their own; managers can remove any. |
| `/repodrop list` | `channel` (optional) | Lists the server's or channel's subscriptions. |
| `/repodrop test` | `repo` | Posts the latest release to check formatting and permissions. Managers only. |
| `/repodrop latest` | `repo` (autocomplete), `public` (bool) | Shows a repo's newest release with buttons, no subscription needed (§8.5). Everyone by default; can be limited to managers. |
| `/repodrop status` | `channel` (optional) | Health report for the server's subscriptions (§8.6). Managers only. |
| `/repodrop settings` | none | Opens the settings panel (§8.7). Managers only; changing the manager role requires Manage Server. |

**Limits:** 25 distinct repos and 100 total subscriptions per server by default, overridable per server by the operator (§8.1, §8.8).

**Owner commands** (dev server only, §8.8):

| Command | Behavior |
|---|---|
| `/repodrop-owner limits guild_id max_repos max_subscriptions` | Set or clear a server's caps. |
| `/repodrop-owner commits guild_id allowed` | Allow or disallow commit subscriptions. |
| `/repodrop-owner block guild_id reason` | Block a server with a reason shown to its members. |
| `/repodrop-owner unblock guild_id` | Lift a block. |
| `/repodrop-owner inspect guild_id` | Show settings, usage and recent failures. |

## 10. Operational Concerns

### GitHub rate limits

- Authenticate with a token for 5,000 requests per hour. Unauthenticated access is limited to 60 per hour and is not viable.
- Track the `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers. When remaining drops below a threshold, stretch poll intervals until the reset.
- Respect `Retry-After` on secondary rate limits, and keep request concurrency modest.

### Discord

- Enforce embed limits: a 4096-character description (truncate release notes and link to the full release) and a 256-character title.
- Disable all mentions so release notes can never ping anyone.
- Use `on_guild_remove` to delete that guild's subscriptions and settings, keeping the settings row if the server is blocked.
- Switch to `AutoShardedBot` as the bot grows. Discord requires verification at 100 servers.

### Configuration (`pydantic-settings`)

- `DISCORD_TOKEN`
- `GITHUB_TOKEN`
- `DATABASE_URL`
- `LOGFIRE_TOKEN`
- `MAX_REPOS_PER_GUILD` (default 25)
- `MAX_SUBS_PER_GUILD` (default 100)
- `MAX_BRANCHES_PER_SUB` (default 5)
- `OWNER_IDS` (Discord user IDs allowed to run owner commands)
- `DEV_GUILD_ID` (the private server owner commands are registered in)
- `DEV_SYNC` (default false; when true, `/repodrop` is also synced to `DEV_GUILD_ID` for instant updates while developing. Otherwise `/repodrop` is synced globally.)
- `BLOCK_CONTACT_URL` (optional link shown in block notices)
- `POLL_MIN_INTERVAL`
- `POLL_MAX_INTERVAL`
- `POLL_CONCURRENCY`

### Observability (Logfire)

- Call `logfire.configure(service_name="repodrop")` at startup.
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
repodrop/
├── pyproject.toml
├── alembic.ini
├── alembic/
│   └── versions/
└── src/repodrop/
    ├── __main__.py          # entrypoint: configure logfire, start bot + tasks
    ├── config.py            # pydantic-settings
    ├── observability.py     # logfire setup, custom metrics
    ├── guild_settings.py    # effective settings + in-memory cache
    ├── db/
    │   ├── models.py        # SQLAlchemy models
    │   ├── session.py       # engine / async session factory
    │   └── queries.py       # subscription, watch, delivery, settings queries
    ├── github/
    │   ├── client.py        # httpx client, ETag handling, rate-limit tracking
    │   └── schemas.py       # Pydantic models for API responses
    ├── poller/
    │   ├── scheduler.py     # due-watch selection, adaptive intervals
    │   └── detectors.py     # release / tag / commit diffing
    ├── announcer/
    │   ├── dispatcher.py    # claim + send + retry
    │   └── embeds.py        # embed + link-button builders (shared with /repodrop latest)
    └── bot/
        ├── client.py        # bot subclass, setup_hook starts tasks
        ├── checks.py        # is_manager, can_subscribe, blocked interaction_check
        └── cogs/
            ├── subscriptions.py # subscribe / unsubscribe / list / test
            ├── lookup.py        # /repodrop latest
            ├── status.py        # /repodrop status, admin notices
            ├── settings.py      # /repodrop settings panel
            └── owner.py         # /repodrop-owner commands (dev server only)
```

## 12. Build Phases

1. **Foundation:** config, Logfire setup, SQLAlchemy models, first Alembic migration, and bot skeleton.
2. **Subscriptions:** `/repodrop subscribe` (create and update), `/repodrop unsubscribe`, `/repodrop list`, repo validation, the `guild_settings` table, distinct-repo and total limits, manager and blocked checks, and guild-leave cleanup.
3. **Release polling:** GitHub client with ETags, the release detector, baselining, and event and delivery creation.
4. **Announcer:** the outbox dispatcher, release embeds with link buttons, retries, handling of lost permissions, and `/repodrop latest`.
5. **More event kinds:** tags, commits (batched, multi-branch, with default-branch tracking and deleted-branch handling), and the prerelease option.
6. **Server settings:** the `/repodrop settings` panel, embed styles, `/repodrop latest` access rules, the settings cache, and `/repodrop-owner` commands.
7. **Hardening:** adaptive intervals, rate-limit backoff, missing-repo handling, orphan repo cleanup, metrics, `/repodrop test`, `/repodrop status`, and admin notices.
8. **Public launch:** bot listing, an invite link with minimal permissions (View Channel, Send Messages, Embed Links), and verification.

## 13. Future Work

- **GitHub App + FastAPI webhook service:** maintainers who install the app get instant, push-based updates, and their repos skip polling. This service can be added as a new entrypoint because the core is Discord-independent.
- **Web dashboard:** Discord OAuth login for managing subscriptions outside Discord, served by the same FastAPI service.
- **Per-subscription filters:** tag name patterns, commit path filters, and custom message templates.
- **Role pings:** an opt-in role mention per subscription.
- **Redis:** only if the bot splits into multiple shard processes that need a shared GitHub rate-limit budget or cross-process caching.
- **Multiple pollers:** use Postgres advisory locks or `SKIP LOCKED` on `repo_watches` so poller instances never double-poll a repo.
