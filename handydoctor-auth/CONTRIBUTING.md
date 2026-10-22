# Contributing to HandyDoctor

Thanks for considering a contribution. This guide covers everything you need to get the full development stack running locally, understand the codebase structure, and submit a change.

---

## Prerequisites

- Python 3.12+
- Docker and Docker Compose v2
- `openssl` (for RSA key generation)
- `git`

Optional (for real RAG embeddings):
- An [OpenAI API key](https://platform.openai.com)

---

## Local development setup

```bash
# 1. Clone the repository
git clone https://github.com/your-org/handydoctor.git
cd handydoctor

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install all dependencies (including dev tools)
make install

# 4. Generate RSA keys for JWT signing
make keys

# 5. Copy the example environment file and fill in values
cp .env.example .env
# Edit .env — the defaults work for local Docker development

# 6. Start the full development stack (Postgres, Redis, API)
make up

# 7. Verify everything is healthy
curl http://localhost:8000/health
```

The API is now running at `http://localhost:8000` with hot-reload.
Interactive docs are at `http://localhost:8000/docs`.

---

## Running the test suite

Tests use an in-memory SQLite database — no running services required.

```bash
make test          # full suite (217 tests)
make test-fast     # stop on first failure, no coverage
make test-cov      # with HTML coverage report → htmlcov/index.html
```

All tests must pass before opening a pull request.

---

## Code quality

```bash
make lint          # ruff linting
make format        # auto-format with ruff
make format-check  # CI-mode format check (no changes written)
make typecheck     # mypy static type checking
make ci            # run the full local CI pipeline
```

The CI pipeline (`.github/workflows/ci.yml`) runs the same checks on every push. `make ci` should pass locally before you push.

---

## Project structure

```
handydoctor/
├── app/
│   ├── auth/             JWT, OAuth, rate limiting, password hashing
│   ├── chat/             Chat session models, RAG pipeline (ai/), router
│   │   └── ai/           retrieval.py, pipeline.py, triage.py
│   ├── email/            Transactional email sender (Resend)
│   ├── gdpr/             GDPR deletion worker and APScheduler
│   ├── middleware/        Rate-limit middleware and config
│   ├── notifications/    Session summary & follow-up email worker
│   ├── routers/          FastAPI routers (accounts, admin)
│   ├── audit.py          AuditLog model and AuditEvent enum
│   ├── config.py         Pydantic Settings (reads from .env)
│   ├── database.py       SQLAlchemy async engine and session factory
│   ├── logging_config.py Structured JSON / coloured dev logging
│   ├── models.py         Core ORM models (User, tokens, etc.)
│   └── schemas.py        Public-facing Pydantic schemas
├── migrations/           Alembic migrations
│   └── versions/         0001 → 0005
├── scripts/
│   ├── ingest/           NHS guideline ingestion pipeline
│   │   ├── sources.py    26 curated NHS/NICE/WHO sources
│   │   ├── fetcher.py    Async HTTP fetcher with politeness
│   │   ├── parser.py     HTML to clean text
│   │   ├── chunker.py    Overlapping text chunker
│   │   ├── embedder.py   OpenAI + TF-IDF embedders
│   │   ├── store.py      Chroma vector store wrapper
│   │   └── run.py        CLI entry point
│   ├── backup.sh         PostgreSQL backup with S3 upload
│   ├── deploy.sh         Zero-downtime rolling deploy
│   ├── generate-keys.sh  RSA key pair generation
│   ├── postgres-init.sql PostgreSQL extensions and tuning
│   └── setup.sh          First-run production setup
├── tests/                217 tests across 8 test files
├── nginx/nginx.conf       Production nginx config
├── .github/workflows/    CI (ci.yml) and deploy (deploy.yml)
├── docker-compose.yml     Development stack
├── docker-compose.prod.yml Production stack
├── Dockerfile            Multi-stage production build
├── Makefile              Convenient local commands
├── pyproject.toml        ruff, mypy, pytest, coverage config
├── alembic.ini           Alembic configuration
└── main.py               FastAPI application entry point
```

---

## Making changes

### Adding a new API endpoint

1. Add the route to the appropriate router file in `app/routers/`.
2. Add request/response schemas to `app/schemas.py` (or `admin_schemas.py` for admin endpoints).
3. Add a rate-limit policy to `app/middleware/config.py` if the endpoint is sensitive.
4. Write tests in `tests/test_<module>.py` — minimum coverage for the happy path plus the key error cases.
5. Run `make ci` to confirm everything passes.

### Adding a new database model

1. Create the model in `app/models.py` (or a dedicated file for large features).
2. Import it in `migrations/env.py` so Alembic's autogenerate can see it.
3. Generate a migration: `make migrate-new MSG="add_my_table"`
4. Review the generated file in `migrations/versions/`.
5. Apply it: `make migrate`

### Adding a new guideline source

Add an entry to `SOURCES` in `scripts/ingest/sources.py`, then run:
```bash
python scripts/ingest/run.py --source <your_new_id>
```

### Changing notification behaviour

The notification worker lives in `app/notifications/worker.py`. The tuning constants (`SESSION_QUIET_MINUTES`, `FOLLOWUP_MIN_HOURS`, etc.) are at the top of the file with comments explaining their rationale.

---

## Branch and PR conventions

- Branch names: `feature/short-description`, `fix/issue-description`, `chore/what-changed`
- Commit messages: imperative mood, present tense ("Add admin endpoint" not "Added admin endpoint")
- Each PR should do one thing. Large features should be split into reviewable chunks.
- Every PR requires passing CI (`ci-success` is a required check on `main` and `develop`).

---

## Medical content and safety

HandyDoctor handles health-related user queries. A few extra considerations apply:

- **Never** generate or store advice that could substitute for emergency services. The emergency pre-check in `app/chat/ai/pipeline.py` must remain the first step in the pipeline.
- **Never** remove the `⚕️ This is not a substitute for professional medical advice.` disclaimer from generated responses.
- Changes to the triage classifier (`app/chat/ai/triage.py`) require specific tests for both the new behaviour and regression against existing cases.
- New guideline sources added to `scripts/ingest/sources.py` must be from established clinical authorities (NHS, NICE, WHO, Royal Colleges, BTS, etc.).

---

## Reporting issues

Please open a GitHub Issue with:
- A clear description of what's wrong or what you'd like changed
- Steps to reproduce (for bugs)
- The relevant log output or error message
- Your environment (Python version, OS, Docker version)

For security vulnerabilities, **do not** open a public issue — email `security@handydoctor.app` instead.
