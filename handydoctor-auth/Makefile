# Makefile — HandyDoctor API
#
# Mirrors the CI pipeline so you can run the exact same checks locally
# before pushing. Run `make help` to see all targets.

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test test-cov migrate \
        migrate-check docker-build docker-run up down logs clean ci

# ── Colours ───────────────────────────────────────────────────────────────────
BLUE  := \033[0;34m
GREEN := \033[0;32m
RESET := \033[0m

help: ## Show this help message
	@echo ""
	@echo "$(BLUE)HandyDoctor API — make targets$(RESET)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-18s$(RESET) %s\n", $$1, $$2}'
	@echo ""

# ── Setup ─────────────────────────────────────────────────────────────────────
install: ## Install all dependencies (including dev tools)
	pip install -r requirements.txt
	pip install ruff mypy pytest-cov fakeredis

keys: ## Generate RSA key pair for local JWT signing
	./scripts/generate-keys.sh

# ── Code quality (mirrors CI `lint` job) ───────────────────────────────────────
lint: ## Run ruff linter
	ruff check app/ scripts/ tests/ main.py

format: ## Auto-format code with ruff
	ruff format app/ scripts/ tests/ main.py

format-check: ## Check formatting without modifying files (CI mode)
	ruff format --check app/ scripts/ tests/ main.py

typecheck: ## Run mypy static type checking
	mypy app/ main.py --ignore-missing-imports --no-strict-optional

# ── Testing (mirrors CI `test` job) ─────────────────────────────────────────────
test: ## Run the full test suite
	DATABASE_URL=sqlite+aiosqlite:///:memory: pytest tests/ -v

test-cov: ## Run tests with coverage report
	DATABASE_URL=sqlite+aiosqlite:///:memory: pytest tests/ \
		--cov=app --cov-report=term-missing --cov-report=html

test-fast: ## Run tests, stop on first failure, no coverage
	DATABASE_URL=sqlite+aiosqlite:///:memory: pytest tests/ -x -q

# ── Database ──────────────────────────────────────────────────────────────────
migrate: ## Apply all pending migrations
	alembic upgrade head

migrate-down: ## Roll back one migration
	alembic downgrade -1

migrate-new: ## Create a new migration (usage: make migrate-new MSG="add_table")
	alembic revision --autogenerate -m "$(MSG)"

migrate-check: ## Verify migrations apply cleanly from scratch (CI parity)
	@echo "Testing migration round-trip..."
	DATABASE_URL=sqlite+aiosqlite:///migration_check.db alembic upgrade head
	DATABASE_URL=sqlite+aiosqlite:///migration_check.db alembic downgrade base
	DATABASE_URL=sqlite+aiosqlite:///migration_check.db alembic upgrade head
	@rm -f migration_check.db
	@echo "$(GREEN)Migrations round-trip cleanly.$(RESET)"

# ── Docker (development) ────────────────────────────────────────────────────────
up: ## Start the dev stack (postgres, redis, api) in the background
	docker compose up -d

down: ## Stop the dev stack
	docker compose down

logs: ## Tail logs from the dev stack
	docker compose logs -f api

restart: ## Restart just the API container
	docker compose restart api

# ── Docker (production parity check) ─────────────────────────────────────────────
docker-build: ## Build the production image locally (CI parity)
	docker build -t handydoctor-api:local .

docker-run: docker-build ## Build and smoke-test the production image
	docker run --rm \
		-e DATABASE_URL=sqlite+aiosqlite:///:memory: \
		-e SECRET_KEY=local-check \
		-e JWT_PRIVATE_KEY_PATH=/dev/null \
		-e JWT_PUBLIC_KEY_PATH=/dev/null \
		handydoctor-api:local \
		python -c "import main; print('Image OK')"

# ── Ingestion ─────────────────────────────────────────────────────────────────
ingest-status: ## Show what's currently indexed in Chroma
	python scripts/ingest/run.py --status

ingest-all: ## Index all guideline sources
	python scripts/ingest/run.py --all

# ── Composite ─────────────────────────────────────────────────────────────────
ci: lint format-check typecheck test migrate-check ## Run the full CI pipeline locally
	@echo ""
	@echo "$(GREEN)✅ All CI checks passed locally.$(RESET)"

clean: ## Remove caches, coverage reports, and compiled files
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache htmlcov coverage.xml .coverage .mypy_cache .ruff_cache
	@echo "$(GREEN)Cleaned.$(RESET)"
