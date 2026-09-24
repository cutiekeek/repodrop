# RepoDrop

A Discord bot that watches public GitHub repositories and announces new releases, tags, and commits in your server's channels.

## Add RepoDrop to your server

**[Invite RepoDrop](https://discord.com/oauth2/authorize?client_id=1552550763207856128&scope=bot+applications.commands&permissions=19456)**

You need the **Manage Server** permission in a server to add a bot to it. RepoDrop asks only for what it needs to post announcements:

| Permission | Why |
|---|---|
| View Channel | See the channels you subscribe |
| Send Messages | Post announcements |
| Embed Links | Show announcements as rich embeds with buttons |

It can't read your messages. It uses slash commands only, with no message-content access.

When it joins, RepoDrop posts a short getting-started message in your server's system channel (or the first channel it can post in).

If you remove RepoDrop, your server's subscriptions and settings are kept for **7 days**. Re-add it within that time and everything is restored; after that, they're deleted.

## Getting started

1. In the channel where announcements should appear, run:
   ```
   /repodrop subscribe repo:owner/name
   ```
   `repo` also accepts a GitHub URL. You get releases by default; add `tags:true` or `commits:true` for more.
2. RepoDrop records the repo's current state, so nothing old is re-announced. New releases are posted from now on, usually within 5–10 minutes (up to about an hour for very quiet repos).
3. Check your setup with `/repodrop list`, and preview how an announcement looks with `/repodrop test repo:owner/name`.

A few more examples:

```
/repodrop subscribe repo:owner/name commits:true branches:main,dev
/repodrop subscribe repo:owner/name tags:false          # change an existing subscription
/repodrop latest repo:owner/name                        # look up a release without subscribing
```

Server managers can open `/repodrop settings` to choose a manager role, limit subscribing to a role, set a default channel, switch to a compact announcement style, and control who can use `/repodrop latest`.

## Commands

All under `/repodrop`, with replies only you can see. Everyone can see the commands; access is checked when you use one:

- A **manager** has Manage Server, or the server's manager role if one is set.
- **Subscribing** is open to everyone unless the server sets a subscriber role (managers always can).
- A subscription can be changed or removed by the member who added it, or a manager.

| Command | What it does |
|---|---|
| `/repodrop subscribe repo [channel] [releases] [tags] [commits] [branches] [prereleases]` | Start announcing a repo (releases by default). Running it again for the same repo and channel updates it, changing only the options you pass: `commits:true` adds commits, `tags:false` removes tags, `branches:main,dev` sets the commit branches, `branches:default` follows the default branch. You need to be able to post in the target channel yourself (managers excepted). |
| `/repodrop unsubscribe repo [channel]` | Stop announcing a repo in a channel. Your own subscriptions, or any as a manager. |
| `/repodrop list [channel]` | Show this server's subscriptions. |
| `/repodrop status [channel]` | Health report: what each subscription posts, when it last posted and was checked, and any problems (missing permissions, deleted branches, disabled subscriptions). Managers only. |
| `/repodrop latest repo [public]` | Show a repo's newest release (or newest tag) with its buttons, no subscription needed. Only you see it unless you pass `public:true`. Servers can limit it to managers or turn off public posts. |
| `/repodrop settings` | Settings panel: manager role, subscriber role, default channel, embed style, and who can use `/repodrop latest`. Changes save immediately. Managers only; changing the manager role needs Manage Server. |
| `/repodrop test repo [channel]` | Post the repo's latest release to check formatting and permissions. Managers only. |

Announcements carry link buttons: **View release** / **View tag** / **View commits**, plus **Compare** against the previous version. Servers can choose a compact style (no release notes).

When a subscription is disabled automatically (the channel was deleted, the bot lost permission, or the repo was deleted or made private), or a followed branch is deleted, the bot posts one notice in the server's system channel if it can.

### Limits

Each server can follow up to **25 repos** across up to **100 subscriptions** (a subscription is one repo in one channel), and a commit subscription can follow up to **5 branches**. Only public repositories are supported.

---

## Running your own instance

Requires [uv](https://docs.astral.sh/uv/) and PostgreSQL. Create a role and database for the bot first:

```sql
CREATE ROLE repodrop LOGIN PASSWORD 'repodrop';
CREATE DATABASE repodrop OWNER repodrop;
```

```sh
uv sync                      # creates .venv and installs dependencies
cp .env.example .env         # then fill in DISCORD_TOKEN and GITHUB_TOKEN
uv run alembic upgrade head  # create the schema
uv run repodrop              # start the bot, poller, and announcer
```

- **Discord token:** create an application at <https://discord.com/developers/applications>. No privileged intents are needed. Invite it with the `bot` and `applications.commands` scopes and the View Channel, Send Messages, and Embed Links permissions (`permissions=19456`).
- **GitHub token:** a fine-grained token with no extra permissions works (public repo read access only). It raises the rate limit from 60 to 5,000 requests/hour.
- Set `DEV_GUILD_ID` to your own private server and `OWNER_IDS` to your Discord user ID: the `/repodrop-owner` commands are registered only there and only run for you. `/repodrop` is registered globally; set `DEV_SYNC=true` while developing to register it only in `DEV_GUILD_ID`, where changes show up instantly.

See [DESIGN.md](DESIGN.md) for the full design.

### Operator commands

Registered only in `DEV_GUILD_ID`, hidden there from members without Administrator, and only run for `OWNER_IDS`. Server IDs are passed as text.

| Command | What it does |
|---|---|
| `/repodrop-owner limits guild_id [max_repos] [max_subscriptions] [reset]` | Set a server's caps, or reset them to the defaults. Lowering a cap deletes nothing; it only blocks new subscriptions. |
| `/repodrop-owner commits guild_id allowed` | Allow or disallow commit announcements. Existing commit subscriptions are kept. |
| `/repodrop-owner block guild_id reason` | Every `/repodrop` command in that server shows the reason; pending announcements are skipped. Survives the bot being removed and re-invited. |
| `/repodrop-owner unblock guild_id` | Lift a block; subscriptions resume as they were. |
| `/repodrop-owner inspect guild_id` | Settings, usage, and recent delivery failures. |

## Development

```sh
uv run pytest                 # DB tests use a throwaway schema in DATABASE_URL (or TEST_DATABASE_URL); skipped if Postgres is down
uv run ruff check . && uv run ruff format .
uv run alembic revision --autogenerate -m "describe change"   # needs a running database
```

Layout (`src/repodrop/`): `github/`, `db/`, and `poller/` never import `discord`; only `bot/` and `announcer/` do.
