#!/usr/bin/env bash
# scripts/setup.sh
# ══════════════════════════════════════════════════════════════════════════════
# HandyDoctor — First-run production setup
# ══════════════════════════════════════════════════════════════════════════════
#
# Run this ONCE on a fresh server before starting the production stack.
#
# What it does:
#   1. Checks prerequisites (Docker, Docker Compose, openssl)
#   2. Generates RSA-2048 key pair (if not already present)
#   3. Guides you through creating .env.production from the template
#   4. Creates the www/ directory and copies the frontend HTML
#   5. Issues a Let's Encrypt TLS certificate via Certbot
#   6. Runs database migrations
#   7. Starts all services
#
# Usage:
#   chmod +x scripts/setup.sh
#   ./scripts/setup.sh

set -euo pipefail
IFS=$'\n\t'

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${BLUE}ℹ ${RESET}$*"; }
success() { echo -e "${GREEN}✅ ${RESET}$*"; }
warn()    { echo -e "${YELLOW}⚠️  ${RESET}$*"; }
error()   { echo -e "${RED}❌ ${RESET}$*" >&2; exit 1; }
step()    { echo -e "\n${BOLD}── $* ${RESET}"; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║   🤚 HandyDoctor — Production Setup  ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${RESET}"

# ── Step 1: Prerequisites ─────────────────────────────────────────────────────
step "1/7  Checking prerequisites"

for cmd in docker openssl curl; do
    if command -v "$cmd" &>/dev/null; then
        success "$cmd found: $(command -v $cmd)"
    else
        error "$cmd is required but not installed. Aborting."
    fi
done

# Check Docker Compose (v2 plugin or standalone)
if docker compose version &>/dev/null 2>&1; then
    success "Docker Compose v2 found"
elif docker-compose version &>/dev/null 2>&1; then
    success "docker-compose (standalone) found"
else
    error "Docker Compose not found. Install from https://docs.docker.com/compose/install/"
fi

# ── Step 2: RSA key pair ──────────────────────────────────────────────────────
step "2/7  RSA key pair"

if [[ -f "$ROOT/private.pem" && -f "$ROOT/public.pem" ]]; then
    success "Keys already exist — skipping generation."
else
    bash "$ROOT/scripts/generate-keys.sh"
fi

echo ""
warn "Ensure private.pem is in .gitignore and backed up securely!"
echo "   Add to .gitignore:  echo 'private.pem' >> .gitignore"

# ── Step 3: Environment file ──────────────────────────────────────────────────
step "3/7  Environment configuration"

if [[ -f "$ROOT/.env.production" ]]; then
    success ".env.production already exists — skipping."
    warn "Review it and ensure all [REQUIRED] values are filled in."
else
    cp "$ROOT/.env.production.example" "$ROOT/.env.production"
    info "Created .env.production from template."
    echo ""
    warn "ACTION REQUIRED: Open .env.production and fill in every [REQUIRED] value."
    warn "Pay special attention to:"
    warn "  • DATABASE_URL   — set a strong POSTGRES_PASSWORD"
    warn "  • REDIS_URL      — set a strong REDIS_PASSWORD"
    warn "  • SECRET_KEY     — run: python -c \"import secrets; print(secrets.token_hex(64))\""
    warn "  • RESEND_API_KEY — from https://resend.com"
    warn "  • FRONTEND_URL   — your production domain"
    warn "  • ALLOWED_ORIGINS — your production domain"
    echo ""
    read -rp "Press Enter when you have filled in .env.production... "
fi

# Source env to get DOMAIN
# shellcheck disable=SC1091
FRONTEND_URL=$(grep '^FRONTEND_URL=' "$ROOT/.env.production" | cut -d= -f2 | tr -d '"' | tr -d "'")
DOMAIN="${FRONTEND_URL#https://}"
DOMAIN="${DOMAIN#http://}"
DOMAIN="${DOMAIN%%/*}"

if [[ -z "$DOMAIN" ]]; then
    error "FRONTEND_URL is not set in .env.production. Cannot issue TLS certificate."
fi

info "Detected domain: $DOMAIN"

# ── Step 4: Frontend HTML ─────────────────────────────────────────────────────
step "4/7  Frontend HTML"

mkdir -p "$ROOT/www"

if [[ -f "$ROOT/www/index.html" ]]; then
    success "www/index.html already present."
else
    warn "Place handydoc_frontend.html at $ROOT/www/index.html"
    warn "Example:  cp handydoc_frontend.html www/index.html"
    echo ""
    read -rp "Have you placed the HTML file? (y/N) " confirm
    [[ "${confirm,,}" == "y" ]] || error "Please add the frontend HTML before continuing."
fi

# ── Step 5: TLS certificate ───────────────────────────────────────────────────
step "5/7  TLS certificate (Let's Encrypt)"

EMAIL=$(grep '^EMAIL_FROM=' "$ROOT/.env.production" | cut -d= -f2 | tr -d '"' | tr -d "'" | sed 's/^noreply@/admin@/')

if [[ -d "/etc/letsencrypt/live/$DOMAIN" ]]; then
    success "Certificate already exists for $DOMAIN — skipping issuance."
else
    info "Starting nginx for ACME challenge..."
    docker compose -f "$ROOT/docker-compose.prod.yml" up -d nginx certbot

    info "Requesting certificate for $DOMAIN (email: $EMAIL)..."
    docker compose -f "$ROOT/docker-compose.prod.yml" run --rm certbot \
        certonly \
        --webroot \
        --webroot-path /var/www/certbot \
        --email "$EMAIL" \
        --agree-tos \
        --no-eff-email \
        --domain "$DOMAIN" \
        --domain "www.$DOMAIN"

    success "Certificate issued for $DOMAIN"

    info "Reloading nginx with TLS..."
    docker compose -f "$ROOT/docker-compose.prod.yml" exec nginx nginx -s reload
fi

# ── Step 6: Database migrations ───────────────────────────────────────────────
step "6/7  Database migrations"

info "Starting database..."
docker compose -f "$ROOT/docker-compose.prod.yml" up -d postgres redis

# Wait for PostgreSQL to be healthy
info "Waiting for PostgreSQL to be ready..."
timeout 60 bash -c "
    until docker compose -f '$ROOT/docker-compose.prod.yml' exec postgres \
        pg_isready -U hduser -d handydoctor &>/dev/null; do
        sleep 2
    done
"
success "PostgreSQL is ready."

info "Building API image..."
docker build -t handydoctor-api:latest "$ROOT"

info "Running migrations..."
docker run --rm \
    --env-file "$ROOT/.env.production" \
    --env JWT_PRIVATE_KEY_PATH=/dev/null \
    --env JWT_PUBLIC_KEY_PATH=/dev/null \
    --network "$(basename "$ROOT")_internal" \
    handydoctor-api:latest \
    alembic upgrade head

success "Migrations applied."

# ── Step 7: Start all services ────────────────────────────────────────────────
step "7/7  Starting all services"

docker compose -f "$ROOT/docker-compose.prod.yml" up -d

info "Waiting for API to become healthy..."
timeout 60 bash -c "
    until docker compose -f '$ROOT/docker-compose.prod.yml' exec api \
        python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\" \
        &>/dev/null; do
        sleep 3
    done
"

echo ""
success "═══════════════════════════════════════"
success " HandyDoctor is live at https://$DOMAIN"
success "═══════════════════════════════════════"
echo ""
echo "  Useful commands:"
echo "   docker compose -f docker-compose.prod.yml logs -f api       # tail logs"
echo "   docker compose -f docker-compose.prod.yml exec api bash     # shell into api"
echo "   ./scripts/deploy.sh                                          # deploy an update"
echo ""
