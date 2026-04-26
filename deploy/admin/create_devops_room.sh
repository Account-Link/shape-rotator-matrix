#!/usr/bin/env bash
# Create the #matrix-devops child room of the Shape Rotator space.
#
# - Cleartext (no m.room.encryption) so the existing raw-HTTP /sync in
#   approver.py can read admin commands without needing mautrix.
# - Restricted join rule pointing at the space, so any space member auto-
#   joins. Also explicitly invites the configured admins.
# - Linked as a child of the space via m.space.child state.
#
# Idempotent-ish: refuses to recreate if an alias already resolves. Safe
# to re-run if a previous attempt failed midway — pass an existing room
# id as $1 to skip creation and only patch state.
set -euo pipefail

cd "$(dirname "$0")/../.."
TOKEN=$(grep -E '^KNOCK_APPROVER_TOKEN=' .env | cut -d= -f2-)
SPACE_ID=$(grep -E '^SHAPEROTATOR_SPACE_ID=' .env | cut -d= -f2-)
ALLOWLIST=$(grep -E '^ADMIN_ALLOWLIST=' .env | cut -d= -f2- || echo '')

HS=https://mtrx.shaperotator.xyz
ALIAS_LOCAL=matrix-devops
ALIAS_FULL="#${ALIAS_LOCAL}:mtrx.shaperotator.xyz"

[ -n "$TOKEN" ] || { echo "FAIL: no KNOCK_APPROVER_TOKEN in .env" >&2; exit 1; }
[ -n "$SPACE_ID" ] || { echo "FAIL: no SHAPEROTATOR_SPACE_ID in .env" >&2; exit 1; }

ENC() { python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$1"; }

if [ "${1:-}" != "" ]; then
  ROOM="$1"
  echo "[reuse] using passed room id: $ROOM"
else
  # Pre-flight: does the alias already exist?
  ALIAS_RESP=$(curl -sS -H "Authorization: Bearer $TOKEN" \
    "$HS/_matrix/client/v3/directory/room/$(ENC "$ALIAS_FULL")")
  if echo "$ALIAS_RESP" | python3 -c "import json,sys; r=json.load(sys.stdin); sys.exit(0 if 'room_id' in r else 1)"; then
    ROOM=$(echo "$ALIAS_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['room_id'])")
    echo "[skip] $ALIAS_FULL already resolves to $ROOM — using it"
  else
    echo "[create] new room with alias $ALIAS_LOCAL"
    BODY=$(cat <<JSON
{
  "preset": "private_chat",
  "name": "Matrix DevOps",
  "topic": "Development discussion, admin commands, and deploy notifications for the Shape Rotator Matrix deployment.",
  "room_alias_name": "$ALIAS_LOCAL",
  "is_direct": false
}
JSON
)
    ROOM=$(curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      "$HS/_matrix/client/v3/createRoom" -d "$BODY" \
      | python3 -c "import json,sys; r=json.load(sys.stdin); print(r.get('room_id') or r)")
    if [ "${ROOM:0:1}" != "!" ]; then
      echo "FAIL createRoom: $ROOM" >&2
      exit 1
    fi
    echo "[ok] created $ROOM"
  fi
fi

ROOM_ENC=$(ENC "$ROOM")
SPACE_ENC=$(ENC "$SPACE_ID")

# Set restricted join_rule so space members auto-join
echo "[state] m.room.join_rules → restricted (allow space members)"
curl -sS -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$HS/_matrix/client/v3/rooms/$ROOM_ENC/state/m.room.join_rules" \
  -d "$(python3 -c "import json,sys; print(json.dumps({'join_rule':'restricted','allow':[{'type':'m.room_membership','room_id':sys.argv[1]}]}))" "$SPACE_ID")" >/dev/null

# Link as space child
echo "[state] m.space.child on space → $ROOM (auto_join=true)"
curl -sS -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$HS/_matrix/client/v3/rooms/$SPACE_ENC/state/m.space.child/$ROOM_ENC" \
  -d '{"via":["mtrx.shaperotator.xyz"],"suggested":true,"auto_join":true}' >/dev/null

# Bump every allowlisted admin to PL 50 in the room (so commands work even
# if we later want PL-gated semantics in addition to allowlist).
if [ -n "$ALLOWLIST" ]; then
  echo "[state] m.room.power_levels → bumping allowlist to PL 50"
  PL_USERS=$(ALLOWLIST="$ALLOWLIST" python3 -c "
import json, os
mxids = [m.strip() for m in os.environ['ALLOWLIST'].split(',') if m.strip()]
print(json.dumps({m: 50 for m in mxids}))")
  CUR_PL=$(curl -sS -H "Authorization: Bearer $TOKEN" \
    "$HS/_matrix/client/v3/rooms/$ROOM_ENC/state/m.room.power_levels")
  NEW_PL=$(python3 -c "
import json, sys
cur = json.loads(sys.argv[1])
add = json.loads(sys.argv[2])
cur.setdefault('users', {})
cur['users'].update(add)
print(json.dumps(cur))
" "$CUR_PL" "$PL_USERS")
  curl -sS -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    "$HS/_matrix/client/v3/rooms/$ROOM_ENC/state/m.room.power_levels" \
    -d "$NEW_PL" >/dev/null
fi

# Invite each allowlisted admin
if [ -n "$ALLOWLIST" ]; then
  for mxid in $(echo "$ALLOWLIST" | tr ',' ' '); do
    echo "[invite] $mxid"
    curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      "$HS/_matrix/client/v3/rooms/$ROOM_ENC/invite" \
      -d "$(python3 -c "import json,sys; print(json.dumps({'user_id':sys.argv[1]}))" "$mxid")" \
      | head -c 200
    echo
  done
fi

echo
echo "DONE."
echo "  room_id   = $ROOM"
echo "  alias     = $ALIAS_FULL"
echo
echo "Next:"
echo "  printf '%s' '$ROOM' | gh secret set ADMIN_COMMAND_ROOM --repo Account-Link/shape-rotator-matrix"
echo "  echo 'ADMIN_COMMAND_ROOM=$ROOM' >> .env  # (or edit existing line)"
