# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.


> **⚠️ 重要：修改完代码之后，自动commit相关修改的代码，然后 push**

> **⚠️ 重要：git commit 时不要添加 Co-Authored-By 行**


## Project Overview

WeChat Official Account (MP) RSS subscription tool. A monolithic **FastAPI + Vue 3** app that scrapes WeChat articles via Playwright browser automation and serves them as RSS feeds. Supports distributed scraping via a parent-child cascade system.

## Build & Development Commands

### Backend
```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # edit with your settings
cp .env.example .env                 # edit credentials
python main.py -job True -init True  # full startup (server + jobs + DB init)
python main.py -job False            # server only, no scheduled jobs
```

### Frontend
```bash
cd web_ui
npm install
npm run dev      # dev server at http://localhost:3000
npm run build    # production build → static/
```

### Docker
```bash
docker compose -f compose/docker-compose.dev.yaml up -d --force-recreate  # dev
docker compose -f compose/docker-compose.yaml up -d                       # prod (MySQL)
docker compose -f compose/docker-compose-sqlite.yaml up -d                # prod (SQLite)
```

### Testing
```bash
cd core/lax && python -m unittest test_template_parser.py   # only existing unit test
# Smoke test: start `python main.py` and exercise affected endpoints/UI
# Frontend: `npm run build` must pass
```

## Architecture

**Entry points:** `main.py` → `web.py` (FastAPI app with CORS, AK middleware, router registration).

**Backend layers:**
- `core/` — Business logic: config, DB (SQLAlchemy), auth (JWT/OAuth2), RSS generation, cascade system, models, notifications, caching
- `apis/` — FastAPI route handlers (REST endpoints)
- `views/` — Server-side rendered HTML pages (legacy)
- `jobs/` — APScheduler background tasks: article fetching, cascade sync, webhooks
- `driver/` — Playwright browser automation for WeChat login/scraping, anti-crawler scripts

**Frontend:** `web_ui/` — Vue 3 + Vite SPA, Arco Design + Ant Design Vue, TypeScript. API modules in `src/api/`, views in `src/views/` (PascalCase), composables in `src/utils/`.

**Data flow:** WeChat QR auth (driver/wx.py) → scheduled scraping (jobs/mps.py) → SQLAlchemy models (core/models/) → REST APIs (apis/) → Vue frontend or RSS feed (core/rss.py).

**Cascade system:** Parent nodes distribute scraping tasks to child nodes via `core/cascade.py` and `jobs/cascade_task_dispatcher.py`. Child nodes poll for tasks via `jobs/cascade_sync.py`.

**Database:** SQLAlchemy 2.0 supporting SQLite (default), MySQL, PostgreSQL. Models in `core/models/`.

## Code Style

- Python: 4-space indent, snake_case, feature-grouped folders
- Vue: PascalCase filenames in `views/`, camelCase in `utils/` and `api/`
- No enforced formatter — match surrounding conventions
- Commit style: Angular-style prefixes (`feat:`, `fix:`, etc.)

## Key Configuration

- `config.yaml` — Main app config (server, DB, RSS, webhooks, cascade). Never commit this file.
- `.env` — Credentials and runtime settings. Never commit.
- Redis is optional (in-memory fallback exists)
- For datacenter IP issues, use the Sing-Box proxy sidecar via `PROXY_URL` in `.env`

## Important Constraints

- Do not commit `config.yaml`, `.env`, tokens, cookies, or anything in `data/`
- Keep `origin` pointed at fork, `upstream` at `https://github.com/rachelos/we-mp-rss`
- Review `SECURITY.md` before changing auth, webhooks, or access-key flows
