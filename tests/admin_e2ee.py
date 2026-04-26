"""End-to-end test of admin commands in an encrypted room.

Covers the mautrix migration: the bot decrypts incoming `!mint` (and
friends) sent in an E2EE room and replies with an encrypted message
that the test client can decrypt back. If the migration regresses, this
test fails — the haiku captcha isn't enough to cover this path.

What it asserts:
  1. A mautrix client signed in as the admin user (allowlisted) joins
     the encrypted ADMIN_COMMAND_ROOM.
  2. Sends `!mint --uses 3 admin-e2ee-test` via send_message_event
     (auto-encrypted because the room has m.room.encryption).
  3. Within ~30s, sees a reply event from @admin (the bot in this
     test stack) whose decrypted body contains a `/join?code=…` URL.
  4. The minted code shows up in `/data/codes.json` on the bot side
     (validated indirectly via /signup-or-knock or /codes — for v1
     we just trust the URL).

Env (set by run_in_runner.sh):
  DEV_HS, DEV_REG_TOKEN, ADMIN_COMMAND_ROOM, ADMIN_MXID
"""
import asyncio, json, os, secrets, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

# Reuse the make_client + sync_once helpers from the existing E2EE test.
from sas_e2e import make_client, sync_once, register

from mautrix.types import (EventType, MessageType, TextMessageEventContent)

HS = os.environ.get("DEV_HS", "http://landing:80").rstrip("/")
REG_TOKEN = os.environ["DEV_REG_TOKEN"]
ADMIN_ROOM = os.environ["ADMIN_COMMAND_ROOM"]

# The bot's own mxid in the test stack — the bootstrap admin user is
# the bot here (single-user test env). The PROD bot is @shape-rotator-2;
# in prod tests we'd use that instead.
BOT_MXID_HINT = os.environ.get("ADMIN_MXID", "")

results = []
def log(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    results.append((name, ok))


async def main():
    # The bootstrap admin is also our bot AND on the implicit allowlist
    # (it has PL 100 in the space → passes the PL gate). For the test we
    # need a SECOND user who is also on the allowlist OR has PL ≥ 50, and
    # who has E2EE-capable client to send the !mint.
    #
    # Simpler shape for v1: register a fresh user, set ADMIN_ALLOWLIST
    # via a !mint shouldn't actually need it — wait, the bot does need
    # to see this user as admin. Easiest: bump PL of the new user in
    # ADMIN_ROOM to 50 via an admin token call.
    username = f"admin_e2ee_{int(time.time())}_{secrets.token_hex(2)}"
    device   = f"E2EEADMIN{secrets.token_hex(2)}"
    user_mxid, user_token = register(username, secrets.token_urlsafe(32), device)
    print(f"[admin_e2ee] test user: {user_mxid} device={device}", flush=True)

    # Bump PL of the test user in ADMIN_ROOM so they pass the bot's
    # _is_admin gate. The bootstrap admin (= bot here) has PL 100 and
    # can write power_levels.
    import urllib.request, urllib.parse
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    if not admin_token:
        print("FAIL: no ADMIN_TOKEN env (needed to bump PL on test user)", file=sys.stderr)
        sys.exit(2)

    pl_url = f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(ADMIN_ROOM)}/state/m.room.power_levels"
    # Read current
    req = urllib.request.Request(pl_url, headers={"Authorization": f"Bearer {admin_token}"})
    cur_pl = json.loads(urllib.request.urlopen(req).read())
    cur_pl.setdefault("users", {})
    cur_pl["users"][user_mxid] = 50
    # Write back
    body = json.dumps(cur_pl).encode()
    req = urllib.request.Request(pl_url, data=body, method="PUT",
                                  headers={"Authorization": f"Bearer {admin_token}",
                                           "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        log("bumped test user PL to 50 in admin room", r.status == 200, f"status={r.status}")

    # Invite + auto-accept invite to ADMIN_ROOM via raw HTTP (it's encrypted,
    # but join is a state event, no encryption needed). Note: room is
    # restricted-join via space membership, but we're not in the space
    # yet — explicit invite is the simpler path here.
    inv_url = f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(ADMIN_ROOM)}/invite"
    req = urllib.request.Request(inv_url, method="POST",
        data=json.dumps({"user_id": user_mxid}).encode(),
        headers={"Authorization": f"Bearer {admin_token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            log("invited test user to admin room", r.status == 200, f"status={r.status}")
    except Exception as e:
        log("invited test user to admin room", False, f"err={e}")

    # Spin up mautrix client + accept invite + start syncing.
    client, cs, ss, db = await make_client(
        user_mxid, user_token, device,
        db_path=f"/tmp/admin_e2ee_{secrets.token_hex(4)}.db")
    await client.crypto.share_keys()

    # Accept invite via raw join.
    join_url = f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(ADMIN_ROOM)}/join"
    req = urllib.request.Request(join_url, method="POST", data=b"{}",
        headers={"Authorization": f"Bearer {user_token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        log("test user joined admin room", r.status == 200, f"status={r.status}")

    # Initial sync to learn room state + member device keys.
    for _ in range(3):
        await sync_once(client, ss, timeout=2000, first=True)

    enc = await ss.is_encrypted(ADMIN_ROOM)
    log("admin room reports encrypted", bool(enc))

    # Send !mint via mautrix's send_message_event — auto-encrypts.
    label = f"admin-e2ee-{secrets.token_hex(3)}"
    cmd = f"!mint --uses 3 {label}"
    sent_id = await client.send_message_event(
        ADMIN_ROOM, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=cmd))
    log("sent encrypted !mint command", bool(sent_id), f"event_id={sent_id}")

    # Poll for the bot's reply: decrypted body should contain the label
    # we passed AND a /join?code= URL.
    deadline = time.time() + 45
    seen_reply = None
    received = asyncio.Event()

    all_seen = []
    async def on_msg(evt):
        nonlocal seen_reply
        body = (getattr(evt.content, "body", "") or "")
        if str(evt.room_id) == ADMIN_ROOM and str(evt.sender) != user_mxid:
            all_seen.append((str(evt.sender), body))
            print(f"[admin_e2ee] received from {evt.sender}: {body[:100]!r}", flush=True)
        if evt.room_id != ADMIN_ROOM:
            return
        if evt.sender == user_mxid:
            return  # our own !mint command echoing back
        if "/join?code=" in body and label in body:
            seen_reply = body
            received.set()

    client.add_event_handler(EventType.ROOM_MESSAGE, on_msg)
    while time.time() < deadline and not received.is_set():
        await sync_once(client, ss, timeout=2000)
    log("bot replied with encrypted !mint result",
        seen_reply is not None,
        f"body[:120]={(seen_reply or '')[:120]!r} ; all_seen={all_seen!r}")
    if seen_reply:
        # Pull the URL out and confirm shape
        import re
        m = re.search(r"https?://\S+/join\?code=(\S+)", seen_reply)
        log("reply contains a /join URL with code",
            bool(m),
            f"code={m.group(1) if m else None!r}")

    await db.stop()

    failed = [name for name, ok in results if not ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} pass ===")
    if failed:
        print("FAILED: " + ", ".join(failed), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
