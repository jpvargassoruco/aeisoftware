#!/usr/bin/env bash
# smoke_test.sh — Basic health + API smoke tests for the SaaS Portal
#
# Usage:
#   ./portal/tests/smoke_test.sh <BASE_URL> <API_KEY>
#
# Examples:
#   ./portal/tests/smoke_test.sh http://localhost:8000 my-dev-key
#   ./portal/tests/smoke_test.sh https://portal-qa.aeisoftware.com "$PORTAL_API_KEY"
#
# Exit codes: 0 = all tests passed, 1 = one or more tests failed

set -euo pipefail

BASE_URL="${1:?Usage: $0 <BASE_URL> <API_KEY>}"
API_KEY="${2:?Usage: $0 <BASE_URL> <API_KEY>}"

PASS=0
FAIL=0

# ── helpers ──────────────────────────────────────────────────────────────────
check() {
  local label="$1"
  local url="$2"
  local expected_status="${3:-200}"
  local extra_args=("${@:4}")

  http_status=$(curl -s -o /dev/null -w "%{http_code}" \
    --max-time 10 \
    "${extra_args[@]}" \
    "$url")

  if [[ "$http_status" == "$expected_status" ]]; then
    echo "  ✅  PASS  [$http_status]  $label"
    ((PASS++))
  else
    echo "  ❌  FAIL  [$http_status expected $expected_status]  $label"
    ((FAIL++))
  fi
}

auth_header=(-H "X-API-Key: $API_KEY")

# ── tests ─────────────────────────────────────────────────────────────────────
echo ""
echo "🔍  Smoke testing: $BASE_URL"
echo "────────────────────────────────────────────"

check "GET /health  (unauthenticated)"              "$BASE_URL/health"            200
check "GET /api/instances  (authenticated)"         "$BASE_URL/api/instances"     200  "${auth_header[@]}"
check "GET /api/instances  (no auth → 403)"         "$BASE_URL/api/instances"     403
check "GET /api/templates  (authenticated)"         "$BASE_URL/api/templates"     200  "${auth_header[@]}"
check "GET /api/nonexistent → 404"                  "$BASE_URL/api/nonexistent"   404

# ── summary ───────────────────────────────────────────────────────────────────
echo "────────────────────────────────────────────"
echo "  Results: ${PASS} passed, ${FAIL} failed"
echo ""

if [[ "$FAIL" -gt 0 ]]; then
  echo "❌  Smoke test FAILED"
  exit 1
fi

echo "✅  Smoke test PASSED"
exit 0
