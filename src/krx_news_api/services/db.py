from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import aiosqlite

from krx_news_api.config import settings

logger = logging.getLogger(__name__)

_conn: aiosqlite.Connection | None = None
_db_path: str | None = None
_write_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY,
  source TEXT, category TEXT, title TEXT, url TEXT,
  content TEXT, summary TEXT, author TEXT,
  published_at TEXT, collected_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source, published_at);

CREATE TABLE IF NOT EXISTS article_tickers (
  article_id TEXT, ticker TEXT,
  PRIMARY KEY (article_id, ticker),
  FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_article_tickers_ticker ON article_tickers(ticker);

CREATE TABLE IF NOT EXISTS disclosures (
  id TEXT PRIMARY KEY, source TEXT, title TEXT, url TEXT,
  company TEXT, ticker TEXT, disclosure_type TEXT,
  published_at TEXT, collected_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_disclosures_ticker ON disclosures(ticker, published_at);
CREATE INDEX IF NOT EXISTS idx_disclosures_published ON disclosures(published_at);

CREATE TABLE IF NOT EXISTS crawler_status (
  source TEXT PRIMARY KEY, last_crawled_at TEXT,
  articles_count INTEGER, is_healthy INTEGER, error TEXT
);
"""


def set_db_path(path: str) -> None:
    global _db_path, _conn
    _db_path = path
    _conn = None


async def get_db() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        async with _write_lock:
            if _conn is None:
                path = _db_path or settings.db_path
                conn = await aiosqlite.connect(path)
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA foreign_keys=ON")
                _conn = conn
    return _conn


async def init_db() -> None:
    conn = await get_db()
    await conn.executescript(SCHEMA)


async def close_db() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
