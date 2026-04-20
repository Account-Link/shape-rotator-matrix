"""Auto-approve Matrix knocks on the Shape Rotator space, AND proxy signups.

Two responsibilities, both running in one process:

1. **Knock approver** (long-running /sync loop).
   Watches the space for membership=knock events. When the knock reason matches
   an entry in /data/codes.json, POSTs /invite to approve.

2. **Signup auth proxy** (HTTP server on port 8001).
   POST /signup/api  body: {"code", "username", "password"}
   Validates code against /data/signup_codes.json, completes continuwuity
   registration using the server-side CONDUWUIT_REGISTRATION_TOKEN (never
   exposed to clients), and auto-invites the new account to the space.

Env:
  HS                           homeserver URL (https://mtrx.shaperotator.xyz)
  MATRIX_TOKEN                 access token for a user with PL >= 50 in the space
  SPACE_ID                     unsuffixed space room id
  CONDUWUIT_REGISTRATION_TOKEN shared reg token (kept server-side)
  INITIAL_CODES                JSON seed for knock codes
  INITIAL_SIGNUP_CODES         JSON seed for signup codes

State files on the knock-data volume:
  /data/codes.json          knock codes
  /data/signup_codes.json   signup codes
  /data/log.jsonl           audit log
  /data/sync_since.txt      /sync cursor
"""
import asyncio, json, os, sys, time
from pathlib import Path
import aiohttp
from aiohttp import web

HS            = os.environ["HS"].rstrip("/")
# Public-facing URL returned to signup clients (the client needs to point Element
# at the public name, not the internal docker hostname).
HS_PUBLIC     = os.environ.get("HS_PUBLIC", HS).rstrip("/")
TOKEN         = os.environ["MATRIX_TOKEN"]
SPACE_ID      = os.environ["SPACE_ID"]
REG_TOKEN     = os.environ.get("CONDUWUIT_REGISTRATION_TOKEN", "")
CODES_PATH    = Path(os.environ.get("CODES_PATH",        "/data/codes.json"))
SIGNUP_PATH   = Path(os.environ.get("SIGNUP_CODES_PATH", "/data/signup_codes.json"))
LOG_PATH      = Path(os.environ.get("LOG_PATH",          "/data/log.jsonl"))
SYNC_STATE    = Path(os.environ.get("SYNC_STATE",        "/data/sync_since.txt"))
HTTP_PORT     = int(os.environ.get("HTTP_PORT", "8001"))

AUTH = {"Authorization": f"Bearer {TOKEN}"}


# --- JSON-file helpers ---

def _load(path):
    if path.exists():
        return json.loads(path.read_text())
    return {}

def _save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)

def audit(event):
    event["ts"] = time.time()
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(event) + "\n")

def merge_seed(path, env_key):
    """Merge JSON from env var into the codes file; only adds missing keys."""
    seed = os.environ.get(env_key, "").strip()
    if not seed:
        return
    try:
        data = json.loads(seed)
    except Exception as e:
        print(f"{env_key} parse error: {e}", flush=True)
        return
    existing = _load(path)
    added = 0
    for k, v in data.items():
        if k not in existing:
            existing[k] = v
            added += 1
    if added:
        _save(path, existing)
        print(f"seeded {added} new entries into {path.name} from {env_key}", flush=True)


# --- Knock approval ---

async def approve_knock(session, room_id, user_id):
    url = f"{HS}/_matrix/client/v3/rooms/{room_id}/invite"
    async with session.post(url, json={"user_id": user_id, "reason": "auto-approved"}) as r:
        return r.status, await r.text()

async def handle_knock(session, room_id, user_id, reason):
    code = (reason or "").strip()
    codes = _load(CODES_PATH)
    entry = codes.get(code)
    if not entry or entry.get("uses_remaining", 0) <= 0:
        audit({"type": "knock_rejected", "user": user_id, "room": room_id, "reason": reason})
        print(f"[knock rejected] {user_id}", flush=True)
        return
    status, body = await approve_knock(session, room_id, user_id)
    if status == 200:
        entry["uses_remaining"] -= 1
        codes[code] = entry
        _save(CODES_PATH, codes)
        audit({"type": "knock_approved", "user": user_id, "room": room_id, "code": code,
               "uses_left": entry["uses_remaining"]})
        print(f"[knock approved] {user_id} via {code} (left={entry['uses_remaining']})", flush=True)
    else:
        audit({"type": "knock_invite_failed", "user": user_id, "room": room_id, "code": code,
               "status": status, "body": body[:200]})
        print(f"[knock failed] {user_id} status={status}", flush=True)

def iter_knock_events(rooms_data):
    for room_id, rd in rooms_data.get("join", {}).items():
        if room_id.split(":", 1)[0] != SPACE_ID.split(":", 1)[0]:
            continue
        for section in ("timeline", "state"):
            for ev in rd.get(section, {}).get("events", []):
                if ev.get("type") != "m.room.member":
                    continue
                c = ev.get("content") or {}
                if c.get("membership") != "knock":
                    continue
                yield room_id, ev["state_key"], c.get("reason", "")

async def sync_loop():
    since = SYNC_STATE.read_text().strip() if SYNC_STATE.exists() else None
    timeout = aiohttp.ClientTimeout(total=None, sock_read=45)
    async with aiohttp.ClientSession(headers=AUTH, timeout=timeout) as s:
        while True:
            params = {"timeout": "30000"}
            if since:
                params["since"] = since
            try:
                async with s.get(f"{HS}/_matrix/client/v3/sync", params=params) as r:
                    if r.status != 200:
                        print(f"[sync {r.status}] {(await r.text())[:300]}", flush=True)
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


# --- Signup auth proxy ---

def valid_username(u: str) -> bool:
    return (u.isascii() and 1 <= len(u) <= 32
            and all(c.isalnum() or c in "-_.=" for c in u))

async def signup_handler(request):
    if not REG_TOKEN:
        return web.json_response({"error": "signup_disabled"}, status=503)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)

    code     = (data.get("code") or "").strip()
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""

    if not (code and username and password):
        return web.json_response({"error": "missing_fields"}, status=400)
    if not valid_username(username):
        return web.json_response({"error": "bad_username"}, status=400)
    if len(password) < 12:
        return web.json_response({"error": "password_too_short"}, status=400)

    codes = _load(SIGNUP_PATH)
    entry = codes.get(code)
    if not entry or entry.get("uses_remaining", 0) <= 0:
        audit({"type": "signup_rejected", "username": username, "why": "invalid_code"})
        return web.json_response({"error": "invalid_code"}, status=403)

    async with aiohttp.ClientSession() as s:
        # Step 1: get a fresh UIA session
        async with s.post(f"{HS}/_matrix/client/v3/register", json={}) as r:
            if r.status == 401:
                session = (await r.json()).get("session")
            else:
                return web.json_response({"error": "register_init_unexpected",
                                          "status": r.status}, status=502)

        # Step 2: complete with server-side reg token
        body = {
            "auth": {"type": "m.login.registration_token",
                     "token": REG_TOKEN, "session": session},
            "username": username,
            "password": password,
            "initial_device_display_name": "shape-rotator signup",
        }
        async with s.post(f"{HS}/_matrix/client/v3/register", json=body) as r:
            result = await r.json()
            if r.status != 200:
                audit({"type": "signup_failed", "username": username,
                       "status": r.status, "body": str(result)[:300]})
                err = str(result.get("errcode", "register_failed")).lower()
                return web.json_response({"error": err,
                                          "detail": result.get("error")}, status=400)

    # Decrement code (only on success)
    entry["uses_remaining"] -= 1
    codes[code] = entry
    _save(SIGNUP_PATH, codes)

    # Auto-invite to the space (best effort)
    mxid = result["user_id"]
    try:
        async with aiohttp.ClientSession(
            headers={**AUTH, "Content-Type": "application/json"}
        ) as s:
            invite_url = f"{HS}/_matrix/client/v3/rooms/{SPACE_ID}/invite"
            async with s.post(invite_url,
                              json={"user_id": mxid, "reason": "signup auto-invite"}) as r:
                if r.status != 200:
                    print(f"[signup] invite of {mxid} failed: {r.status} "
                          f"{(await r.text())[:200]}", flush=True)
    except Exception as e:
        print(f"[signup] invite error: {e}", flush=True)

    audit({"type": "signup_ok", "user": mxid, "code": code,
           "uses_left": entry["uses_remaining"]})
    print(f"[signup ok] {mxid} via {code} (left={entry['uses_remaining']})", flush=True)

    return web.json_response({
        "user_id": mxid,
        "access_token": result["access_token"],
        "device_id": result["device_id"],
        "homeserver": HS_PUBLIC,
        "space_id": SPACE_ID,
    })

async def run_http():
    app = web.Application()
    app.router.add_post("/signup/api", signup_handler)
    app.router.add_get("/health",      lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    print(f"signup HTTP server listening on :{HTTP_PORT}", flush=True)


# --- Main ---

async def main():
    print(f"approver starting. space={SPACE_ID} signup_enabled={bool(REG_TOKEN)}", flush=True)
    for p in (CODES_PATH, SIGNUP_PATH, LOG_PATH):
        p.parent.mkdir(parents=True, exist_ok=True)
    if not CODES_PATH.exists():  _save(CODES_PATH,  {})
    if not SIGNUP_PATH.exists(): _save(SIGNUP_PATH, {})
    merge_seed(CODES_PATH,  "INITIAL_CODES")
    merge_seed(SIGNUP_PATH, "INITIAL_SIGNUP_CODES")

    await run_http()
    await sync_loop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
