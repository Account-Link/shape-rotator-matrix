#!/usr/bin/env bash
# bridge/deploy-pod.sh — deploy the Matrix<->Telegram relay to a tee-daemon pod.
#
#   bash bridge/deploy-pod.sh
#
# Not a CVM of its own: the relay is one `runtime: image` tenant on an existing
# pod, alongside the other webhost-apps. It has nothing to do with the
# continuwuity CVM that hosts the homeserver.
#
# Secrets are read from disk at run time and travel ONLY in the deploy POST's
# manifest env — never committed, never printed. Same pattern as
# webhost-apps/router-dashboard/deploy.sh.
#
#   TEE_DAEMON_TOKEN       <- ~/.oauth3-prod-secrets.env   (verified canonical
#                             for pod.dstack; ~/.claude/tee-daemon-token is
#                             STALE and 403s -- see webhost-apps/REGISTRY.md)
#   TELEGRAM_BOT_TOKEN     <- ~/.shape-bridge-bot/telegram-bot-token
#   MATRIX_BRIDGE_PASSWORD <- $MATRIX_BRIDGE_PASSWORD, else
#                             ~/.shape-bridge-bot/matrix-password
#
# The bot mints its own Matrix access_token and device_id from that password on
# first boot, into the named volume. No credential FILE is ever copied to the
# pod -- only the password, in sealed env.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CVM="${CVM:-https://pod.dstack.soc1024.com}"
NAME="${NAME:-mx-tg-relay}"
IMAGE="${IMAGE:-ghcr.io/amiller/shape-mx-tg-relay}"
TAG="${TAG:-latest}"

MATRIX_USER="${MATRIX_BRIDGE_USER:-shape-bridge}"
MATRIX_HS="${MATRIX_HOMESERVER:-https://mtrx.shaperotator.xyz}"

die() { printf 'deploy-pod: %s\n' "$*" >&2; exit 1; }

# --- secrets (read, never echoed) -------------------------------------------
SECRETS_ENV="$HOME/.oauth3-prod-secrets.env"
# Tokens are PER-POD. The wrong one still passes read-only GETs and only fails
# on write (403 "invalid token or scope"), so aim it explicitly with TOKEN_FILE.
DAEMON_TOKEN="${TEE_DAEMON_TOKEN:-}"
if [ -z "$DAEMON_TOKEN" ] && [ -n "${TOKEN_FILE:-}" ] && [ -f "$TOKEN_FILE" ]; then
  case "$TOKEN_FILE" in
    *.env) DAEMON_TOKEN="$(grep -m1 '^TEE_DAEMON_TOKEN=' "$TOKEN_FILE" | cut -d= -f2-)" ;;
    *)     DAEMON_TOKEN="$(tr -d '[:space:]' <"$TOKEN_FILE")" ;;
  esac
fi
if [ -z "$DAEMON_TOKEN" ] && [ -f "$SECRETS_ENV" ]; then
  DAEMON_TOKEN="$(grep -m1 '^TEE_DAEMON_TOKEN=' "$SECRETS_ENV" | cut -d= -f2-)"
fi
[ -n "$DAEMON_TOKEN" ] || die "no TEE_DAEMON_TOKEN (looked in \$TEE_DAEMON_TOKEN and $SECRETS_ENV)"

TG_TOKEN_FILE="$HOME/.shape-bridge-bot/telegram-bot-token"
TG_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
[ -z "$TG_TOKEN" ] && [ -f "$TG_TOKEN_FILE" ] && TG_TOKEN="$(tr -d '[:space:]' <"$TG_TOKEN_FILE")"
[ -n "$TG_TOKEN" ] || die "no TELEGRAM_BOT_TOKEN (looked in \$TELEGRAM_BOT_TOKEN and $TG_TOKEN_FILE)"

MX_PW_FILE="$HOME/.shape-bridge-bot/matrix-password"
MX_PW="${MATRIX_BRIDGE_PASSWORD:-}"
[ -z "$MX_PW" ] && [ -f "$MX_PW_FILE" ] && MX_PW="$(tr -d '[:space:]' <"$MX_PW_FILE")"
[ -n "$MX_PW" ] || die "no MATRIX_BRIDGE_PASSWORD (looked in \$MATRIX_BRIDGE_PASSWORD and $MX_PW_FILE)"

# --- fixtures: the ONLY venues this relay may touch --------------------------
FIXTURES="$HOME/.teleport-travel/test-fixtures.json"
[ -f "$FIXTURES" ] || die "missing fixtures $FIXTURES"
python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
assert str(d.get("matrix_room_id","")).startswith("!"), "bad matrix_room_id"
assert d.get("telegram_chat_id"), "missing telegram_chat_id"' "$FIXTURES" || die "fixtures invalid"
ROOM="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["matrix_room_id"])' "$FIXTURES")"
CHAT="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["telegram_chat_id"])' "$FIXTURES")"

echo "deploy-pod: $NAME -> $CVM"
echo "  room=$ROOM  chat=$CHAT  matrix_user=$MATRIX_USER"

# --- build + push ------------------------------------------------------------
echo "deploy-pod: building $IMAGE:$TAG"
docker build -t "$IMAGE:$TAG" -f "$REPO/bridge/Dockerfile" "$REPO/bridge"
echo "deploy-pod: pushing (ghcr packages default to PRIVATE on first push --"
echo "            the deploy 500s until visibility is flipped to public, web UI only)"
docker push "$IMAGE:$TAG"
DIGEST="$(docker inspect "$IMAGE:$TAG" --format '{{index .RepoDigests 0}}')"
echo "deploy-pod: pinned $DIGEST"

# --- deploy ------------------------------------------------------------------
# The fixtures ride along as env so the container needs no bind mount; relay.py
# still refuses to touch anything but this pair.
payload="$(TG_TOKEN="$TG_TOKEN" MX_PW="$MX_PW" python3 - "$DIGEST" "$NAME" "$ROOM" "$CHAT" "$MATRIX_USER" "$MATRIX_HS" <<'PY'
import json, os, sys
digest, name, room, chat, user, hs = sys.argv[1:7]
print(json.dumps({
    "name": name,
    "runtime": "image",
    "image": digest,
    "image_port": 8080,
    "volumes": [{"name": f"{name}-data", "mount": "/data"}],
    "env": {
        "TELEGRAM_BOT_TOKEN": os.environ["TG_TOKEN"],
        "MATRIX_BRIDGE_USER": user,
        "MATRIX_BRIDGE_PASSWORD": os.environ["MX_PW"],
        "MATRIX_HOMESERVER": hs,
        "MATRIX_ROOM_ID": room,
        "TELEGRAM_CHAT_ID": str(chat),
    },
}))
PY
)"

code="$(TG_TOKEN="$TG_TOKEN" MX_PW="$MX_PW" curl -s -o /tmp/deploy-pod-resp.json -w '%{http_code}' \
  -X POST "$CVM/_api/projects" \
  -H "Authorization: Bearer $DAEMON_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$payload")"

echo "deploy-pod: HTTP $code"
head -c 600 /tmp/deploy-pod-resp.json; echo
[ "$code" = "200" ] || [ "$code" = "201" ] || die "deploy failed (HTTP $code)"

echo "deploy-pod: health -> $CVM/$NAME/health"
curl -s "$CVM/$NAME/health" | head -c 400; echo
