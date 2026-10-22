# Changelog

All notable changes to HandyDoctor are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [1.0.0] — 2026-05-23

First production-ready release. All core components built, tested, and deployed.

### Added

#### Authentication & Accounts (`app/routers/accounts.py`)
- User registration with email verification (24-hour token, hash deep-link)
- Login with bcrypt cost-12, RS256 JWT access tokens (15 min) and rotating refresh tokens (7 days, HttpOnly cookie)
- Google OAuth 2.0 PKCE flow
- Per-user login rate limiting (5 attempts → 15-minute lockout, Redis sliding window)
- Password reset flow (15-minute tokens)
- Email address change with re-verification
- Profile editing (name, DOB, gender, country)
- Session listing and revocation
- GDPR Article 17 account deletion (14-day grace period, data export before wipe)
- GDPR Article 20 data export (JSON, delivered via email)

#### AI Chat & RAG (`app/chat/`)
- ChatSession and ChatMessage models with PostgreSQL persistence
- RAG pipeline: emergency pre-check → retrieval → GPT-4o-mini generation → triage classification
- MockRetriever (keyword-scored, topic-matched) for development without Chroma/OpenAI
- ChromaRetriever backed by the NHS guideline ingestion pipeline
- Session auto-titling from first message
- Full CRUD: create, list (paginated), get (with messages), delete, export (JSON)
- Emergency keyword detection (instant 999 response, no LLM call)
- Mental health crisis detection (Samaritans response)
- Triage severity badge: green / amber / red, derived from generated response content

#### Rate Limiting (`app/middleware/`)
- Sliding-window algorithm using Redis sorted sets
- Per-IP (unauthenticated) and per-user (authenticated via JWT decode) buckets
- Route-specific policies: strict for auth/email endpoints, standard for API, open for health
- Standard rate-limit response headers (X-RateLimit-Limit/Remaining/Reset/Scope, Retry-After)

#### Notifications (`app/notifications/`)
- NotificationLog model for deduplication and audit trail (SET NULL on GDPR erasure)
- Session summary emails: sent ~30 minutes after a session goes quiet
- Follow-up reminder emails: sent 48–120 hours after green/amber sessions
- Skips: user preference off, red-severity session, user has started a newer consultation
- HTML + plain-text email payloads with severity badges, citations, and NHS guideline links
- APScheduler integration: session summaries every 5 min, follow-ups every hour

#### Admin Router (`app/routers/admin.py`)
- Protected by `is_superuser` flag + `get_admin_user` dependency (HTTP 403 for non-admins)
- System dashboard: user counts, session/message counts, notification breakdown, severity breakdown
- User management: list with search/filter/pagination, detail view, activate/deactivate, grant/revoke superuser
- Self-protection: admins cannot modify their own active status or superuser flag
- Audit log browser: filterable by event type and user_id, with email enrichment
- Notification log browser: filterable by delivery status
- GDPR deletion queue with include_cancelled / include_completed flags
- All mutations write proper `admin_*` audit events (4 new AuditEvent values)

#### Data Ingestion Pipeline (`scripts/ingest/`)
- 26 curated NHS, NICE CKS, and WHO guideline sources
- Async fetcher with 1-second politeness delay and exponential backoff
- BeautifulSoup HTML parser with authority-specific CSS selector cascade
- Overlapping paragraph-aware chunker (~400 tokens per chunk, 80-token overlap)
- OpenAI `text-embedding-3-small` embedder with TF-IDF zero-cost fallback
- Chroma vector store with idempotent upserts and source-level delete
- CLI: `--all`, `--source`, `--force`, `--status`, `--query`, `--no-openai`

#### Deployment Infrastructure
- Multi-stage Dockerfile (builder with gcc/libpq, non-root runtime user)
- `docker-compose.yml` (development: hot-reload, local Postgres + Redis)
- `docker-compose.prod.yml` (production: nginx + Certbot + Postgres + Redis + API, internal network, Docker secrets for RSA keys)
- `nginx/nginx.conf`: TLS termination, HSTS, CSP, X-Frame-Options, connection rate limiting, WebSocket support, ACME challenge, dotfile blocking
- `scripts/setup.sh`: interactive first-run (keys → env → TLS → migrations → start)
- `scripts/deploy.sh`: zero-downtime rolling update with health-check gate and rollback
- `scripts/backup.sh`: compressed pg_dump with 7-day local retention and optional S3 upload
- `scripts/generate-keys.sh`: idempotent RSA-2048 key pair generation
- `scripts/postgres-init.sql`: PostgreSQL extensions and performance tuning

#### CI/CD (`.github/workflows/`)
- `ci.yml`: lint (ruff + format-check + mypy), full test suite (real Postgres 16 + Redis 7 services), migration round-trip check, Docker build smoke-test, frontend HTML validation, `ci-success` gate job
- `deploy.yml`: triggered by `workflow_run` after CI succeeds, builds and pushes to GHCR, SSH deploy via `scripts/deploy.sh`, production health check, GitHub Deployment record

#### Frontend (`handydoc_frontend.html`)
- Single-page application: Home (hero, stats, how-it-works, features, testimonials, FAQ, CTA, footer), Chat, Hospitals, Past Chats, Profile, Login, Register, Forgot Password, Reset Password, Verify Email, Auth Gate
- Real API client (`apiFetch`) with silent token refresh, 429 handling, `credentials: include`
- Mobile hamburger drawer with all navigation
- App breadcrumb replaces nav links on app pages
- Deep-link handler: `#verify&token=`, `#reset&token=`, `#change-email&token=`
- Emergency detection fast-path (pre-check before API call)
- Live past chats: fetch, render, search, delete, export JSON
- Live profile: fetch, save, password change, notification prefs, session revocation, data export, account deletion
- FAQ accordion, testimonials section, trust badges

#### Testing
- 217 tests across 8 suites (accounts, GDPR, rate limit, chat, notifications, admin, ingest)
- In-memory SQLite for all tests — no external services required
- `conftest.py`: RSA key generation, bcrypt self-test patch, Redis mock injection into `app.state`

#### Tooling
- `pyproject.toml`: ruff config (E/W/F/I/B/C4/UP/ASYNC/S), mypy, pytest (asyncio_mode=auto), coverage (70% gate, branch=True)
- `Makefile`: `make ci`, `make test`, `make lint`, `make format`, `make migrate`, `make docker-build`, `make ingest-all`, etc.
- `app/logging_config.py`: structured JSON logging for production, coloured text for development, request-ID context injection

#### Database Migrations
- `0001`: initial schema (users, refresh_tokens, email_verifications, password_reset_tokens, account_deletions, notification_prefs)
- `0002`: audit_logs
- `0003`: chat_sessions, chat_messages
- `0004`: notification_logs
- `0005`: admin audit event enum values

---

## [Unreleased]

### Planned
- WebSocket streaming for chat responses (real-time token delivery)
- Chroma / pgvector production deployment guide
- Admin UI (React dashboard consuming the `/api/admin/*` endpoints)
- Multi-language support (NHS Wales bilingual guidelines)
- Session sharing (read-only link to share a consultation with a GP)
