# AyrNews — Ayrshire News Aggregator

Local news aggregator for Ayrshire, Scotland. Curates articles from BBC Scotland, The Guardian, STV News, Daily Record, Sky News and more.

**Live:** https://news.ayrshire.wispayr.online

## Features

- 📰 Aggregates RSS feeds from major Scottish & UK news sources
- 🏠 Automatically detects Ayrshire-specific articles (keyword matching)
- 🗃️ SQLite caching with 15-minute auto-refresh
- 🌙 Dark/light theme
- 📱 Mobile responsive
- 🔍 Search & category filtering (All / Local / Scotland / UK)
- ♻️ Frontend auto-refreshes every 5 minutes
- 💓 nuro fleet integration (heartbeat endpoint)

## Tech Stack

- **Backend:** FastAPI + uvicorn
- **RSS:** feedparser + httpx (async)
- **Cache:** SQLite (WAL mode)
- **Frontend:** Vanilla HTML/CSS/JS (single-file SPA)

## Quick Start

```bash
pip install -r requirements.txt
python server.py
# → http://localhost:3877
```

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/articles` | GET | List articles (query: `category`, `search`, `limit`, `offset`) |
| `/api/stats` | GET | Article counts by category, source breakdown |
| `/api/health` | GET | Health check |
| `/api/nuro/heartbeat` | GET | nuro fleet dashboard integration |
| `/api/refresh` | POST | Trigger manual feed refresh |

## PM2 Deployment

```bash
cp -r . /opt/ayrnews
cd /opt/ayrnews
pip install -r requirements.txt
pm2 start ecosystem.config.cjs
pm2 save
```

## Architecture

```
server.py          — FastAPI app, RSS fetcher, SQLite cache
static/index.html  — Single-file frontend (no build step)
data/ayrnews.db    — SQLite database (auto-created)
```

## Part of WispAyr

https://ayrshire.wispayr.online
