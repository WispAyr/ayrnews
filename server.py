"""
AyrNews - Local news aggregator for Ayrshire, Scotland
FastAPI backend: RSS aggregation, SQLite caching, static frontend
Port: 3877
"""

import asyncio
import hashlib
import logging
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import feedparser
import httpx
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ayrnews")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = 3877
DB_PATH = Path(__file__).parent / "data" / "ayrnews.db"
REFRESH_INTERVAL = 15 * 60  # 15 minutes
NURO_URL = "http://192.168.195.33:3960"

RSS_FEEDS = [
    {"name": "BBC Scotland", "url": "https://feeds.bbci.co.uk/news/scotland/rss.xml",
     "website": "https://www.bbc.co.uk/news/scotland", "category": "scotland", "author": "BBC News"},
    {"name": "The Guardian Scotland", "url": "https://www.theguardian.com/uk/scotland/rss",
     "website": "https://www.theguardian.com/uk/scotland", "category": "scotland", "author": "The Guardian"},
    {"name": "Sky News UK", "url": "http://feeds.skynews.com/feeds/rss/uk.xml",
     "website": "https://news.sky.com/uk", "category": "uk", "author": "Sky News"},
    {"name": "Daily Record", "url": "https://www.dailyrecord.co.uk/?service=rss",
     "website": "https://www.dailyrecord.co.uk", "category": "scotland", "author": "Daily Record"},
    {"name": "Daily Record Scottish News", "url": "https://www.dailyrecord.co.uk/news/scottish-news/?service=rss",
     "website": "https://www.dailyrecord.co.uk/news/scottish-news", "category": "scotland", "author": "Daily Record"},
    {"name": "STV News", "url": "https://news.stv.tv/feed",
     "website": "https://news.stv.tv", "category": "scotland", "author": "STV News"},
]

# Ayrshire keyword matching
AYR_KEYWORDS = [
    "ayr", "ayrshire", "prestwick", "troon", "kilmarnock", "irvine",
    "arran", "auchinleck", "maybole", "girvan", "south ayrshire",
    "north ayrshire", "east ayrshire", "ayrshire coast", "carrick",
    "cumnock", "darvel", "galston", "stewarton", "tarbolton",
    "largs", "west kilbride", "ardrossan", "saltcoats", "stevenston",
    "ayr hospital", "ayr racecourse", "ayr united",
    "university of the west of scotland",
]

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            link TEXT NOT NULL UNIQUE,
            summary TEXT,
            source TEXT,
            source_url TEXT,
            author TEXT,
            category TEXT DEFAULT 'scotland',
            is_local INTEGER DEFAULT 0,
            image_url TEXT,
            published_at TEXT,
            fetched_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category);
        CREATE INDEX IF NOT EXISTS idx_articles_local ON articles(is_local);

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialised")


def set_meta(key: str, value: str):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
    conn.commit()
    conn.close()


def get_meta(key: str) -> Optional[str]:
    conn = get_db()
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


# ---------------------------------------------------------------------------
# RSS fetching & filtering
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()


def is_ayr_related(title: str, summary: str) -> bool:
    combined = (title + " " + summary).lower()
    return any(kw in combined for kw in AYR_KEYWORDS)


def extract_image(entry) -> Optional[str]:
    # media:thumbnail
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        thumbs = entry.media_thumbnail if isinstance(entry.media_thumbnail, list) else [entry.media_thumbnail]
        if thumbs:
            url = thumbs[0].get("url", "")
            if "ichef.bbci.co.uk" in url and "/240/" in url:
                url = url.replace("/240/", "/976/")
            return url
    # media:content
    if hasattr(entry, "media_content"):
        for m in entry.media_content:
            if m.get("type", "").startswith("image/"):
                return m.get("url")
    # enclosures
    if hasattr(entry, "enclosures"):
        for enc in entry.enclosures:
            if enc.get("type", "").startswith("image/"):
                return enc.get("href")
    return None


async def fetch_single_feed(feed_cfg: dict) -> list[dict]:
    articles = []
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(
                feed_cfg["url"],
                headers={"User-Agent": "AyrNews/1.0 (news aggregator for Ayrshire)"},
            )
            if resp.status_code != 200:
                logger.warning(f"Feed {feed_cfg['name']}: HTTP {resp.status_code}")
                return []

        feed = feedparser.parse(resp.text)
        if not feed.entries:
            return []

        for entry in feed.entries[:25]:
            title = entry.get("title", "Untitled") or "Untitled"
            summary_raw = entry.get("summary", "") or entry.get("description", "") or ""
            summary = strip_html(summary_raw)[:500]
            link = entry.get("link", "") or ""
            if not link:
                continue

            local = is_ayr_related(title, summary)
            category = "local" if local else feed_cfg.get("category", "scotland")

            # Parse published date
            published = None
            parsed_time = entry.get("published_parsed") or entry.get("updated_parsed")
            if parsed_time:
                try:
                    published = datetime(*parsed_time[:6], tzinfo=timezone.utc).isoformat()
                except Exception:
                    pass
            if not published:
                published = datetime.now(timezone.utc).isoformat()

            article_id = hashlib.sha256(link.encode()).hexdigest()[:16]

            articles.append({
                "id": article_id,
                "title": title,
                "link": link,
                "summary": summary if summary else "No summary available.",
                "source": feed_cfg["name"],
                "source_url": feed_cfg.get("website", ""),
                "author": entry.get("author", feed_cfg.get("author", feed_cfg["name"])),
                "category": category,
                "is_local": 1 if local else 0,
                "image_url": extract_image(entry),
                "published_at": published,
            })

        logger.info(f"Parsed {len(articles)} articles from {feed_cfg['name']}")
    except Exception as e:
        logger.error(f"Error fetching {feed_cfg['name']}: {e}")
    return articles


async def refresh_feeds():
    """Fetch all feeds and upsert into SQLite."""
    logger.info("Refreshing feeds...")
    all_articles: list[dict] = []

    tasks = [fetch_single_feed(f) for f in RSS_FEEDS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, list):
            all_articles.extend(r)

    if not all_articles:
        logger.warning("No articles fetched from any feed")
        set_meta("last_refresh", datetime.now(timezone.utc).isoformat())
        return

    conn = get_db()
    inserted = 0
    for a in all_articles:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO articles(id, title, link, summary, source, source_url,
                    author, category, is_local, image_url, published_at, fetched_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                a["id"], a["title"], a["link"], a["summary"], a["source"],
                a["source_url"], a["author"], a["category"], a["is_local"],
                a["image_url"], a["published_at"],
                datetime.now(timezone.utc).isoformat(),
            ))
            if conn.total_changes:
                inserted += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()

    # Prune old articles (keep last 7 days worth, max 500)
    conn.execute("""
        DELETE FROM articles WHERE id NOT IN (
            SELECT id FROM articles ORDER BY published_at DESC LIMIT 500
        )
    """)
    conn.commit()
    conn.close()

    set_meta("last_refresh", datetime.now(timezone.utc).isoformat())
    logger.info(f"Feed refresh complete. {inserted} new articles inserted, {len(all_articles)} total parsed.")


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def periodic_refresh():
    """Refresh feeds every REFRESH_INTERVAL seconds."""
    while True:
        try:
            await refresh_feeds()
        except Exception as e:
            logger.error(f"Periodic refresh error: {e}")
        await asyncio.sleep(REFRESH_INTERVAL)


async def nuro_heartbeat():
    """Send heartbeat to nuro hub every 30s."""
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(f"{NURO_URL}/api/heartbeat", json={
                    "service": "AyrNews",
                    "version": "1.0",
                    "icon": "📰",
                    "port": PORT,
                    "endpoint": f"http://192.168.195.33:{PORT}/api/nuro/heartbeat",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
        except Exception:
            pass
        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Initial fetch
    await refresh_feeds()
    # Start background tasks
    refresh_task = asyncio.create_task(periodic_refresh())
    heartbeat_task = asyncio.create_task(nuro_heartbeat())
    logger.info(f"AyrNews started on port {PORT}")
    yield
    refresh_task.cancel()
    heartbeat_task.cancel()


app = FastAPI(title="AyrNews", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/api/articles")
async def get_articles(
    category: Optional[str] = Query(None, description="Filter: all, local, scotland, uk"),
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    conn = get_db()
    conditions = []
    params: list = []

    if category and category != "all":
        if category == "local":
            conditions.append("is_local = 1")
        else:
            conditions.append("category = ?")
            params.append(category)

    if search:
        conditions.append("(title LIKE ? OR summary LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = conn.execute(
        f"SELECT * FROM articles{where} ORDER BY published_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    total = conn.execute(f"SELECT COUNT(*) as cnt FROM articles{where}", params).fetchone()["cnt"]
    conn.close()

    articles = [dict(r) for r in rows]
    return JSONResponse({"articles": articles, "total": total})


@app.get("/api/stats")
async def get_stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) as cnt FROM articles").fetchone()["cnt"]
    local = conn.execute("SELECT COUNT(*) as cnt FROM articles WHERE is_local = 1").fetchone()["cnt"]
    scotland = conn.execute("SELECT COUNT(*) as cnt FROM articles WHERE category = 'scotland'").fetchone()["cnt"]
    uk = conn.execute("SELECT COUNT(*) as cnt FROM articles WHERE category = 'uk'").fetchone()["cnt"]

    sources = conn.execute(
        "SELECT source, COUNT(*) as cnt FROM articles GROUP BY source ORDER BY cnt DESC"
    ).fetchall()
    conn.close()

    return JSONResponse({
        "total": total,
        "local": local,
        "scotland": scotland,
        "uk": uk,
        "sources": [{"name": s["source"], "count": s["cnt"]} for s in sources],
        "last_refresh": get_meta("last_refresh"),
    })


@app.get("/api/health")
async def health():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as cnt FROM articles").fetchone()["cnt"]
    conn.close()
    return JSONResponse({
        "status": "healthy",
        "service": "AyrNews",
        "version": "1.0.0",
        "article_count": count,
        "last_refresh": get_meta("last_refresh"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/nuro/heartbeat")
async def nuro_heartbeat_endpoint():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as cnt FROM articles").fetchone()["cnt"]
    local_count = conn.execute("SELECT COUNT(*) as cnt FROM articles WHERE is_local = 1").fetchone()["cnt"]
    conn.close()

    return JSONResponse({
        "version": "2.0",
        "label": "AyrNews",
        "icon": "📰",
        "description": "Ayrshire news aggregator — local & Scotland news",
        "streams": [
            {"id": "total", "label": "Total Articles", "type": "stat", "value": count, "unit": "", "color": "#3b82f6"},
            {"id": "local", "label": "Local (Ayrshire)", "type": "stat", "value": local_count, "unit": "", "color": "#10b981"},
        ],
        "status": "healthy",
        "article_count": count,
        "local_count": local_count,
        "last_refresh": get_meta("last_refresh"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


@app.post("/api/refresh")
async def manual_refresh():
    """Trigger a manual feed refresh."""
    await refresh_feeds()
    return JSONResponse({"status": "refreshed", "timestamp": datetime.now(timezone.utc).isoformat()})


# Mount static files (must be last)
app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
