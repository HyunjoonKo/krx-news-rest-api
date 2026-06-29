from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import aiosqlite

from krx_news_api.config import settings
from krx_news_api.models.schemas import CrawlerStatus, Disclosure, NewsArticle, NewsCategory, NewsSource

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


# ---------------------------------------------------------------------------
# Articles
# ---------------------------------------------------------------------------


def _row_to_article(r) -> NewsArticle:
    return NewsArticle(
        id=r["id"], source=NewsSource(r["source"]),
        category=NewsCategory(r["category"]) if r["category"] else NewsCategory.MARKET,
        title=r["title"] or "", url=r["url"] or "", content=r["content"] or "",
        summary=r["summary"] or "", author=r["author"] or "",
        tickers=[], published_at=_parse_dt(r["published_at"]) or datetime.now(),
        collected_at=_parse_dt(r["collected_at"]) or datetime.now(),
    )


async def insert_articles(source: NewsSource, articles: list[NewsArticle]) -> int:
    if not articles:
        return 0
    conn = await get_db()
    inserted = 0
    async with _write_lock:
        for a in articles:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO articles "
                "(id,source,category,title,url,content,summary,author,published_at,collected_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.source.value, a.category.value, a.title, a.url, a.content,
                 a.summary, a.author, a.published_at.isoformat(), a.collected_at.isoformat()),
            )
            if cur.rowcount:
                inserted += 1
            for t in a.tickers:
                await conn.execute(
                    "INSERT OR IGNORE INTO article_tickers (article_id,ticker) VALUES (?,?)",
                    (a.id, t),
                )
        await conn.commit()
    return inserted


async def _load_article_tickers(conn, ids: list[str]) -> dict[str, list[str]]:
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    rows = await conn.execute_fetchall(
        f"SELECT article_id, ticker FROM article_tickers WHERE article_id IN ({q})", ids
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["article_id"], []).append(r["ticker"])
    return out


async def _hydrate_tickers(conn, items: list[NewsArticle]) -> None:
    tmap = await _load_article_tickers(conn, [it.id for it in items])
    for it in items:
        it.tickers = tmap.get(it.id, [])


async def get_articles(
    source: NewsSource | None, page: int, page_size: int
) -> tuple[list[NewsArticle], int]:
    conn = await get_db()
    offset = (page - 1) * page_size
    if source is not None:
        sv = source.value
        total_row = await (await conn.execute(
            "SELECT COUNT(*) c FROM articles WHERE source=?", (sv,))).fetchone()
        rows = await conn.execute_fetchall(
            "SELECT * FROM articles WHERE source=? ORDER BY published_at DESC LIMIT ? OFFSET ?",
            (sv, page_size, offset))
    else:
        total_row = await (await conn.execute("SELECT COUNT(*) c FROM articles")).fetchone()
        rows = await conn.execute_fetchall(
            "SELECT * FROM articles ORDER BY published_at DESC LIMIT ? OFFSET ?",
            (page_size, offset))
    total = total_row["c"]
    items = [_row_to_article(r) for r in rows]
    await _hydrate_tickers(conn, items)
    return items, total


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def search_articles(query: str, page: int, page_size: int) -> tuple[list[NewsArticle], int]:
    conn = await get_db()
    offset = (page - 1) * page_size
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    total_row = await (await conn.execute(
        "SELECT COUNT(*) c FROM articles WHERE title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\'",
        (like, like))).fetchone()
    rows = await conn.execute_fetchall(
        "SELECT * FROM articles WHERE title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' "
        "ORDER BY published_at DESC LIMIT ? OFFSET ?",
        (like, like, page_size, offset))
    items = [_row_to_article(r) for r in rows]
    await _hydrate_tickers(conn, items)
    return items, total_row["c"]


# ---------------------------------------------------------------------------
# Disclosures
# ---------------------------------------------------------------------------


def _row_to_disclosure(r) -> Disclosure:
    return Disclosure(
        id=r["id"], source=NewsSource(r["source"]), title=r["title"] or "",
        url=r["url"] or "", company=r["company"] or "", ticker=r["ticker"] or "",
        disclosure_type=r["disclosure_type"] or "",
        published_at=_parse_dt(r["published_at"]) or datetime.now(),
        collected_at=_parse_dt(r["collected_at"]) or datetime.now())


async def insert_disclosures(source: NewsSource, disclosures: list[Disclosure]) -> int:
    if not disclosures:
        return 0
    conn = await get_db()
    inserted = 0
    async with _write_lock:
        for d in disclosures:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO disclosures "
                "(id,source,title,url,company,ticker,disclosure_type,published_at,collected_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (d.id, d.source.value, d.title, d.url, d.company, d.ticker,
                 d.disclosure_type, d.published_at.isoformat(), d.collected_at.isoformat()))
            if cur.rowcount:
                inserted += 1
        await conn.commit()
    return inserted


async def get_disclosures(
    source: NewsSource | None, ticker: str | None, page: int, page_size: int
) -> tuple[list[Disclosure], int]:
    conn = await get_db()
    offset = (page - 1) * page_size
    where, params = [], []
    if source is not None:
        where.append("source=?"); params.append(source.value)
    if ticker:
        where.append("ticker=?"); params.append(ticker)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total_row = await (await conn.execute(
        f"SELECT COUNT(*) c FROM disclosures{clause}", params)).fetchone()
    rows = await conn.execute_fetchall(
        f"SELECT * FROM disclosures{clause} ORDER BY published_at DESC LIMIT ? OFFSET ?",
        (*params, page_size, offset))
    return [_row_to_disclosure(r) for r in rows], total_row["c"]


# ---------------------------------------------------------------------------
# Crawler status
# ---------------------------------------------------------------------------


async def update_crawler_status(source: NewsSource, count: int = 0, error: str | None = None) -> None:
    now = datetime.now().isoformat()
    healthy = 0 if error else 1
    conn = await get_db()
    async with _write_lock:
        await conn.execute(
            "INSERT INTO crawler_status (source,last_crawled_at,articles_count,is_healthy,error) "
            "VALUES (?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
            "last_crawled_at=excluded.last_crawled_at, articles_count=excluded.articles_count, "
            "is_healthy=excluded.is_healthy, error=excluded.error",
            (source.value, now, count, healthy, error))
        await conn.commit()


async def get_all_crawler_status() -> list[CrawlerStatus]:
    conn = await get_db()
    rows = await conn.execute_fetchall("SELECT * FROM crawler_status")
    return [CrawlerStatus(
        source=NewsSource(r["source"]), last_crawled_at=_parse_dt(r["last_crawled_at"]),
        articles_count=r["articles_count"] or 0, is_healthy=bool(r["is_healthy"]),
        error=r["error"]) for r in rows]
