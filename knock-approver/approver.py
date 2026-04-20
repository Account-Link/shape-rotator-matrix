"""Auto-approve Matrix knocks on the Shape Rotator space when the knock reason
matches an entry in codes.json.

Env:
  HS                 homeserver URL (https://mtrx.shaperotator.xyz)
  MATRIX_TOKEN       access token for a user with PL >= 50 in the space
  SPACE_ID           e.g. !4FL8uL5...:mtrx.shaperotator.xyz

State file (bind-mounted volume):
  /data/codes.json   {"code": {"uses_remaining": N, "label": "..."}}

Appends to /data/log.jsonl for auditability.
"""
import asyncio, json, os, sys, time
from pathlib import Path
import aiohttp

HS          = os.environ["HS"].rstrip("/")
TOKEN       = os.environ["MATRIX_TOKEN"]
SPACE_ID    = os.environ["SPACE_ID"]
CODES_PATH  = Path(os.environ.get("CODES_PATH", "/data/codes.json"))
LOG_PATH    = Path(os.environ.get("LOG_PATH",   "/data/log.jsonl"))
SYNC_STATE  = Path(os.environ.get("SYNC_STATE", "/data/sync_since.txt"))

HEADERS = {"Authorization": f"Bearer {TOKEN}"}

def load_codes():
    if CODES_PATH.exists():
        return json.loads(CODES_PATH.read_text())
    return {}

def save_codes(codes):
    tmp = CODES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(codes, indent=2, sort_keys=True))
    tmp.replace(CODES_PATH)

def audit(event):
    event["ts"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(event) + "\n")

async def approve(session, room_id, user_id, reason_code):
    url = f"{HS}/_matrix/client/v3/rooms/{room_id}/invite"
    async with session.post(url, json={"user_id": user_id, "reason": "auto-approved"}) as r:
        body = await r.text()
        return r.status, body

async def handle_knock(session, room_id, user_id, reason):
    code = (reason or "").strip()
    codes = load_codes()
    entry = codes.get(code)
    if not entry or entry.get("uses_remaining", 0) <= 0:
        audit({"type": "rejected", "user": user_id, "room": room_id, "reason": reason, "why": "no valid code"})
        print(f"[rejected] {user_id} reason={reason!r}", flush=True)
        return
    status, body = await approve(session, room_id, user_id, code)
    if status == 200:
        entry["uses_remaining"] -= 1
        codes[code] = entry
        save_codes(codes)
        audit({"type": "approved", "user": user_id, "room": room_id, "code": code, "uses_left": entry["uses_remaining"]})
        print(f"[approved] {user_id} via code={code} (uses_left={entry['uses_remaining']})", flush=True)
    else:
        audit({"type": "invite_failed", "user": user_id, "room": room_id, "code": code, "status": status, "body": body[:200]})
        print(f"[invite_failed] {user_id} status={status} body={body[:200]}", flush=True)

def iter_knock_events(rooms_data):
    # After a user knocks, the space sees an m.room.member with membership=knock.
    # Events can arrive in either state or timeline, under rooms.join.<space>.
    for room_id, rd in rooms_data.get("join", {}).items():
        if room_id.split(":", 1)[0] != SPACE_ID.split(":", 1)[0]:
            continue
        for section in ("timeline", "state"):
            for ev in rd.get(section, {}).get("events", []):
                if ev.get("type") != "m.room.member":
                    continue
                content = ev.get("content") or {}
                if content.get("membership") != "knock":
                    continue
                yield room_id, ev["state_key"], content.get("reason", "")

async def main():
    print(f"knock-approver starting. space={SPACE_ID}", flush=True)
    CODES_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CODES_PATH.exists():
        save_codes({})

    # Merge any seed codes from INITIAL_CODES env var into the on-disk file.
    # Missing codes are added; existing codes are left alone.
    initial = os.environ.get("INITIAL_CODES", "").strip()
    if initial:
        try:
            seed = json.loads(initial)
            codes = load_codes()
            added = 0
            for k, v in seed.items():
                if k not in codes:
                    codes[k] = v
                    added += 1
            if added:
                save_codes(codes)
                print(f"seeded {added} new code(s) from INITIAL_CODES", flush=True)
        except Exception as e:
            print(f"INITIAL_CODES parse error: {e}", flush=True)

    since = SYNC_STATE.read_text().strip() if SYNC_STATE.exists() else None

    timeout = aiohttp.ClientTimeout(total=None, sock_read=45)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as s:
        while True:
            params = {"timeout": "30000"}
            if since:
                params["since"] = since
            try:
                async with s.get(f"{HS}/_matrix/client/v3/sync", params=params) as r:
                    if r.status != 200:
                        body = await r.text()
                        print(f"[sync {r.status}] {body[:300]}", flush=True)
                        await asyncio.sleep(5)
                        continue
                    data = await r.json()
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[sync error] {type(e).__name__}: {e}", flush=True)
                await asyncio.sleep(5)
                continue

            since = data["next_batch"]
            SYNC_STATE.write_text(since)

            for room_id, user_id, reason in iter_knock_events(data.get("rooms", {})):
                await handle_knock(s, room_id, user_id, reason)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
