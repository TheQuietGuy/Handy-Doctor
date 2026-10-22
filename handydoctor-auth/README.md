# 🤚 HandyDoctor

> AI-powered medical triage grounded in NHS, NICE, and WHO clinical guidelines.

[![CI](https://github.com/your-org/handydoctor/actions/workflows/ci.yml/badge.svg)](https://github.com/your-org/handydoctor/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/your-org/handydoctor/branch/main/graph/badge.svg)](https://codecov.io/gh/your-org/handydoctor)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

HandyDoctor is a full-stack medical triage web application. Users describe their symptoms in plain English; a RAG (Retrieval-Augmented Generation) pipeline retrieves the most relevant passages from indexed NHS, NICE, and WHO clinical guidelines, then generates a cited, severity-tagged response. Every response includes a triage badge (✅ Self-care · ⚠️ See a GP · 🚨 Call 999) and links to the source guidelines.

---

## Contents

- [Architecture](#architecture)
- [Quick start (development)](#quick-start-development)
- [Project structure](#project-structure)
- [Core components](#core-components)
- [Configuration reference](#configuration-reference)
- [Database migrations](#database-migrations)
- [Running tests](#running-tests)
- [Indexing NHS guidelines](#indexing-nhs-guidelines)
- [Production deployment](#production-deployment)
- [API reference](#api-reference)
- [Medical disclaimer](#medical-disclaimer)

---

## Architecture

```
Browser (SPA)
     │
     ▼
  Nginx  ──── Let's Encrypt TLS
     │        Static HTML serving
     │        Connection-level rate limiting
     │
     ▼
FastAPI (Python 3.12)
     │
     ├── Auth          RS256 JWT · bcrypt · Google OAuth 2.0 PKCE
     ├── Chat API      Session CRUD · RAG pipeline
     ├── Admin API     Dashboard · user management · audit log
     ├── Rate limiter  Sliding-window per-IP/per-user (Redis)
     ├── GDPR worker   APScheduler · deletion grace period
     └── Notif worker  Session summaries · follow-up reminders
          │
          ├── PostgreSQL 16   Primary database (8 tables, 5 migrations)
          ├── Redis 7         Rate limiting · refresh token store
          ├── Chroma          Vector store (optional — MockRetriever fallback)
          └── Resend          Transactional email delivery
```

### RAG pipeline

```
User query
  │
  ├─→ Emergency keyword check (instant 999 response, no LLM)
  │
  ├─→ Retrieval: embed query → top-k cosine search in Chroma
  │         (falls back to keyword-scored MockRetriever)
  │
  ├─→ Augment: build prompt with retrieved NHS/NICE/WHO passages
  │
  ├─→ Generate: GPT-4o-mini (falls back to mock generator)
  │
  └─→ Classify: triage severity from response content → badge
```

---

## Quick start (development)

**Prerequisites:** Python 3.12, Docker Compose v2, openssl

```bash
git clone https://github.com/your-org/handydoctor.git
cd handydoctor

# Install dependencies and dev tools
make install

# Generate RSA-2048 key pair for JWT signing
make keys

# Copy and configure environment
cp .env.example .env
# The defaults work for local Docker development — no changes needed for first run

# Start Postgres + Redis + API (hot-reload)
make up

# Verify
curl http://localhost:8000/health
# → {"status":"ok","version":"1.0.0"}
```

API docs: `http://localhost:8000/docs`

Open `handydoc_frontend.html` in your browser — it connects to `http://localhost:8000` by default. To change the backend URL, edit `API_BASE` at the top of the `<script>` block.

---

## Project structure

```
handydoctor/
├── app/
│   ├── auth/               JWT, OAuth, dependencies, password hashing
│   ├── chat/               ChatSession & ChatMessage models, RAG pipeline
│   │   └── ai/             pipeline.py, retrieval.py, triage.py
│   ├── email/              Transactional email via Resend
│   ├── gdpr/               GDPR deletion worker + APScheduler
│   ├── middleware/         Sliding-window rate limiting
│   ├── notifications/      Session summary & follow-up email worker
│   ├── routers/            accounts.py, admin.py
│   ├── audit.py            AuditLog model + AuditEvent enum
│   ├── config.py           Pydantic Settings (reads .env)
│   ├── database.py         SQLAlchemy async engine
│   ├── logging_config.py   Structured JSON / coloured dev logging
│   ├── models.py           User, tokens, NotificationPrefs
│   └── schemas.py          Public Pydantic schemas
├── scripts/
│   ├── ingest/             NHS guideline ingestion pipeline
│   │   ├── sources.py      26 curated NHS/NICE/WHO sources
│   │   ├── fetcher.py      Async HTTP fetcher
│   │   ├── parser.py       HTML → clean text
│   │   ├── chunker.py      Overlapping paragraph chunker
│   │   ├── embedder.py     OpenAI text-embedding-3-small + TF-IDF fallback
│   │   ├── store.py        Chroma vector store wrapper
│   │   └── run.py          CLI entry point
│   ├── backup.sh           pg_dump + S3 upload + 7-day retention
│   ├── deploy.sh           Zero-downtime rolling deployment
│   ├── generate-keys.sh    RSA key pair generation
│   └── setup.sh            First-run production setup wizard
├── migrations/versions/    0001 → 0005 Alembic migrations
├── tests/                  217 tests, SQLite in-memory, no external services
├── nginx/nginx.conf         Production Nginx config (TLS, CSP, rate limits)
├── .github/workflows/      ci.yml + deploy.yml
├── docker-compose.yml       Development stack
├── docker-compose.prod.yml  Production stack
├── Dockerfile               Multi-stage production build
├── Makefile                 Convenient local commands
└── handydoc_frontend.html   Single-file SPA frontend
```

---

## Core components

### Authentication (`app/routers/accounts.py`)

| Feature | Detail |
|---|---|
| Registration | Email + password, sends verification link |
| Verification | Hash-based deep link (`#verify&token=...`) |
| Login | bcrypt cost-12, RS256 JWT access token (15 min) |
| Session | Rotating refresh token in HttpOnly cookie (7 days) |
| Google OAuth | PKCE flow via `/api/accounts/oauth/google/initiate` |
| Rate limiting | 5 attempts → 15-min lockout per email (Redis) |
| Password reset | 15-minute tokens, all sessions revoked on success |
| GDPR deletion | 14-day grace period, data export before wipe |
| Data export | JSON delivered via email (GDPR Article 20) |

### Chat & RAG (`app/chat/`)

Sessions are persisted with their full message history. The RAG pipeline:

1. **Emergency pre-check** — keyword scan before any network call. Emergencies get an instant 999 response.
2. **Retrieval** — the user query is embedded and the top-3 most semantically similar NHS/NICE/WHO guideline chunks are fetched from Chroma (or MockRetriever in development).
3. **Augmentation** — retrieved chunks are injected into the system prompt. The LLM can only cite what was retrieved.
4. **Generation** — GPT-4o-mini generates a concise, cited response. Falls back to a mock generator if `OPENAI_API_KEY` is not set.
5. **Triage classification** — the generated text is scanned for severity language ("call 999", "see a GP soon", etc.) to derive the badge.

### Rate limiting (`app/middleware/`)

Sliding-window algorithm using Redis sorted sets. Buckets are per-IP for unauthenticated requests and per-user-ID for authenticated ones (extracted from the JWT claim). Every response includes standard headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, `X-RateLimit-Scope`, `Retry-After` on 429.

Route policies (requests / window):

| Route prefix | Limit |
|---|---|
| `/api/accounts/register` | 5 / 15 min |
| `/api/accounts/login` | 20 / 15 min |
| `/api/accounts/forgot-password` | 5 / 15 min |
| `/api/accounts/me/export` | 3 / hour |
| `/api/chat/sessions` | 60 / min |
| `/api/admin/*` | 120 / min |
| everything else | 200 / min |

### Notifications (`app/notifications/`)

Two APScheduler jobs:

**Session summaries** (every 5 minutes): finds sessions quiet for ≥ 30 minutes, builds an HTML email with the triage outcome, key advice excerpts, and citations, and sends via Resend. A `NotificationLog` row is written for every attempt (sent / failed / skipped) — this row is the deduplication guard.

**Follow-up reminders** (every hour): finds green/amber sessions that are 48–120 hours old. Skips if the user has started a newer consultation (assumed resolved), if the preference is off, or if the session was red-severity (emergency — no 48-hour follow-up appropriate).

### Admin API (`app/routers/admin.py`)

All endpoints require `is_superuser = True`. Protected by the `get_admin_user` FastAPI dependency which returns HTTP 403 (not 404) for authenticated non-admins.

| Endpoint | Purpose |
|---|---|
| `GET /api/admin/stats` | Dashboard: user counts, sessions, severity breakdown, notification delivery |
| `GET /api/admin/users` | Search/filter users, paginated, enriched with session count + deletion status |
| `GET /api/admin/users/{id}` | Single user detail |
| `PATCH /api/admin/users/{id}/active` | Activate/deactivate (writes `admin_user_activated/deactivated` audit event) |
| `PATCH /api/admin/users/{id}/superuser` | Grant/revoke admin (writes `admin_superuser_granted/revoked` audit event) |
| `GET /api/admin/audit-logs` | Full audit trail, filterable by event type and user |
| `GET /api/admin/notification-logs` | Email delivery history, filterable by status |
| `GET /api/admin/deletions` | GDPR deletion queue |

Admins cannot modify their own `is_active` or `is_superuser` through the API — this prevents accidental lockout.

---

## Configuration reference

Copy `.env.production.example` to `.env` (development) or `.env.production` (production) and set these values:

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | `postgresql+asyncpg://user:pass@host:5432/dbname` |
| `REDIS_URL` | ✅ | `redis://:password@host:6379/0` |
| `SECRET_KEY` | ✅ | 64+ char random hex (`python -c "import secrets; print(secrets.token_hex(64))"`) |
| `RESEND_API_KEY` | ✅ | From [resend.com](https://resend.com) |
| `FRONTEND_URL` | ✅ | Your production domain, e.g. `https://handydoctor.app` |
| `ALLOWED_ORIGINS` | ✅ | Comma-separated CORS origins |
| `GOOGLE_CLIENT_ID` | ☑ | Required for Google login |
| `GOOGLE_CLIENT_SECRET` | ☑ | Required for Google login |
| `OPENAI_API_KEY` | ☑ | Required for real RAG generation (falls back to mock otherwise) |
| `OPENAI_MODEL` | — | Default: `gpt-4o-mini` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | — | Default: `15` |
| `REFRESH_TOKEN_EXPIRE_DAYS` | — | Default: `7` |
| `LOG_LEVEL` | — | `DEBUG`/`INFO`/`WARNING`/`ERROR` (default: `INFO`) |
| `LOG_FORMAT` | — | `json` (production) or `text` (development, default) |

---

## Database migrations

```bash
# Apply all pending migrations
make migrate

# Generate a new migration after changing models
make migrate-new MSG="add_my_table"

# Check migration integrity (up → down → up round-trip)
make migrate-check

# Roll back one step
make migrate-down
```

Migration history:

| Revision | Description |
|---|---|
| `0001` | Initial schema (users, tokens, prefs) |
| `0002` | Audit logs |
| `0003` | Chat sessions and messages |
| `0004` | Notification logs |
| `0005` | Admin audit event enum values |

---

## Running tests

```bash
make test           # 217 tests, SQLite in-memory (no Docker required)
make test-cov       # with coverage report → htmlcov/index.html
make test-fast      # stop on first failure
```

Tests are organised by component:

| File | Tests | Coverage |
|---|---|---|
| `test_accounts.py` | Registration, login, OAuth, profile, GDPR | Auth flows |
| `test_gdpr.py` | Deletion worker, data export, anonymisation | GDPR pipeline |
| `test_rate_limit.py` | Policy resolution, sliding window, middleware | Rate limiter |
| `test_chat.py` | Session CRUD, RAG pipeline, triage | Chat component |
| `test_notifications.py` | Builder, worker, dedup, preference gating | Notification worker |
| `test_admin.py` | Auth, stats, user management, audit log | Admin router |
| `test_ingest.py` | Sources, parser, chunker, embedder, pipeline | Ingest pipeline |

---

## Indexing NHS guidelines

The RAG pipeline uses a `MockRetriever` by default (keyword-scored, no external dependencies). To enable real semantic search:

**1. Start Chroma:**
```bash
docker run -p 8001:8000 chromadb/chroma
```

**2. Index all 26 guideline sources:**
```bash
# With real OpenAI embeddings (recommended, ~$0.05 total):
OPENAI_API_KEY=sk-... python scripts/ingest/run.py --all

# Or with the free TF-IDF fallback:
python scripts/ingest/run.py --all --no-openai
```

**3. Check what's indexed:**
```bash
python scripts/ingest/run.py --status
```

**4. Test retrieval:**
```bash
python scripts/ingest/run.py --query "I have a sore throat and fever"
```

**5. Swap retriever in `app/chat/ai/pipeline.py`:**
```python
# Replace MockRetriever() with:
from app.chat.ai.retrieval import ChromaRetriever
retriever=ChromaRetriever()
```

---

## Production deployment

**Prerequisites:**
- A server with Docker and Docker Compose v2
- A domain pointing to the server
- Ports 80 and 443 open

```bash
# 1. Clone onto the production server
git clone https://github.com/your-org/handydoctor.git /opt/handydoctor
cd /opt/handydoctor

# 2. Copy the frontend HTML
cp handydoc_frontend.html www/index.html

# 3. Run the setup wizard (guides through keys, env, TLS, migrations, start)
chmod +x scripts/*.sh
./scripts/setup.sh
```

The setup wizard handles RSA key generation, `.env.production` configuration, Let's Encrypt certificate issuance, and the first `alembic upgrade head`. After it completes the application is live.

**Subsequent deployments:**
```bash
git pull origin main
./scripts/deploy.sh          # zero-downtime rolling restart
```

Or push to `main` and let the GitHub Actions deploy workflow handle it automatically (requires `PROD_SSH_HOST`, `PROD_SSH_USER`, `PROD_SSH_PRIVATE_KEY` repository secrets).

**Backups:**
```bash
# Add to crontab: daily at 03:00
0 3 * * * /opt/handydoctor/scripts/backup.sh >> /var/log/handydoctor-backup.log 2>&1
```

---

## API reference

Full interactive documentation is available at `/docs` (Swagger UI) and `/redoc` when the server is running. Key routes:

```
POST   /api/accounts/register
POST   /api/accounts/login
POST   /api/accounts/logout
POST   /api/accounts/token/refresh
GET    /api/accounts/me
PATCH  /api/accounts/me
POST   /api/accounts/me/change-password
GET    /api/accounts/me/notification-prefs
PATCH  /api/accounts/me/notification-prefs
GET    /api/accounts/me/sessions
DELETE /api/accounts/me/sessions/{id}
POST   /api/accounts/me/export
POST   /api/accounts/me/request-deletion
POST   /api/accounts/verify-email
POST   /api/accounts/forgot-password
POST   /api/accounts/reset-password
GET    /api/accounts/oauth/google/initiate
GET    /api/accounts/oauth/google/callback

POST   /api/chat/sessions
GET    /api/chat/sessions
GET    /api/chat/sessions/{id}
POST   /api/chat/sessions/{id}/messages
DELETE /api/chat/sessions/{id}
GET    /api/chat/sessions/{id}/export

GET    /api/admin/stats
GET    /api/admin/users
GET    /api/admin/users/{id}
PATCH  /api/admin/users/{id}/active
PATCH  /api/admin/users/{id}/superuser
GET    /api/admin/audit-logs
GET    /api/admin/notification-logs
GET    /api/admin/deletions

GET    /health
```

---

## Medical disclaimer

HandyDoctor is a triage assistance tool intended for informational purposes only.

**It is not a substitute for professional medical advice, diagnosis, or treatment.**

- Always seek the advice of a qualified clinician for personal medical decisions
- In a medical emergency, call 999 (UK) immediately — do not wait for any AI response
- Guideline content is sourced from publicly available NHS, NICE, and WHO publications and may not reflect the most recent clinical updates
- The accuracy of responses depends on the currency of indexed guideline content

See [LICENSE](./LICENSE) for the full medical disclaimer.

---

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for development setup, coding conventions, and the PR process.

## License

MIT — see [LICENSE](./LICENSE).

Copyright (c) 2026 HandyDoctor Contributors
