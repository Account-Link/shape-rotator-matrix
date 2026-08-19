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
room, chats, path = sys.argv[1], sys.argv[2], sys.argv[3]
if not room.startswith("!"):
    raise SystemExit(f"refusing suspicious MATRIX_ROOM_ID: {room!r}")
# TELEGRAM_CHAT_ID may name several groups, comma separated. Hub-and-spoke:
# each mirrors with the room; they do not see each other.
ids = [int(x) for x in chats.replace(" ", "").split(",") if x]
if not ids:
    raise SystemExit("TELEGRAM_CHAT_ID had no usable chat ids")
json.dump({
    "matrix_room_id": room,
    "telegram_chat_ids": ids,
    "note": "Seeded from sealed env on first boot. The ONLY venues bridge code may send to.",
}, open(path, "w"), indent=2)
print(f"seeded {path}: room={room} chats={ids}", flush=True)
PY
else
  # Fixtures already exist in the volume. Reconcile the TELEGRAM chat list from
  # env, because adding or removing a group is a normal operator change and the
  # env only changes via a deploy. Log it loudly -- a re-pairing should never be
  # something you discover later.
  #
  # The MATRIX ROOM is deliberately NOT reconciled: moving rooms strands the
  # megolm sessions this device holds, so a mismatch fails loudly and the
  # operator decides (wipe the volume for a clean re-pair, or fix the env).
  python3 - "$FIXTURES" "${MATRIX_ROOM_ID:-}" "${TELEGRAM_CHAT_ID:-}" <<'PY'
import json, sys
path, room_env, chats_env = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(path))
cur_room = d.get("matrix_room_id")
if room_env and room_env != cur_room:
    raise SystemExit(
        f"MATRIX_ROOM_ID={room_env!r} but the volume is paired to {cur_room!r}. "
        "Refusing to re-point a live device at a different room: its megolm "
        "sessions belong to the old one. Wipe the volume to re-pair deliberately.")
cur = d.get("telegram_chat_ids") or ([d["telegram_chat_id"]] if "telegram_chat_id" in d else [])
cur = [int(x) for x in cur]
if chats_env:
    want = [int(x) for x in chats_env.replace(" ", "").split(",") if x]
    if want and want != cur:
        d.pop("telegram_chat_id", None)
        d["telegram_chat_ids"] = want
        tmp = path + ".tmp"
        json.dump(d, open(tmp, "w"), indent=2)
        import os; os.replace(tmp, path)
        print(f"RE-PAIRED telegram chats: {cur} -> {want}", flush=True)
    else:
        print(f"telegram chats unchanged: {cur}", flush=True)
PY
fi

# Bot token likewise: env is the transport, the volume is the source of truth
# tg.py reads.
if [ ! -f /data/telegram-bot-token ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  umask 077
  printf '%s\n' "$TELEGRAM_BOT_TOKEN" > /data/telegram-bot-token
fi

exec "$@"
