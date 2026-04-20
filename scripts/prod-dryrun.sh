#!/usr/bin/env bash
#
# Local "prod dry run" — runs Somm against a throwaway SQLite with the
# same environment shape Render uses. Catches misconfiguration (missing
# env vars, bad paths, migration failures, cookie-secure mismatches)
# BEFORE you push to Render.
#
# Usage:
#   ./scripts/prod-dryrun.sh
#
# What it does:
#   1. Creates a temp directory for a throwaway DB (so it doesn't
#      stomp your dev somm.db).
#   2. Loads .env (if present), then applies prod-like overrides.
#   3. Runs `alembic upgrade head` — exactly what Render runs.
#   4. Starts uvicorn on 127.0.0.1:8765.
#   5. Hits /health and /healthz so you see a fresh snapshot.
#
# Stop with Ctrl-C. The temp dir cleans up on exit.

set -euo pipefail

cd "$(dirname "$0")/.."

TMPDIR="$(mktemp -d -t somm-dryrun-XXXXXX)"
trap 'rm -rf "$TMPDIR"; kill %1 2>/dev/null || true' EXIT

# Load .env if present (dev convenience — real prod reads from Render).
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# Prod-like overrides for this run. These override .env so the dry-run
# exercises the same paths Render will use, not your dev shortcuts.
export DATABASE_URL="sqlite+aiosqlite:///$TMPDIR/somm.db"
export COOKIE_SECURE="false"                         # still false because local is HTTP
export APP_URL="http://127.0.0.1:8765"
export SOMM_DATA_PATH="./somm_data"
export CHARTIER_XLSX_PATH="./somm_data/Chartier_Wine_Food_Pairings.xlsx"

echo "=== Somm prod-dryrun ==="
echo "DB:          $DATABASE_URL"
echo "APP_URL:     $APP_URL"
echo "SOMM_DATA:   $SOMM_DATA_PATH"
echo "XLSX:        $CHARTIER_XLSX_PATH"
echo "RESEND set:  $([ -n "${RESEND_API_KEY:-}" ] && echo yes || echo no)"
echo "ADMIN_EMAIL: ${ADMIN_EMAIL:-(unset — founder seeding skipped)}"
echo "========================"

# Sanity — files exist?
for f in "$SOMM_DATA_PATH/SKILL.md" "$CHARTIER_XLSX_PATH"; do
  if [ ! -f "$f" ]; then
    echo "❌ Missing file: $f"
    exit 1
  fi
done

# Migrations — this must succeed or the deploy would fail on Render too.
# alembic/env.py reads DATABASE_URL and auto-translates async→sync, so we
# just invoke alembic and let it pick up the env var we exported above.
# Use python3 -m to avoid PATH issues with pip --user installs.
echo "→ Running alembic upgrade head"
python3 -m alembic upgrade head

# Start uvicorn in background, hit /health & /healthz, then foreground it.
PORT=8765
echo "→ Starting uvicorn on 127.0.0.1:$PORT"
python3 -m uvicorn main:app --host 127.0.0.1 --port "$PORT" --workers 1 &
UVICORN_PID=$!

# Wait for readiness (max ~10s).
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

echo
echo "=== /health ==="
curl -fsS "http://127.0.0.1:$PORT/health" && echo
echo "=== /healthz ==="
curl -fsS "http://127.0.0.1:$PORT/healthz" | python3 -m json.tool || true
echo
echo "✅ App is up. Open http://127.0.0.1:$PORT in a browser."
echo "   Ctrl-C to stop."

wait "$UVICORN_PID"
