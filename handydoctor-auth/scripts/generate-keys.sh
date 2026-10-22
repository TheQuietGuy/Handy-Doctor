#!/usr/bin/env bash
# scripts/generate-keys.sh
#
# Generates the RS256 RSA key pair required for JWT signing.
# Run this ONCE before your first `docker compose up` or local start.
#
# Usage:
#   chmod +x scripts/generate-keys.sh
#   ./scripts/generate-keys.sh
#
# Output:
#   ./private.pem   — private key (KEEP SECRET — add to .gitignore)
#   ./public.pem    — public key  (safe to commit if needed)

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRIVATE_KEY="$PROJECT_ROOT/private.pem"
PUBLIC_KEY="$PROJECT_ROOT/public.pem"

if [[ -f "$PRIVATE_KEY" ]] && [[ -f "$PUBLIC_KEY" ]]; then
  echo "✅  Keys already exist — skipping generation."
  echo "    Private: $PRIVATE_KEY"
  echo "    Public:  $PUBLIC_KEY"
  echo ""
  echo "    Delete them and re-run this script to regenerate."
  exit 0
fi

echo "🔑  Generating RSA-2048 key pair…"

openssl genrsa -out "$PRIVATE_KEY" 2048 2>/dev/null
openssl rsa -in "$PRIVATE_KEY" -pubout -out "$PUBLIC_KEY" 2>/dev/null

# Protect the private key: owner read-only
chmod 600 "$PRIVATE_KEY"
chmod 644 "$PUBLIC_KEY"

echo "✅  Keys generated successfully."
echo "    Private: $PRIVATE_KEY  (mode 600 — never commit this file)"
echo "    Public:  $PUBLIC_KEY"
echo ""
echo "⚠️   Ensure private.pem is in your .gitignore:"
echo "    echo 'private.pem' >> .gitignore"
