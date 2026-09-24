import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from repodrop.config import DatabaseSettings
from repodrop.db.models import Base
from repodrop.db.session import create_session_factory


def _database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL") or DatabaseSettings().database_url


@pytest.fixture
async def sessions():
    """A session factory bound to a throwaway schema, dropped after the test.

    Skips when Postgres isn't reachable.
    """
    url = _database_url()
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(url)
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    except OSError as exc:
        await admin.dispose()
        pytest.skip(f"Postgres not reachable: {exc}")

    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield create_session_factory(engine)
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
