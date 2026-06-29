from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from krx_news_api.models.schemas import Disclosure, NewsArticle, NewsCategory, NewsSource
from krx_news_api.services import db
from krx_news_api.services import scheduler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def memdb():
    """In-memory SQLite DB — mirrors the fixture in test_db.py."""
    db.set_db_path(":memory:")
    await db.init_db()
    yield
    await db.close_db()


def _article(i: int, source: NewsSource = NewsSource.NAVER) -> NewsArticle:
    return NewsArticle(
        id=f"{source.value}:{i:012d}",
        source=source,
        category=NewsCategory.MARKET,
        title=f"title {i}",
        url=f"https://x/{i}",
        content=f"body {i}",
        tickers=[],
        published_at=datetime(2026, 6, 30, 9, i % 60),
        collected_at=datetime(2026, 6, 30, 10, 0),
    )


def _disc(i: int, source: NewsSource = NewsSource.DART) -> Disclosure:
    return Disclosure(
        id=f"{source.value}:{i:012d}",
        source=source,
        title=f"d{i}",
        url=f"https://d/{i}",
        company="삼성전자",
        ticker="005930",
        disclosure_type="정기공시",
        published_at=datetime(2026, 6, 30, 9, i % 60),
        collected_at=datetime(2026, 6, 30, 10, 0),
    )


def _make_mock_scraper(
    source: NewsSource,
    articles: list[NewsArticle] | None = None,
    disclosures: list[Disclosure] | None = None,
) -> MagicMock:
    """Build a scraper stub that returns the provided data without network I/O."""
    scraper = MagicMock()
    scraper.source = source
    scraper.scrape_news = AsyncMock(return_value=articles or [])
    scraper.scrape_disclosures = AsyncMock(return_value=disclosures or [])
    scraper.close = AsyncMock()
    return scraper


# ---------------------------------------------------------------------------
# Seam note
# ---------------------------------------------------------------------------
# The autouse `mock_scheduler` in conftest patches `scheduler.crawl_all_news`
# and `scheduler.crawl_all_disclosures` at the module attribute level, but
# does NOT patch `crawl_source`.  We therefore call `crawl_source(source)`
# directly, which exercises the full insert path, and monkeypatch
# `scheduler._scrapers` so no real HTTP requests are made.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crawl_news_persists_to_db(memdb, monkeypatch):
    """crawl_source for a news source inserts articles into SQLite."""
    articles = [_article(1), _article(2)]
    mock = _make_mock_scraper(NewsSource.NAVER, articles=articles)
    monkeypatch.setattr(scheduler, "_scrapers", {NewsSource.NAVER: mock})

    await scheduler.crawl_source(NewsSource.NAVER)

    items, total = await db.get_articles(NewsSource.NAVER, 1, 50)
    assert total == 2
    assert {it.id for it in items} == {a.id for a in articles}


@pytest.mark.asyncio
async def test_crawl_disclosures_persists_to_db(memdb, monkeypatch):
    """crawl_source for a disclosure source inserts disclosures into SQLite."""
    discs = [_disc(1), _disc(2), _disc(3)]
    mock = _make_mock_scraper(NewsSource.DART, disclosures=discs)
    monkeypatch.setattr(scheduler, "_scrapers", {NewsSource.DART: mock})

    await scheduler.crawl_source(NewsSource.DART)

    items, total = await db.get_disclosures(None, None, 1, 50)
    assert total == 3
    assert {it.id for it in items} == {d.id for d in discs}


@pytest.mark.asyncio
async def test_crawl_updates_crawler_status(memdb, monkeypatch):
    """After a successful crawl, crawler_status reflects the source with correct count."""
    articles = [_article(1), _article(2)]
    mock = _make_mock_scraper(NewsSource.NAVER, articles=articles)
    monkeypatch.setattr(scheduler, "_scrapers", {NewsSource.NAVER: mock})

    await scheduler.crawl_source(NewsSource.NAVER)

    statuses = await db.get_all_crawler_status()
    by_source = {s.source: s for s in statuses}
    assert NewsSource.NAVER in by_source
    s = by_source[NewsSource.NAVER]
    assert s.is_healthy is True
    assert s.articles_count == 2


@pytest.mark.asyncio
async def test_crawl_error_sets_unhealthy_status(memdb, monkeypatch):
    """When a scraper raises, update_crawler_status is called with error=."""
    mock = _make_mock_scraper(NewsSource.NAVER)
    mock.scrape_news = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(scheduler, "_scrapers", {NewsSource.NAVER: mock})

    # crawl_source must not raise — it catches internally
    await scheduler.crawl_source(NewsSource.NAVER)

    statuses = await db.get_all_crawler_status()
    by_source = {s.source: s for s in statuses}
    assert NewsSource.NAVER in by_source
    s = by_source[NewsSource.NAVER]
    assert s.is_healthy is False
    assert s.error is not None and "boom" in s.error
