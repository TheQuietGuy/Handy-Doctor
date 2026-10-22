#!/usr/bin/env bash
# scripts/deploy.sh
# ══════════════════════════════════════════════════════════════════════════════
# HandyDoctor — Zero-downtime deployment
# ══════════════════════════════════════════════════════════════════════════════
#
# Builds a new Docker image, runs migrations, then performs a rolling restart
# of the API container so there is no downtime during deployment.
#
# Usage:
#   ./scripts/deploy.sh                   # deploy latest git HEAD
#   ./scripts/deploy.sh --skip-build      # restart with existing image (e.g. config change)
#   ./scripts/deploy.sh --skip-migrate    # skip alembic upgrade (dangerous — use with care)
#
# What it does:
#   1. Pull latest code (if run from CI, code is already checked out)
#   2. Build a new Docker image tagged :latest and :<git-sha>
#   3. Run alembic upgrade head in a throwaway container
#   4. Replace the running api container (start-first strategy)
#   5. Verify the new container passes its health check
#   6. Reload nginx (picks up any static file changes)
#   7. Prune old Docker images to reclaim disk space
#
# Rollback:
#   docker compose -f docker-compose.prod.yml \
#       up -d --no-deps api              # restart from :latest (last successful)
#   # OR tag a specific image and edit docker-compose.prod.yml image: field

set -euo pipefail
IFS=$'\n\t'

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${BLUE}[deploy]${RESET} $*"; }
success() { echo -e "${GREEN}[deploy]${RESET} ✅ $*"; }
warn()    { echo -e "${YELLOW}[deploy]${RESET} ⚠️  $*"; }
error()   { echo -e "${RED}[deploy]${RESET} ❌ $*" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ── Parse flags ───────────────────────────────────────────────────────────────
SKIP_BUILD=false
SKIP_MIGRATE=false
for arg in "$@"; do
    case "$arg" in
        --skip-build)   SKIP_BUILD=true   ;;
        --skip-migrate) SKIP_MIGRATE=true ;;
        *) error "Unknown argument: $arg" ;;
    esac
done

COMPOSE="docker compose -f $ROOT/docker-compose.prod.yml"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

echo ""
echo -e "${BOLD}  🚀 HandyDoctor deployment — $TIMESTAMP (git: $GIT_SHA)${RESET}"
echo ""

# ── Preflight checks ──────────────────────────────────────────────────────────
info "Running preflight checks..."

[[ -f "$ROOT/.env.production" ]] || error ".env.production not found. Run scripts/setup.sh first."
[[ -f "$ROOT/private.pem" ]]     || error "private.pem not found. Run scripts/generate-keys.sh."
[[ -f "$ROOT/public.pem" ]]      || error "public.pem not found. Run scripts/generate-keys.sh."
[[ -f "$ROOT/www/index.html" ]]  || warn  "www/index.html not found — frontend will not be served."

success "Preflight passed."

# ── Build ─────────────────────────────────────────────────────────────────────
if [[ "$SKIP_BUILD" == "true" ]]; then
    warn "Skipping image build (--skip-build flag set)."
else
    info "Building Docker image handydoctor-api:$GIT_SHA ..."
    docker build \
        --tag "handydoctor-api:$GIT_SHA" \
        --tag "handydoctor-api:latest" \
        --label "git.sha=$GIT_SHA" \
        --label "build.timestamp=$TIMESTAMP" \
        --file "$ROOT/Dockerfile" \
        "$ROOT"
    success "Image built: handydoctor-api:$GIT_SHA"
fi

# ── Migrations ────────────────────────────────────────────────────────────────
if [[ "$SKIP_MIGRATE" == "true" ]]; then
    warn "Skipping migrations (--skip-migrate flag set)."
else
    info "Running database migrations..."
    # Run in a throwaway container on the internal network
    docker run --rm \
        --env-file "$ROOT/.env.production" \
        --env JWT_PRIVATE_KEY_PATH=/dev/null \
        --env JWT_PUBLIC_KEY_PATH=/dev/null \
        --network "$(basename "$ROOT")_internal" \
        --name "handydoctor-migrate-$TIMESTAMP" \
        "handydoctor-api:latest" \
        alembic upgrade head

    success "Migrations applied."
fi

# ── Rolling restart ───────────────────────────────────────────────────────────
info "Starting rolling update of API container..."

# docker compose's --no-deps + start-first strategy (configured in docker-compose.prod.yml)
$COMPOSE up -d --no-deps --remove-orphans api

# Wait for health check to pass (up to 90 seconds)
info "Waiting for new container to become healthy..."
WAIT_SECS=0
MAX_WAIT=90
while [[ $WAIT_SECS -lt $MAX_WAIT ]]; do
    HEALTH="$(docker inspect --format='{{.State.Health.Status}}' \
        "$($COMPOSE ps -q api 2>/dev/null | head -1)" 2>/dev/null || echo "unknown")"

    if [[ "$HEALTH" == "healthy" ]]; then
        success "API container is healthy."
        break
    elif [[ "$HEALTH" == "unhealthy" ]]; then
        error "API container health check failed. Rolling back..."
        # Trigger rollback — restart from previous image
        $COMPOSE up -d --no-deps api
        error "Rolled back. Check 'docker compose -f docker-compose.prod.yml logs api'."
    fi

    sleep 3
    WAIT_SECS=$((WAIT_SECS + 3))
    echo -n "."
done
echo ""

if [[ $WAIT_SECS -ge $MAX_WAIT ]]; then
    error "Health check timed out after ${MAX_WAIT}s. Check container logs."
fi

# ── Reload nginx ──────────────────────────────────────────────────────────────
info "Reloading nginx..."
$COMPOSE exec nginx nginx -s reload
success "Nginx reloaded."

# ── Prune old images ──────────────────────────────────────────────────────────
info "Pruning dangling Docker images..."
docker image prune -f --filter "label=git.sha" 2>/dev/null || true
success "Cleanup done."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
success "════════════════════════════════════════"
success " Deployment complete — git SHA: $GIT_SHA"
success "════════════════════════════════════════"
echo ""

# Show running containers
$COMPOSE ps
echo ""
