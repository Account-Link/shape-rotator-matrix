#!/usr/bin/env bash
# Mint a fresh @shape-rotator-2 access token, push it to local .env + the
# matching GitHub Actions secret, then re-enable + trigger the deploy
# workflow. Used to recover when the running CVM's KNOCK_APPROVER_TOKEN
# has been invalidated (or when rotating on a schedule).
#
# Usage:
#   SHAPE_ROTATOR_2_PASSWORD='your-password' bash deploy/admin/mint_token.sh
#
# What it does, in order:
#   1. POST /_matrix/client/v3/login as @shape-rotator-2 → fresh access_token
#   2. Validate via /whoami (refuses to proceed if mxid doesn't match)
#   3. Update local .env's KNOCK_APPROVER_TOKEN
#   4. Update GitHub secret KNOCK_APPROVER_TOKEN (token never echoed)
#   5. Re-enable the deploy workflow if disabled
#   6. Trigger a deploy run
set -euo pipefail

HS=https://mtrx.shaperotator.xyz
EXPECTED_MXID='@shape-rotator-2:mtrx.shaperotator.xyz'
REPO=Account-Link/shape-rotator-matrix

# Resolve repo root from the script's location so this works regardless of
# the cwd `bash deploy/admin/mint_token.sh` is invoked from.
REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
ENV_LOCAL="$REPO_ROOT/.env"

PASSWORD="${SHAPE_ROTATOR_2_PASSWORD:-}"
if [ -z "$PASSWORD" ]; then
  echo "FAIL: set SHAPE_ROTATOR_2_PASSWORD env var first" >&2
  echo "  e.g.  SHAPE_ROTATOR_2_PASSWORD='xxx' bash deploy/admin/mint_token.sh" >&2
  exit 1
fi

# --- 1. Login ---
LOGIN_RESP=$(PASSWORD="$PASSWORD" python3 - <<'PY'
import json, os, urllib.request
body = json.dumps({
  "type": "m.login.password",
  "identifier": {"type": "m.id.user", "user": "shape-rotator-2"},
  "password": os.environ["PASSWORD"],
  "initial_device_display_name": "knock-approver",
}).encode()
req = urllib.request.Request(
  "https://mtrx.shaperotator.xyz/_matrix/client/v3/login",
  data=body, method="POST", headers={"Content-Type": "application/json"})
try:
  with urllib.request.urlopen(req, timeout=15) as r:
    print(r.read().decode())
except Exception as e:
  print(json.dumps({"error": str(e)}))
PY
)

TOKEN=$(echo "$LOGIN_RESP"  | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))")
DEVICE_ID=$(echo "$LOGIN_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('device_id',''))")

if [ -z "$TOKEN" ]; then
  echo "FAIL login: $LOGIN_RESP" >&2
  exit 1
fi
echo "[1/5] login ok — got fresh access_token (${#TOKEN} bytes), device_id=$DEVICE_ID"

# --- 2. Validate ---
WHOAMI=$(curl -sS -H "Authorization: Bearer $TOKEN" "$HS/_matrix/client/v3/account/whoami")
WHOAMI_USER=$(echo "$WHOAMI" | python3 -c "import json,sys; print(json.load(sys.stdin).get('user_id',''))")
if [ "$WHOAMI_USER" != "$EXPECTED_MXID" ]; then
  echo "FAIL: /whoami returned $WHOAMI_USER, not $EXPECTED_MXID" >&2
  echo "  body: $WHOAMI" >&2
  exit 1
fi
echo "[2/5] /whoami → $WHOAMI_USER ✓"

# --- 3. Update local .env ---
TOKEN="$TOKEN" ENV_LOCAL="$ENV_LOCAL" python3 - <<'PY'
import os
from pathlib import Path
p = Path(os.environ["ENV_LOCAL"])
token = os.environ["TOKEN"]
out, found = [], False
for line in p.read_text().splitlines():
    if line.startswith("KNOCK_APPROVER_TOKEN="):
        out.append(f"KNOCK_APPROVER_TOKEN={token}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"KNOCK_APPROVER_TOKEN={token}")
p.write_text("\n".join(out) + "\n")
PY
echo "[3/5] local .env updated: $ENV_LOCAL"

# --- 4. Update GH secret ---
printf '%s' "$TOKEN" | gh secret set KNOCK_APPROVER_TOKEN --repo "$REPO" >/dev/null
echo "[4/5] gh secret KNOCK_APPROVER_TOKEN updated on $REPO"

# --- 5. Re-enable + trigger deploy ---
gh workflow enable deploy.yml --repo "$REPO" >/dev/null 2>&1 || true
gh workflow run deploy.yml --ref main --repo "$REPO" >/dev/null
echo "[5/5] deploy workflow re-enabled + triggered"

echo
echo "Watch the deploy:"
echo "  gh run list --workflow deploy.yml --repo $REPO --limit 1"
echo "  gh run watch <id> --repo $REPO"
