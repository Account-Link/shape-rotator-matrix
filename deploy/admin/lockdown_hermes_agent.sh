#!/usr/bin/env bash
# Lock down the shape-rotator hermes profile so it refuses cleartext
# inputs from anyone (including @socrates1024). The patch is already
# in the hermes image — patch_matrix_require_e2ee.py wraps
# _on_room_message so MATRIX_REQUIRE_ENCRYPTION=true refuses AND
# auto-leaves non-E2EE rooms. We just have to set the env var.
#
# Default: read-only inspect (prints current state, no changes).
# Set APPLY=1 to actually update + restart hermes container.
#
#   bash deploy/admin/lockdown_hermes_agent.sh           # inspect
#   APPLY=1 bash deploy/admin/lockdown_hermes_agent.sh   # apply
set -uo pipefail

KEY=/home/amiller/projects/hermes-agent/deploy-notes/deploy_key
APPLY="${APPLY:-0}"

phala ssh hermes-staging -- -i "$KEY" APPLY="$APPLY" bash -s <<'REMOTE'
set -uo pipefail
APPLY=${APPLY:-0}
PROFILE=/root/.hermes/profiles/shape-rotator
ENV_FILE=$PROFILE/.env

echo "=== current state ==="
echo "profile dir: $PROFILE"
docker ps --format '{{.Names}}\t{{.Status}}' | grep -i hermes || echo "(no hermes container?!)"

echo
echo "=== relevant env vars in $ENV_FILE ==="
HERMES_CID=$(docker ps --format '{{.Names}}' | grep -E 'hermes' | head -n1)
if [ -z "$HERMES_CID" ]; then
  echo "FAIL: no running hermes container found" >&2
  exit 1
fi
docker exec "$HERMES_CID" sh -c "
  if [ ! -f '$ENV_FILE' ]; then
    echo '  $ENV_FILE: MISSING'
    exit 0
  fi
  for k in MATRIX_HOMESERVER MATRIX_USER_ID MATRIX_DEVICE_ID MATRIX_ENCRYPTION MATRIX_REQUIRE_ENCRYPTION MATRIX_ALLOWED_USERS MATRIX_REQUIRE_MENTION; do
    line=\$(grep -E \"^\$k=\" '$ENV_FILE' || true)
    if [ -z \"\$line\" ]; then
      echo \"  \$k: (unset)\"
    else
      # Print key + length only, never the secret value (only show booleans)
      val=\${line#*=}
      if [ \"\$val\" = 'true' ] || [ \"\$val\" = 'false' ]; then
        echo \"  \$line\"
      else
        echo \"  \$k=<\${#val} bytes>\"
      fi
    fi
  done
"

if [ "$APPLY" != "1" ]; then
  echo
  echo "=== read-only mode — re-run with APPLY=1 to update ==="
  exit 0
fi

echo
echo "=== applying MATRIX_REQUIRE_ENCRYPTION=true (dedup any existing entries) ==="
docker exec "$HERMES_CID" sh -c "
  set -e
  # Remove ALL existing lines (handles dupes), then append a single canonical one.
  sed -i '/^MATRIX_REQUIRE_ENCRYPTION=/d' '$ENV_FILE'
  echo 'MATRIX_REQUIRE_ENCRYPTION=true' >> '$ENV_FILE'
  echo 'updated lines:'
  grep -n '^MATRIX_REQUIRE_ENCRYPTION=' '$ENV_FILE'
"

echo
echo "=== restarting hermes container so the new env takes effect ==="
docker restart "$HERMES_CID"
echo "restarted $HERMES_CID"

echo
echo "=== post-restart state ==="
sleep 5
docker ps --format '{{.Names}}\t{{.Status}}' | grep -i hermes
docker exec "$HERMES_CID" grep '^MATRIX_REQUIRE_ENCRYPTION=' "$ENV_FILE" 2>/dev/null || echo "(could not re-read)"
REMOTE
