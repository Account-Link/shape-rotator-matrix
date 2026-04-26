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
import asyncio, base64, json, os, sys, time
from pathlib import Path
import aiohttp
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

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

# Comma-separated list of space-child room IDs that a freshly-signed-up user
# should auto-join via the restricted rule. Typically: general, announcements,
# bot-noise. IDs MUST be unsuffixed (!foo, not !foo:server.tld).
SPACE_CHILD_IDS = [r.strip() for r in os.environ.get("SPACE_CHILD_IDS", "").split(",") if r.strip()]

# Default inviter MXID to DM from the new account when someone signs up.
# Per-code override: set "inviter" on the signup_codes.json entry.
ONBOARDING_INVITER_MXID = os.environ.get("ONBOARDING_INVITER_MXID", "").strip()

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


# --- Knock approval (per-knock vetting room with a haiku captcha) ---
#
# A valid knock no longer invites straight to the space. Instead the approver
# creates a fresh 1:1 vetting room (invite-only, the knocker is the only
# invitee) and posts a wikipedia-fact haiku challenge. Once the knocker joins
# and replies with a 3-line haiku that contains the required keyword, the bot
# invites them to the space — and the existing `restricted` join rule on
# child rooms takes over from there.

VETTING_PATH      = Path(os.environ.get("VETTING_PATH",       "/data/vetting.json"))
VETTING_TIMEOUT   = int(os.environ.get("VETTING_TIMEOUT_SEC", "7200"))
VETTING_MAX_TRIES = int(os.environ.get("VETTING_MAX_TRIES",   "3"))

# Stop-words too generic to use as the haiku-keyword constraint.
_STOPWORDS = {"with", "from", "that", "this", "their", "have", "been",
              "were", "into", "over", "when", "what", "where", "which",
              "would", "could", "should", "about", "after", "before"}


async def _fetch_wiki_challenge():
    """Random wikipedia article -> (title, longest non-stopword >=4-char alpha word)."""
    url = "https://en.wikipedia.org/api/rest_v1/page/random/summary"
    headers = {"User-Agent": "shape-rotator-vetting/1.0", "Accept": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(url) as r:
            body = await r.json()
    title = body["title"]
    words = [w.strip(".,;:'\"()[]") for w in title.split()]
    candidates = [w for w in words
                  if len(w) >= 4 and w.isalpha() and w.lower() not in _STOPWORDS]
    keyword = max(candidates, key=len)
    return title, keyword


async def _send_msg(session, room_id, text):
    txn = f"m{int(time.time()*1000)}-{os.urandom(2).hex()}"
    url = f"{HS}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}"
    async with session.put(url, json={"msgtype": "m.text", "body": text}) as r:
        return r.status


async def _create_vetting_room(session, mxid):
    body = {
        "preset": "private_chat",   # creator PL 100, invitee PL 0
        "invite": [mxid],
        "is_direct": False,
        "name":  f"shape-rotator vetting · {mxid}",
        "topic": "captcha airlock — answer the challenge to be invited to the space.",
    }
    async with session.post(f"{HS}/_matrix/client/v3/createRoom", json=body) as r:
        if r.status != 200:
            return None, await r.text()
        return (await r.json())["room_id"], None


def _vet(displayname, message, keyword):
    if not displayname:
        return False, "set a displayname in element first, then re-paste the haiku"
    text = (message or "").strip()
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) != 3:
        return False, "haiku is three lines"
    if not (30 <= len(text) <= 400):
        return False, "haiku should be roughly 30–400 chars"
    if keyword.lower() not in text.lower():
        return False, f"include the word '{keyword}' somewhere"
    return True, "ok"


async def _promote(session, mxid):
    url = f"{HS}/_matrix/client/v3/rooms/{SPACE_ID}/invite"
    async with session.post(url,
                            json={"user_id": mxid, "reason": "vetted via airlock"}) as r:
        return r.status, await r.text()


async def handle_knock(session, room_id, user_id, reason):
    code = (reason or "").strip()
    codes = _load(CODES_PATH)
    entry = codes.get(code)
    if not entry or entry.get("uses_remaining", 0) <= 0:
        audit({"type": "knock_rejected", "user": user_id, "room": room_id, "reason": reason})
        print(f"[knock rejected] {user_id}", flush=True)
        return

    title, keyword = await _fetch_wiki_challenge()
    vroom, err = await _create_vetting_room(session, user_id)
    if not vroom:
        audit({"type": "vetting_room_failed", "user": user_id,
               "code": code, "err": (err or "")[:200]})
        print(f"[vetting room failed] {user_id}: {err[:200]}", flush=True)
        return

    entry["uses_remaining"] -= 1
    codes[code] = entry
    _save(CODES_PATH, codes)

    state = _load(VETTING_PATH)
    state[vroom] = {
        "mxid": user_id, "code": code, "created": time.time(),
        "title": title, "keyword": keyword,
        "tries_left": VETTING_MAX_TRIES, "promoted": False, "closed": False,
    }
    _save(VETTING_PATH, state)

    await _send_msg(session, vroom,
        f"hi {user_id} — quick captcha to keep bots out of shape rotator.\n\n"
        f"write a 3-line haiku about: {title}\n"
        f"include the word \"{keyword}\" somewhere.\n"
        f"reply in this room. {VETTING_MAX_TRIES} tries.")
    audit({"type": "vetting_room_created", "user": user_id, "code": code,
           "room": vroom, "title": title, "keyword": keyword})
    print(f"[vetting] {user_id} -> {vroom} ({title!r} / {keyword})", flush=True)


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


def iter_vetting_rooms(rooms_data, vetting_state):
    """For each open vetting room we own, yield
    (room_id, meta, join_event_for_user_or_None, list_of_user_messages)."""
    for room_id, rd in rooms_data.get("join", {}).items():
        meta = vetting_state.get(room_id)
        if not meta or meta.get("promoted") or meta.get("closed"):
            continue
        join_ev = None
        for section in ("state", "timeline"):
            for ev in rd.get(section, {}).get("events", []):
                if (ev.get("type") == "m.room.member"
                        and ev.get("state_key") == meta["mxid"]
                        and (ev.get("content") or {}).get("membership") == "join"):
                    join_ev = ev
        msgs = [ev for ev in rd.get("timeline", {}).get("events", [])
                if ev.get("type") == "m.room.message"
                and ev.get("sender") == meta["mxid"]]
        yield room_id, meta, join_ev, msgs


async def process_vetting_room(session, room_id, meta, join_ev, msgs):
    """Process new messages in one vetting room. Returns updated meta or None."""
    # Persist displayname the first time we see the user's join event — Matrix
    # /sync returns the join event in one batch and the user's later messages
    # in subsequent batches, so we can't require both in the same cycle.
    if join_ev:
        meta["displayname"] = (join_ev.get("content") or {}).get("displayname", "")
    if not msgs:
        return meta if join_ev else None
    displayname = meta.get("displayname", "")
    keyword = meta["keyword"]
    for msg in msgs:
        text = (msg.get("content") or {}).get("body", "")
        ok, why = _vet(displayname, text, keyword)
        if ok:
            st, body = await _promote(session, meta["mxid"])
            if st == 200:
                meta["promoted"] = True
                meta["promoted_at"] = time.time()
                meta["displayname"] = displayname
                await _send_msg(session, room_id,
                    "nice — invited you to shape rotator. you can leave this room.")
                audit({"type": "promoted", "user": meta["mxid"],
                       "displayname": displayname, "room": room_id})
                print(f"[promoted] {meta['mxid']} ({displayname})", flush=True)
            else:
                audit({"type": "promote_failed", "user": meta["mxid"],
                       "status": st, "body": body[:200]})
                print(f"[promote failed] {meta['mxid']} status={st}", flush=True)
            return meta
        meta["tries_left"] -= 1
        if meta["tries_left"] <= 0:
            await _send_msg(session, room_id,
                "out of tries. closing this room — get a fresh code and try again.")
            async with session.post(
                f"{HS}/_matrix/client/v3/rooms/{room_id}/leave",
                json={"reason": "vetting failed"}) as r:
                pass
            meta["closed"] = True
            meta["closed_reason"] = "tries_exhausted"
            audit({"type": "vetting_failed", "user": meta["mxid"], "room": room_id})
            return meta
        await _send_msg(session, room_id,
            f"not yet — {why}. {meta['tries_left']} tries left.")
    return meta


async def cleanup_stale_vetting(session, vetting_state):
    """Leave vetting rooms older than VETTING_TIMEOUT. Returns True if state changed."""
    now = time.time()
    dirty = False
    for vroom, meta in list(vetting_state.items()):
        if meta.get("promoted") or meta.get("closed"):
            continue
        if now - meta.get("created", 0) > VETTING_TIMEOUT:
            async with session.post(
                f"{HS}/_matrix/client/v3/rooms/{vroom}/leave",
                json={"reason": "vetting timeout"}) as r:
                pass
            meta["closed"] = True
            meta["closed_reason"] = "timeout"
            audit({"type": "vetting_timeout", "user": meta["mxid"], "room": vroom})
            dirty = True
    return dirty


# --- Admin commands (!mint / !codes / !revoke) ---
#
# Admin chat surface so adding/listing/revoking codes doesn't require ssh.
# The bot listens in ADMIN_COMMAND_ROOM (defaults to SPACE_ID — the space
# room itself is cleartext, so raw-HTTP /sync can read commands there).
# Tracked in issue #7; this is the v1 cut.

ADMIN_COMMAND_ROOM = os.environ.get("ADMIN_COMMAND_ROOM", SPACE_ID)
ADMIN_PL_THRESHOLD = int(os.environ.get("ADMIN_PL_THRESHOLD", "50"))
# Comma-separated mxids allowed regardless of PL. Useful when you want to
# delegate admin to someone whose PL hasn't been bumped yet.
ADMIN_ALLOWLIST = set(
    m.strip() for m in os.environ.get("ADMIN_ALLOWLIST", "").split(",") if m.strip()
)

# Filled at startup by /whoami so we can skip our own messages in the
# command room (we'd otherwise process replies we just sent).
OUR_MXID = ""


async def _whoami(session):
    async with session.get(f"{HS}/_matrix/client/v3/account/whoami") as r:
        if r.status != 200:
            raise RuntimeError(f"whoami: {r.status} {(await r.text())[:200]}")
        return (await r.json())["user_id"]


async def _get_user_pl(session, room_id, mxid):
    url = f"{HS}/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels"
    async with session.get(url) as r:
        if r.status != 200:
            return 0
        pl = await r.json()
    users = pl.get("users") or {}
    if mxid in users:
        return int(users[mxid])
    return int(pl.get("users_default", 0))


async def _is_admin(session, room_id, sender):
    if sender in ADMIN_ALLOWLIST:
        return True
    return (await _get_user_pl(session, room_id, sender)) >= ADMIN_PL_THRESHOLD


def iter_admin_commands(rooms_data, admin_room_id):
    rd = rooms_data.get("join", {}).get(admin_room_id)
    if not rd:
        return
    for ev in rd.get("timeline", {}).get("events", []):
        if ev.get("type") != "m.room.message":
            continue
        body = ((ev.get("content") or {}).get("body", "") or "").strip()
        if not body.startswith("!"):
            continue
        yield ev.get("event_id", ""), ev.get("sender", ""), body


def _new_code():
    return secrets.token_urlsafe(6).rstrip("=").replace("_", "").replace("-", "")[:9] or secrets.token_hex(4)


async def cmd_mint(session, room_id, sender, args):
    """!mint [knock|signup] [label]  — generate a new single-use code."""
    parts = args.split(maxsplit=1)
    kind = "knock"
    if parts and parts[0] in ("knock", "signup"):
        kind = parts.pop(0)
    label = parts[0] if parts else f"minted by {sender}"

    code = _new_code()
    path = SIGNUP_PATH if kind == "signup" else CODES_PATH
    codes = _load(path)
    if code in codes:
        code = _new_code() + secrets.token_hex(2)
    codes[code] = {"uses_remaining": 1, "label": label}
    _save(path, codes)
    audit({"type": "admin_mint", "kind": kind, "code": code,
           "minted_by": sender, "label": label})

    if kind == "signup":
        url = f"{HS_PUBLIC}/signup?code={code}"
    else:
        url = f"{HS_PUBLIC}/join?code={code}"
    return f"minted {kind} code → {url}\n(label: {label})"


async def cmd_codes(session, room_id, sender, args):
    """!codes — list current valid codes."""
    out = []
    for label_name, p in (("knock", CODES_PATH), ("signup", SIGNUP_PATH)):
        codes = _load(p)
        live = {c: m for c, m in codes.items() if m.get("uses_remaining", 0) > 0}
        if not live:
            continue
        out.append(f"{label_name}:")
        for c, m in sorted(live.items()):
            out.append(f"  {c} (uses={m.get('uses_remaining',0)}, label={m.get('label','')!r})")
    return "\n".join(out) if out else "no live codes."


async def cmd_revoke(session, room_id, sender, args):
    """!revoke <code> — zero out a code's uses_remaining."""
    code = args.strip()
    if not code:
        return "usage: !revoke <code>"
    for p in (CODES_PATH, SIGNUP_PATH):
        codes = _load(p)
        if code in codes:
            codes[code]["uses_remaining"] = 0
            _save(p, codes)
            audit({"type": "admin_revoke", "code": code, "revoked_by": sender,
                   "in": p.name})
            return f"revoked {code} (in {p.name})"
    return f"unknown code: {code}"


COMMANDS = {
    "!mint": cmd_mint,
    "!codes": cmd_codes,
    "!revoke": cmd_revoke,
    "!help": None,  # filled below
}

async def cmd_help(session, room_id, sender, args):
    return ("commands: " +
            ", ".join(sorted(c for c in COMMANDS if c != "!help")) +
            ", !help")
COMMANDS["!help"] = cmd_help


async def process_admin_command(session, room_id, event_id, sender, body):
    if not OUR_MXID:
        # /whoami failed at startup; refuse to process anything to avoid
        # ever responding to our own replies (which would loop).
        return
    if sender == OUR_MXID:
        return
    parts = body.split(maxsplit=1)
    cmd = parts[0]
    args = parts[1] if len(parts) > 1 else ""
    handler = COMMANDS.get(cmd)
    if not handler:
        return
    if not await _is_admin(session, room_id, sender):
        await _send_msg(session, room_id,
            f"{sender}: refused — need PL >= {ADMIN_PL_THRESHOLD} or be on the allowlist")
        audit({"type": "admin_refused", "cmd": cmd, "sender": sender})
        return
    try:
        result = await handler(session, room_id, sender, args)
    except Exception as e:
        result = f"!{cmd[1:]} failed: {type(e).__name__}: {e}"
        print(f"[admin] {cmd} crashed: {e}", flush=True)
    await _send_msg(session, room_id, result)


async def sync_loop():
    global OUR_MXID
    since = SYNC_STATE.read_text().strip() if SYNC_STATE.exists() else None
    timeout = aiohttp.ClientTimeout(total=None, sock_read=45)
    async with aiohttp.ClientSession(headers=AUTH, timeout=timeout) as s:
        try:
            OUR_MXID = await _whoami(s)
            print(f"[startup] running as {OUR_MXID}; admin room={ADMIN_COMMAND_ROOM}; "
                  f"allowlist={sorted(ADMIN_ALLOWLIST) or '(empty)'}", flush=True)
        except Exception as e:
            print(f"[startup] whoami failed: {e}", flush=True)
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

            vetting_state = _load(VETTING_PATH)
            v_dirty = False
            for vroom, meta, join_ev, msgs in iter_vetting_rooms(
                    data.get("rooms", {}), vetting_state):
                updated = await process_vetting_room(s, vroom, meta, join_ev, msgs)
                if updated is not None:
                    vetting_state[vroom] = updated
                    v_dirty = True
            if await cleanup_stale_vetting(s, vetting_state):
                v_dirty = True
            if v_dirty:
                _save(VETTING_PATH, vetting_state)

            for ev_id, sender, body in iter_admin_commands(
                    data.get("rooms", {}), ADMIN_COMMAND_ROOM):
                await process_admin_command(s, ADMIN_COMMAND_ROOM, ev_id, sender, body)


# --- Signup auth proxy ---

def valid_username(u: str) -> bool:
    return (u.isascii() and 1 <= len(u) <= 32
            and all(c.isalnum() or c in "-_.=" for c in u))

async def _admin_invite(mxid, room_id, reason="signup auto-invite"):
    """Invite `mxid` to `room_id` using the admin (MATRIX_TOKEN) account."""
    async with aiohttp.ClientSession(
        headers={**AUTH, "Content-Type": "application/json"}
    ) as s:
        url = f"{HS}/_matrix/client/v3/rooms/{room_id}/invite"
        async with s.post(url, json={"user_id": mxid, "reason": reason}) as r:
            return r.status, await r.text()

async def _as_user(access_token, method, path, body=None):
    """Make a request as the freshly-registered user."""
    headers = {"Authorization": f"Bearer {access_token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    url = f"{HS}{path}"
    async with aiohttp.ClientSession(headers=headers) as s:
        kwargs = {"json": body} if body is not None else {}
        async with s.request(method, url, **kwargs) as r:
            return r.status, await r.text()

async def signup_handler(request):
    if not REG_TOKEN:
        return web.json_response({"error": "signup_disabled"}, status=503)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)

    code        = (data.get("code") or "").strip()
    username    = (data.get("username") or "").strip().lower()
    password    = data.get("password") or ""
    displayname = (data.get("display_name") or "").strip()
    intro_raw   = (data.get("intro") or "").strip()

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

    # --- Step 1+2: register ---
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{HS}/_matrix/client/v3/register", json={}) as r:
            if r.status == 401:
                session = (await r.json()).get("session")
            else:
                return web.json_response({"error": "register_init_unexpected",
                                          "status": r.status}, status=502)

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

    entry["uses_remaining"] -= 1
    codes[code] = entry
    _save(SIGNUP_PATH, codes)

    mxid  = result["user_id"]
    token = result["access_token"]
    steps_done = {"register": True}

    # --- Step 3: admin invites the new user to the space ---
    st, _body = await _admin_invite(mxid, SPACE_ID)
    steps_done["space_invited"] = (st == 200)
    if st != 200:
        print(f"[signup] admin invite of {mxid} -> {st}: {_body[:200]}", flush=True)

    # --- Step 4: new user sets display name (if requested) ---
    if displayname:
        import urllib.parse as _up
        st, _body = await _as_user(
            token, "PUT",
            f"/_matrix/client/v3/profile/{_up.quote(mxid)}/displayname",
            {"displayname": displayname[:100]},
        )
        steps_done["displayname_set"] = (st == 200)

    # --- Step 5: new user accepts space invite ---
    st, _body = await _as_user(
        token, "POST", f"/_matrix/client/v3/rooms/{SPACE_ID}/join", {}
    )
    steps_done["space_joined"] = (st == 200)
    if st != 200:
        print(f"[signup] space join by {mxid} -> {st}: {_body[:200]}", flush=True)

    # --- Step 6: new user joins each child room (restricted rule permits) ---
    joined_children = []
    for child in SPACE_CHILD_IDS:
        st, _body = await _as_user(
            token, "POST", f"/_matrix/client/v3/rooms/{child}/join", {}
        )
        if st == 200:
            joined_children.append(child)
        else:
            print(f"[signup] child {child} join by {mxid} -> {st}: {_body[:200]}", flush=True)
    steps_done["children_joined"] = joined_children

    # --- Step 7: create an E2EE DM with the inviter from the new user ---
    # We create the DM encrypted from the start (m.room.encryption in
    # initial_state). We do NOT send a greeting here via raw HTTP —
    # the server would reject plaintext m.room.message in an encrypted
    # room. The intro is left to the bot's matrix-nio startup, which
    # can send encrypted messages properly. We just return dm_room and
    # intro_text so the bot knows where to post and what to say.
    inviter = (entry.get("inviter") or ONBOARDING_INVITER_MXID or "").strip()
    dm_room = None
    intro_text = intro_raw or (
        f"hi — I'm {displayname or mxid}, just signed up on "
        f"{HS_PUBLIC} via a code you issued. Let me know what you need."
    )
    if inviter:
        st, dm_body = await _as_user(
            token, "POST", "/_matrix/client/v3/createRoom",
            {
                "is_direct": True,
                "invite":    [inviter],
                "preset":    "trusted_private_chat",
                "name":      f"{displayname or username} ↔ {inviter}",
                "initial_state": [{
                    "type": "m.room.encryption",
                    "state_key": "",
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                }],
            },
        )
        if st == 200:
            dm_room = json.loads(dm_body).get("room_id")
            steps_done["inviter_dm"] = True
        else:
            print(f"[signup] createRoom (DM to {inviter}) -> {st}: {dm_body[:200]}", flush=True)
            steps_done["inviter_dm"] = False

    audit({"type": "signup_ok", "user": mxid, "code": code,
           "uses_left": entry["uses_remaining"], "steps": steps_done})
    print(f"[signup ok] {mxid} via {code} "
          f"(left={entry['uses_remaining']}, steps={steps_done})", flush=True)

    return web.json_response({
        "user_id":     mxid,
        "access_token": token,
        "device_id":   result["device_id"],
        "homeserver":  HS_PUBLIC,
        "space_id":    SPACE_ID,
        "steps":       steps_done,
        "dm_room":     dm_room,
        "intro_text":  intro_text,   # for the bot to post via nio on startup
    })

# --- Cross-signing bootstrap ---
#
# Generate MSK / SSK / USK for the user, sign SSK and USK with MSK, sign the
# caller's current device with SSK, and upload everything via UIA. After this,
# Element stops showing "encrypted by a device not verified by its owner".
#
# Matrix canonical JSON: keys sorted, no whitespace, no non-ASCII escaping.

def _b64(data: bytes) -> str:
    return base64.b64encode(data).rstrip(b"=").decode()

def _canon(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")

def _raw_pub(privkey: ed25519.Ed25519PrivateKey) -> bytes:
    return privkey.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

def _raw_priv(privkey: ed25519.Ed25519PrivateKey) -> bytes:
    return privkey.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )

def _sign_object(obj: dict, signer: ed25519.Ed25519PrivateKey, user_id: str, key_id: str) -> dict:
    """Sign `obj` per Matrix spec: canonical JSON of obj minus signatures/unsigned,
    attach under signatures[user_id][ed25519:key_id]."""
    to_sign = {k: v for k, v in obj.items() if k not in ("signatures", "unsigned")}
    sig = _b64(signer.sign(_canon(to_sign)))
    sigs = dict(obj.get("signatures", {}))
    user_sigs = dict(sigs.get(user_id, {}))
    user_sigs[f"ed25519:{key_id}"] = sig
    sigs[user_id] = user_sigs
    obj["signatures"] = sigs
    return obj

async def _crosssign(access_token: str, password: str = ""):
    """Bootstrap cross-signing for the user identified by access_token.

    Password is optional — only needed if the homeserver insists on UIA
    m.login.password for /keys/device_signing/upload. Continuwuity (our
    target) currently accepts the upload directly.
    """
    headers = {"Authorization": f"Bearer {access_token}",
               "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as s:
        # Identify the user
        async with s.get(f"{HS}/_matrix/client/v3/account/whoami", headers=headers) as r:
            if r.status != 200:
                raise RuntimeError(f"whoami failed: {r.status}")
            me = await r.json()
        user_id  = me["user_id"]
        device_id = me["device_id"]

        # Generate three ed25519 keypairs
        msk = ed25519.Ed25519PrivateKey.generate()
        ssk = ed25519.Ed25519PrivateKey.generate()
        usk = ed25519.Ed25519PrivateKey.generate()
        msk_pub = _b64(_raw_pub(msk))
        ssk_pub = _b64(_raw_pub(ssk))
        usk_pub = _b64(_raw_pub(usk))

        # Build three signed key objects.
        # MSK self-signs (master signs itself). SSK and USK are signed by MSK.
        master = {
            "user_id": user_id, "usage": ["master"],
            "keys": {f"ed25519:{msk_pub}": msk_pub},
        }
        master = _sign_object(master, msk, user_id, msk_pub)

        self_signing = {
            "user_id": user_id, "usage": ["self_signing"],
            "keys": {f"ed25519:{ssk_pub}": ssk_pub},
        }
        self_signing = _sign_object(self_signing, msk, user_id, msk_pub)

        user_signing = {
            "user_id": user_id, "usage": ["user_signing"],
            "keys": {f"ed25519:{usk_pub}": usk_pub},
        }
        user_signing = _sign_object(user_signing, msk, user_id, msk_pub)

        # Upload the three signing keys. Matrix spec requires UIA here, but
        # continuwuity sometimes skips it — try direct first, fall back to UIA.
        upload_body = {
            "master_key":       master,
            "self_signing_key": self_signing,
            "user_signing_key": user_signing,
        }
        async with s.post(f"{HS}/_matrix/client/v3/keys/device_signing/upload",
                          json=upload_body, headers=headers) as r:
            if r.status == 401:
                if not password:
                    raise RuntimeError("homeserver requires UIA; pass `password` to /crosssign")
                uia = await r.json()
                session = uia.get("session")
                if not session:
                    raise RuntimeError(f"no UIA session: {uia}")
                upload_body["auth"] = {
                    "type": "m.login.password",
                    "identifier": {"type": "m.id.user", "user": user_id},
                    "password": password,
                    "session": session,
                }
                async with s.post(f"{HS}/_matrix/client/v3/keys/device_signing/upload",
                                  json=upload_body, headers=headers) as r2:
                    if r2.status != 200:
                        raise RuntimeError(f"device_signing/upload (UIA retry) failed "
                                           f"{r2.status}: {(await r2.text())[:300]}")
            elif r.status != 200:
                raise RuntimeError(f"device_signing/upload failed {r.status}: "
                                   f"{(await r.text())[:300]}")

        # Try to sign the current device with SSK. If device keys aren't uploaded
        # yet (bot hasn't synced), retry briefly — else succeed partial and let
        # the caller try again once the bot is live.
        device_obj = None
        for attempt in range(4):
            async with s.post(f"{HS}/_matrix/client/v3/keys/query",
                              json={"device_keys": {user_id: [device_id]}},
                              headers=headers) as r:
                if r.status == 200:
                    q = await r.json()
                    device_obj = (q.get("device_keys", {})
                                   .get(user_id, {})
                                   .get(device_id))
                    if device_obj:
                        break
            await asyncio.sleep(1.5)

        device_signed = False
        if device_obj:
            signed_device = _sign_object(device_obj, ssk, user_id, ssk_pub)
            async with s.post(f"{HS}/_matrix/client/v3/keys/signatures/upload",
                              json={user_id: {device_id: signed_device}},
                              headers=headers) as r:
                body = await r.json()
                if r.status == 200 and not body.get("failures"):
                    device_signed = True
                else:
                    print(f"[crosssign] signatures/upload: {r.status} {body}", flush=True)

    return {
        "device_signed": device_signed,
        "user_id": user_id,
        "device_id": device_id,
        "msk_public": msk_pub,
        "ssk_public": ssk_pub,
        "usk_public": usk_pub,
        "private_keys": {
            # Client persists these if it wants to sign future devices or
            # publish its own USK signatures for other users.
            "master":       _b64(_raw_priv(msk)),
            "self_signing": _b64(_raw_priv(ssk)),
            "user_signing": _b64(_raw_priv(usk)),
        },
    }

async def crosssign_handler(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    access_token = (data.get("access_token") or "").strip()
    password     = data.get("password") or ""
    if not access_token:
        return web.json_response({"error": "missing_fields",
                                  "hint": "need access_token; password optional"}, status=400)
    try:
        result = await _crosssign(access_token, password)
    except Exception as e:
        audit({"type": "crosssign_failed", "error": str(e)})
        print(f"[crosssign] {e}", flush=True)
        return web.json_response({"error": "crosssign_failed",
                                  "detail": str(e)[:500]}, status=400)
    audit({"type": "crosssign_ok", "user": result["user_id"],
           "msk": result["msk_public"]})
    print(f"[crosssign ok] {result['user_id']} msk={result['msk_public'][:20]}...", flush=True)
    return web.json_response(result)


async def run_http():
    app = web.Application()
    app.router.add_post("/signup/api",           signup_handler)
    app.router.add_post("/signup/api/crosssign", crosssign_handler)
    app.router.add_get("/health",                lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    print(f"signup HTTP server listening on :{HTTP_PORT}", flush=True)


# --- Main ---

async def main():
    print(f"approver starting. space={SPACE_ID} signup_enabled={bool(REG_TOKEN)}", flush=True)
    for p in (CODES_PATH, SIGNUP_PATH, LOG_PATH, VETTING_PATH):
        p.parent.mkdir(parents=True, exist_ok=True)
    if not CODES_PATH.exists():   _save(CODES_PATH,   {})
    if not SIGNUP_PATH.exists():  _save(SIGNUP_PATH,  {})
    if not VETTING_PATH.exists(): _save(VETTING_PATH, {})
    merge_seed(CODES_PATH,  "INITIAL_CODES")
    merge_seed(SIGNUP_PATH, "INITIAL_SIGNUP_CODES")

    await run_http()
    await sync_loop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
