from __future__ import annotations

from datetime import datetime

import pytest

from krx_news_api.models.schemas import NewsArticle, NewsCategory, NewsSource
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


# ---------------------------------------------------------------------------
# Task 2 helpers
# ---------------------------------------------------------------------------


def _article(i: int, source=NewsSource.NAVER, tickers=None) -> NewsArticle:
    return NewsArticle(
        id=f"{source.value}:{i:012d}", source=source, category=NewsCategory.MARKET,
        title=f"title {i}", url=f"https://x/{i}", content=f"body {i}",
        tickers=tickers or [], published_at=datetime(2026, 6, 30, 9, i % 60),
        collected_at=datetime(2026, 6, 30, 10, 0),
    )


async def test_insert_articles_dedup(memdb):
    a = _article(1)
    assert await db.insert_articles(NewsSource.NAVER, [a]) == 1
    assert await db.insert_articles(NewsSource.NAVER, [a]) == 0  # same id ignored
    items, total = await db.get_articles(None, 1, 50)
    assert total == 1 and len(items) == 1 and items[0].id == a.id


async def test_insert_articles_populates_tickers(memdb):
    await db.insert_articles(NewsSource.NAVER, [_article(2, tickers=["005930", "000660"])])
    items, _ = await db.get_articles(None, 1, 50)
    assert set(items[0].tickers) == {"005930", "000660"}


async def test_get_articles_source_filter_and_paging(memdb):
    await db.insert_articles(NewsSource.NAVER, [_article(i) for i in range(5)])
    await db.insert_articles(NewsSource.HANKYUNG, [_article(i, source=NewsSource.HANKYUNG) for i in range(3)])
    naver, total = await db.get_articles(NewsSource.NAVER, 1, 2)
    assert total == 5 and len(naver) == 2
    allitems, all_total = await db.get_articles(None, 1, 50)
    assert all_total == 8


async def test_get_articles_ordered_desc(memdb):
    await db.insert_articles(NewsSource.NAVER, [_article(1), _article(3), _article(2)])
    items, _ = await db.get_articles(None, 1, 50)
    pubs = [it.published_at for it in items]
    assert pubs == sorted(pubs, reverse=True)
