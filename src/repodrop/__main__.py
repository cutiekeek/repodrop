"""Entrypoint: configure Logfire, then run the bot (which starts the poller and announcer)."""

from repodrop import observability
from repodrop.bot.client import RepoDropBot
from repodrop.config import get_settings


def main() -> None:
    settings = get_settings()
    observability.configure(settings)
    bot = RepoDropBot(settings)
    # log_handler=None: logging is already routed to Logfire by observability.configure.
    bot.run(settings.discord_token.get_secret_value(), log_handler=None)


if __name__ == "__main__":
    main()
