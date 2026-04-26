#!/usr/bin/env bash
# Recover @shape-rotator-2's working access token off the running hermes-
# staging gateway, then point shape-rotator-matrix at it. Approver and
# gateway share the same token by design (per the original two-bot
# pattern), so whatever the gateway is using right now will work for the
# approver too.
#
# Falls back nothing — prints what it tried and exits 1 if the token
# can't be found. In that case run mint_token.sh with the password.
set -uo pipefail

HERMES_KEY=/home/amiller/projects/hermes-agent/deploy-notes/deploy_key
EXPECTED_MXID='@shape-rotator-2:mtrx.shaperotator.xyz'
HS=https://mtrx.shaperotator.xyz
ENV_LOCAL=/home/amiller/projects/dstack/shape-rotator-matrix/.env
REPO=Account-Link/shape-rotator-matrix

echo "[1/6] phala ssh into hermes-staging, scanning for the shape-rotator-2 token"

# Run a small exploration program inside the CVM. It prints exactly one
# line on stdout — either TOKEN=<bytes> or NOTFOUND — and any
# exploration noise on stderr.
RAW=$(phala ssh hermes-staging -- -i "$HERMES_KEY" bash -s <<'REMOTE' 2>&1
set -uo pipefail
EXPECTED='@shape-rotator-2:mtrx.shaperotator.xyz'

emit_token() {
  # Validate we found a non-empty plausible token, then announce.
  local t="$1"
  if [ -n "$t" ] && [ "${#t}" -ge 16 ]; then
    echo "TOKEN=$t"
    exit 0
  fi
}

containers=$(docker ps --format '{{.Names}}' 2>/dev/null || true)
echo "containers: $containers" >&2

for c in $containers; do
  # ---- Strategy A: env vars naming shape-rotator-2 + an access token ----
  env_dump=$(docker exec "$c" env 2>/dev/null || true)
  if echo "$env_dump" | grep -qF "$EXPECTED"; then
    echo "  $c references $EXPECTED in env" >&2
    # Find any SHAPE_ROTATOR-prefixed token first (most specific)
    t=$(echo "$env_dump" | awk -F= '
      /^.*SHAPE.*TOKEN=/ { print substr($0, index($0, "=")+1); exit }
    ')
    [ -n "$t" ] && emit_token "$t"
    # Fall back to plain MATRIX_ACCESS_TOKEN if the var is set in this container
    t=$(echo "$env_dump" | awk -F= '/^MATRIX_ACCESS_TOKEN=/ { print substr($0, index($0, "=")+1); exit }')
    [ -n "$t" ] && emit_token "$t"
  fi

  # ---- Strategy B: hermes profile config files ----
  for d in /root/.hermes/profiles/shape-rotator \
           /root/.hermes/profiles/shape-rotator-2 \
           /root/.hermes/profiles/shape_rotator \
           /root/.hermes/profiles/shape_rotator_2; do
    if docker exec "$c" test -d "$d" 2>/dev/null; then
      echo "  $c has hermes profile dir $d" >&2
      docker exec "$c" find "$d" -maxdepth 4 -type f 2>/dev/null | while read -r f; do
        echo "    $f" >&2
      done
      for f in "$d/.env" "$d/config.json" "$d/credentials.json" \
               "$d/platforms/matrix/.env" "$d/platforms/matrix/config.json"; do
        if docker exec "$c" test -f "$f" 2>/dev/null; then
          echo "    inspecting $f" >&2
          contents=$(docker exec "$c" cat "$f" 2>/dev/null || true)
          # JSON access_token
          t=$(echo "$contents" | python3 -c "
import json, sys
try:
  j = json.loads(sys.stdin.read())
  for k in ('access_token','MATRIX_ACCESS_TOKEN','matrix_access_token','token'):
    if isinstance(j, dict) and k in j: print(j[k]); break
except Exception: pass
" 2>/dev/null)
          [ -n "$t" ] && emit_token "$t"
          # .env-style line
          t=$(echo "$contents" | grep -oE '^(MATRIX_ACCESS_TOKEN|access_token)=[^[:space:]]+' | head -n1 | cut -d= -f2-)
          [ -n "$t" ] && emit_token "$t"
        fi
      done
    fi
  done
done

echo "NOTFOUND"
REMOTE
)

# Pull the canonical TOKEN= line from the mixed stdout/stderr
TOKEN=$(echo "$RAW" | grep -m1 -E '^TOKEN=' | cut -d= -f2-)

if [ -z "$TOKEN" ]; then
  echo "[fail] could not find a shape-rotator-2 token on hermes-staging." >&2
  echo "[fail] noise from the remote scan:" >&2
  echo "$RAW" | sed 's/^/    /' >&2
  echo
  echo "Fall back to mint_token.sh:"
  echo "  SHAPE_ROTATOR_2_PASSWORD='…' bash deploy/admin/mint_token.sh"
  exit 1
fi

echo "[2/6] candidate token (${#TOKEN} bytes), validating against prod /whoami"

WHOAMI=$(curl -sS -H "Authorization: Bearer $TOKEN" "$HS/_matrix/client/v3/account/whoami")
WHOAMI_USER=$(echo "$WHOAMI" | python3 -c "import json,sys; print(json.load(sys.stdin).get('user_id',''))")
if [ "$WHOAMI_USER" != "$EXPECTED_MXID" ]; then
  echo "[fail] /whoami returned $WHOAMI_USER, not $EXPECTED_MXID" >&2
  echo "  body: $WHOAMI" >&2
  echo
  echo "Fall back to mint_token.sh."
  exit 1
fi
echo "[3/6] /whoami → $WHOAMI_USER ✓"

TOKEN="$TOKEN" python3 - <<PY
import os
from pathlib import Path
p = Path("$ENV_LOCAL")
out, found = [], False
for line in p.read_text().splitlines():
    if line.startswith("KNOCK_APPROVER_TOKEN="):
        out.append(f"KNOCK_APPROVER_TOKEN={os.environ['TOKEN']}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"KNOCK_APPROVER_TOKEN={os.environ['TOKEN']}")
p.write_text("\n".join(out) + "\n")
PY
echo "[4/6] local .env updated"

printf '%s' "$TOKEN" | gh secret set KNOCK_APPROVER_TOKEN --repo "$REPO" >/dev/null
echo "[5/6] gh secret updated"

gh workflow enable deploy.yml --repo "$REPO" >/dev/null 2>&1 || true
gh workflow run deploy.yml --ref main --repo "$REPO" >/dev/null
echo "[6/6] deploy triggered"

echo
echo "Watch:  gh run list --workflow deploy.yml --repo $REPO --limit 1"
