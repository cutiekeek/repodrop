"""The other tests build their tables from the models; production builds them with Alembic.

This runs every migration into an empty schema and checks the result is what the models
describe, so a model change without a migration (or a broken migration) fails here.
"""

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from alembic import command
from repodrop.db.models import Base

ALEMBIC_DIR = Path(__file__).parent.parent / "alembic"


async def test_migrations_build_the_schema_the_models_describe(engine):
    # No .ini file: env.py would use it to reconfigure logging for the whole test run.
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))

    def upgrade_then_compare(connection) -> list:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        return compare_metadata(MigrationContext.configure(connection), Base.metadata)

    async with engine.begin() as conn:
        differences = await conn.run_sync(upgrade_then_compare)
    assert differences == []
