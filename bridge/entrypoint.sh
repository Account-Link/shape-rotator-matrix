#!/bin/sh
# Materialise the fixture pair into the volume, then run the relay.
#
# mx.py and tg.py read the pairing from a FILE, not the environment — the
# "only these two venues" rule is enforced by test-fixtures.json. A pod tenant
# has no bind mount to supply one, so the sealed env carries the pair and this
# writes it once into /data.
#
# Written only if absent: after first boot the volume's copy wins, so a
# re-pairing is a deliberate operator action (edit the file or wipe the volume),
# never a silent consequence of redeploying with different env.
set -eu

FIXTURES=/data/test-fixtures.json

if [ ! -f "$FIXTURES" ]; then
  : "${MATRIX_ROOM_ID:?MATRIX_ROOM_ID required to seed $FIXTURES}"
  : "${TELEGRAM_CHAT_ID:?TELEGRAM_CHAT_ID required to seed $FIXTURES}"
  mkdir -p /data
  python3 - "$MATRIX_ROOM_ID" "$TELEGRAM_CHAT_ID" "$FIXTURES" <<'PY'
import json, sys
room, chat, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
if not room.startswith("!"):
    raise SystemExit(f"refusing suspicious MATRIX_ROOM_ID: {room!r}")
json.dump({
    "matrix_room_id": room,
    "telegram_chat_id": chat,
    "note": "Seeded from sealed env on first boot. The ONLY venues bridge code may send to.",
}, open(path, "w"), indent=2)
print(f"seeded {path}: room={room} chat={chat}", flush=True)
PY
fi

# Bot token likewise: env is the transport, the volume is the source of truth
# tg.py reads.
if [ ! -f /data/telegram-bot-token ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  umask 077
  printf '%s\n' "$TELEGRAM_BOT_TOKEN" > /data/telegram-bot-token
fi

exec "$@"
