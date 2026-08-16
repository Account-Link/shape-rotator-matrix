"""Evidence driver for PR #68 / issue #7 — the four NEW admin moderation commands.

`!kick` / `!ban` / `!unban` / `!stats` do not exist in tests/admin_e2ee.py (that
gate covers `!mint` only). This driver exercises them end-to-end against the
live dev-stack homeserver exactly the way a human admin would, over the same
E2EE path the standing gate uses:

  * a real mautrix-python client with OlmMachine + PgCryptoStore per user,
  * commands sent ENCRYPTED into the encrypted admin command room,
  * the branch's own knock-approver/approver.py (bind-mounted into the stack,
    running in the knock-approver container) decrypts, PL-gates, executes the
    membership calls against the real homeserver, and replies encrypted,
  * this driver decrypts the bot's replies and asserts them, AND verifies the
    victim's membership state independently over the client-server HTTP API.

Run inside the test-runner of a standing e2e stack (see flow.md for the exact
compose dance). Env (same names run_in_runner.sh exports for admin_e2ee.py):
  DEV_HS, DEV_REG_TOKEN, ADMIN_COMMAND_ROOM, ADMIN_TOKEN, ADMIN_MXID, SPACE_ID
"""
import asyncio, json, os, secrets, sys, time
import urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "tests"))

from sas_e2e import make_client, sync_once, register  # noqa: E402

from mautrix.types import (EventType, MessageType, TextMessageEventContent)  # noqa: E402

HS = os.environ.get("DEV_HS", "http://landing:80").rstrip("/")
REG_TOKEN = os.environ["DEV_REG_TOKEN"]
ADMIN_ROOM = os.environ["ADMIN_COMMAND_ROOM"]
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]
BOT_MXID = os.environ["ADMIN_MXID"]          # in the test stack the bootstrap admin IS the bot
SPACE_ID = os.environ["SPACE_ID"]

results = []
class StepFailed(Exception):
    """Raised by log() on the first FAIL so cleanup always runs."""

def summarize():
    failed = [n for n, ok in results if not ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} pass ===", flush=True)
    if failed:
        print("FAILED: " + ", ".join(failed), file=sys.stderr)

def log(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    results.append((name, ok))
    if not ok:
        print("[driver] FAILING — remaining steps skipped", flush=True)
        raise StepFailed(name)

def api(method, path, token, body=None):
    """Raw client-server HTTP call; returns (status, parsed_json)."""
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(
        f"{HS}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

def room_path(room):
    return "/_matrix/client/v3/rooms/" + urllib.parse.quote(room, safe="")

def membership_of(user, room=SPACE_ID):
    s, r = api("GET", f"{room_path(room)}/state/m.room.member/{urllib.parse.quote(user, safe='')}", ADMIN_TOKEN)
    return r.get("membership")

async def invite_and_join(user_token, user_mxid, room):
    """Invite via the PL-100 admin token, then join with the user's own token."""
    s, _ = api("POST", f"{room_path(room)}/invite", ADMIN_TOKEN, {"user_id": user_mxid})
    assert s == 200, f"invite {user_mxid} -> {room}: HTTP {s}"
    s, _ = api("POST", f"{room_path(room)}/join", user_token)
    assert s == 200, f"join {user_mxid} -> {room}: HTTP {s}"

class CmdClient:
    """One E2EE-capable admin-command sender (mautrix client + reply capture)."""
    def __init__(self, client, crypto_store, state_store, db, mxid):
        self.client, self.cs, self.ss, self.db, self.mxid = client, crypto_store, state_store, db, mxid
        self.inbox = []           # (sender, body) of every decrypted non-own message
        client.add_event_handler(EventType.ROOM_MESSAGE, self._on_msg)

    async def _on_msg(self, evt):
        body = (getattr(evt.content, "body", "") or "")
        if str(evt.room_id) == ADMIN_ROOM and str(evt.sender) != self.mxid:
            self.inbox.append((str(evt.sender), body))
            print(f"[driver] {self.mxid} received from {evt.sender}: {body[:110]!r}", flush=True)

    async def send_cmd(self, cmd):
        eid = await self.client.send_message_event(
            ADMIN_ROOM, EventType.ROOM_MESSAGE,
            TextMessageEventContent(msgtype=MessageType.TEXT, body=cmd))
        print(f"[driver] {self.mxid} sent (encrypted) {cmd!r} as {eid}", flush=True)
        return eid

    async def wait_reply(self, needle, deadline_s=90):
        """Sync until the bot's decrypted reply contains `needle`; return it."""
        deadline = time.time() + deadline_s
        while time.time() < deadline:
            await sync_once(self.client, self.ss, timeout=2000)
            for sender, body in self.inbox:
                if sender == BOT_MXID and needle in body:
                    return body
        return None


async def spawn_e2ee_user(prefix):
    username = f"{prefix}_{int(time.time())}_{secrets.token_hex(2)}"
    device = f"DRV{secrets.token_hex(2)}"
    user_mxid, user_token = register(username, secrets.token_urlsafe(32), device)
    print(f"[driver] e2ee user: {user_mxid} device={device}", flush=True)
    client, cs, ss, db = await make_client(
        user_mxid, user_token, device, db_path=f"/tmp/drv_{secrets.token_hex(4)}.db")
    await client.crypto.share_keys()
    cc = CmdClient(client, cs, ss, db, user_mxid)
    E2EE_USERS.append(cc)
    return cc, user_mxid, user_token


E2EE_USERS = []   # every spawned CmdClient, for guaranteed db teardown

async def main():
    try:
        await run_steps()
    except StepFailed:
        pass
    finally:
        for u in E2EE_USERS:
            try:
                await u.db.stop()
            except Exception:
                pass
    summarize()
    if any(not ok for _, ok in results):
        sys.exit(1)

async def run_steps():
    # --- user A: PL-50 admin in the command room (same setup as admin_e2ee.py)
    A, a_mxid, _ = await spawn_e2ee_user("mod_admin")
    s, pl = api("GET", f"{room_path(ADMIN_ROOM)}/state/m.room.power_levels", ADMIN_TOKEN)
    assert s == 200, f"read power_levels: HTTP {s}"
    pl.setdefault("users", {})[a_mxid] = 50
    s, _ = api("PUT", f"{room_path(ADMIN_ROOM)}/state/m.room.power_levels", ADMIN_TOKEN, pl)
    log("A bumped to PL 50 in admin command room", s == 200, f"status={s}")
    # invite+join via raw HTTP (A.client token lives in the HTTPAPI object)
    a_token = A.client.api.token
    await invite_and_join(a_token, a_mxid, ADMIN_ROOM)
    # --- user N: joins the command room but keeps PL 0 (the PL-gate negative)
    N, n_mxid, n_token = await spawn_e2ee_user("mod_pleb")
    await invite_and_join(n_token, n_mxid, ADMIN_ROOM)
    # --- user V: plain victim, no client needed (raw HTTP only)
    v_name = f"mod_victim_{int(time.time())}_{secrets.token_hex(2)}"
    v_mxid, v_token = register(v_name, secrets.token_urlsafe(32), f"DTV{secrets.token_hex(2)}")
    print(f"[driver] victim user: {v_mxid}", flush=True)

    for _ in range(6):   # warmup syncs: learn room state, exchange device keys
        await sync_once(A.client, A.ss, timeout=2000, first=True)
    for _ in range(6):
        await sync_once(N.client, N.ss, timeout=2000, first=True)
    log("admin command room is E2EE for A", await A.ss.is_encrypted(ADMIN_ROOM))

    # 0. A (PL 50) asks !stats first. This is not just a feature check: N's
    # client must have DECRYPTED real bot traffic in this room before it
    # sends, or N's megolm session gets keyed to a device set that omits the
    # bot and the bot can never decrypt N's command ("no session with given
    # ID" — seen live on the first attempt). Seeing the bot speak first is
    # also exactly what a real admin room looks like.
    await A.send_cmd("!stats")
    body = await A.wait_reply("last 24h:")
    ok = body is not None and "top captcha keywords:" in body and "pending=0" in body
    log("!stats (A) returns report (knocks/promoted/rejected/pending + keywords)", ok, f"reply={body!r}")
    # N must decrypt the bot's reply too — proves N↔bot device discovery BOTH
    # ways before N sends anything the gate needs to read.
    deadline = time.time() + 60
    while time.time() < deadline and not any(s == BOT_MXID for s, _ in N.inbox):
        await sync_once(N.client, N.ss, timeout=2000)
    log("N decrypted live bot traffic in the room (pre-send device discovery)",
        any(s == BOT_MXID for s, _ in N.inbox),
        f"inbox={[(s, b[:40]) for s, b in N.inbox]!r}")

    # 1. PL gate: N (PL 0) is refused.
    await N.send_cmd("!stats")
    body = await N.wait_reply("refused — need PL >= 50")
    log("PL gate: non-admin refused", body is not None,
        f"reply={body!r}; inbox={[(s, b[:40]) for s, b in N.inbox]!r}")

    # 2. usage guard: !kick with no target.
    await A.send_cmd("!kick")
    body = await A.wait_reply("usage: !kick")
    log("usage guard: bare !kick answered with usage", body is not None, f"reply={body!r}")

    # 3. self-guard: the bot refuses to kick itself.
    await A.send_cmd(f"!kick {BOT_MXID}")
    body = await A.wait_reply("refused: I will not kick myself")
    log("self-guard: bot refuses !kick of itself", body is not None, f"reply={body!r}")

    # 4. (moved up: A's !stats ran as step 0 above)

    # 5. !kick — invite V into the space, V joins, admin kicks them out.
    await invite_and_join(v_token, v_mxid, SPACE_ID)
    log("victim joined the space (pre-state)", membership_of(v_mxid) == "join",
        f"membership={membership_of(v_mxid)}")
    await A.send_cmd(f"!kick {v_mxid} evidence-driver kick test")
    body = await A.wait_reply(f"kicked {v_mxid} from the space")
    log("!kick removes a member (bot reply)", body is not None, f"reply={body!r}")
    m = membership_of(v_mxid)
    log("!kick verified over client-server API", m == "leave", f"membership={m}")

    # 6. !ban — ban the (now-kicked) user.
    await A.send_cmd(f"!ban {v_mxid} evidence-driver ban test")
    body = await A.wait_reply(f"banned {v_mxid} from the space")
    log("!ban bans the user (bot reply)", body is not None, f"reply={body!r}")
    m = membership_of(v_mxid)
    log("!ban verified over client-server API", m == "ban", f"membership={m}")

    # 7. !unban — lift the ban.
    await A.send_cmd(f"!unban {v_mxid} evidence-driver unban test")
    body = await A.wait_reply(f"unbanned {v_mxid} from the space")
    log("!unban lifts the ban (bot reply)", body is not None, f"reply={body!r}")
    m = membership_of(v_mxid)
    log("!unban verified over client-server API", m == "leave", f"membership={m}")

    # 8. !stats still parses after the moderation traffic.
    await A.send_cmd("!stats")
    body = await A.wait_reply("last 24h:")
    log("!stats still well-formed after moderation traffic", body is not None, f"reply={body!r}")

if __name__ == "__main__":
    asyncio.run(main())
