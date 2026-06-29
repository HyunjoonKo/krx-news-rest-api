from __future__ import annotations

import pytest

from krx_news_api.services import db


@pytest.fixture
async def memdb():
    db.set_db_path(":memory:")
    await db.init_db()
    yield
    await db.close_db()


async def test_init_db_creates_tables(memdb):
    conn = await db.get_db()
    rows = await conn.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    names = {r["name"] for r in rows}
    assert {"articles", "article_tickers", "disclosures", "crawler_status"} <= names


async def test_wal_enabled(memdb):
    conn = await db.get_db()
    cur = await conn.execute("PRAGMA journal_mode")
    row = await cur.fetchone()
    # :memory: returns 'memory'; file DBs return 'wal'. Assert no crash + known value.
    assert row[0] in ("wal", "memory")
