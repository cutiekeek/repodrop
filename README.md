# repodrop

A Discord bot that watches public GitHub repositories and announces new releases, tags, and commits in subscribed channels. See [DESIGN.md](DESIGN.md) for the full design.

## Setup

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

- **Discord token:** create an application at <https://discord.com/developers/applications>. No privileged intents are needed. Invite it with the `bot` and `applications.commands` scopes and the View Channel, Send Messages, and Embed Links permissions.
- **GitHub token:** a fine-grained token with no extra permissions works (public repo read access only). It raises the rate limit from 60 to 5,000 requests/hour.
- Set `DEV_GUILD_ID` while developing so slash command changes show up instantly in that server.

## Commands

All under `/github`, with ephemeral replies. The group is visible to everyone; access is checked per command:

- A **manager** has Manage Server, or the server's manager role if one is set.
- **Subscribing** is open to everyone unless the server sets a subscriber role (managers always can).
- A subscription can be changed or removed by the member who added it, or a manager.


| Command | What it does |
|---|---|
| `/github subscribe repo [channel] [releases] [tags] [commits] [branches] [prereleases]` | Start announcing a repo (releases by default). Running it again for the same repo and channel updates it, changing only the options you pass: `commits:true` adds commits, `tags:false` removes tags, `branches:main,dev` sets the commit branches, `branches:default` follows the default branch. You need to be able to post in the target channel yourself (managers excepted). |
| `/github unsubscribe repo [channel]` | Stop announcing a repo in a channel. Your own subscriptions, or any as a manager. |
| `/github list [channel]` | Show this server's subscriptions. |
| `/github test repo [channel]` | Post the repo's latest release to check formatting and permissions. Managers only. |

## Development

```sh
uv run pytest                 # DB tests use a throwaway schema in DATABASE_URL (or TEST_DATABASE_URL); skipped if Postgres is down
uv run ruff check . && uv run ruff format .
uv run alembic revision --autogenerate -m "describe change"   # needs a running database
```

Layout (`src/repodrop/`): `github/`, `db/`, and `poller/` never import `discord`; only `bot/` and `announcer/` do.
