"""Signed-in admin-room flow for issue #20 / PR #70 (the positive path).

tests/admin_e2ee.py proves the REFUSAL side (unverified device -> encrypted
refusal). This driver completes the story on the same stack shape: it walks
the admin room as a *signed-in operator whose device is genuinely
cross-signed* (MSK/SSK/USK via the repo's own POST /signup/api/crosssign,
Paste B) and asserts the four behaviors the PR claims, over the Matrix
client-server HTTP API against a real homeserver running this branch's
approver.py:

  1. cleartext `!mint`            -> refused, no code minted
  2. cross-signed `!mint`         -> ACCEPTED, code minted (positive path)
  3. unverified 3rd-party `!mint` -> refused, no code minted
  4. rotated-master `!mint`       -> refused (fails closed) / actual behavior reported

Env (same contract as admin_e2ee.py, set by the runner):
  DEV_HS, DEV_REG_TOKEN, ADMIN_COMMAND_ROOM, ADMIN_TOKEN, ADMIN_MXID
"""
import asyncio, json, os, secrets, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "tests"))

from sas_e2e import make_client, sync_once, register

from mautrix.types import EventType, MessageType, TextMessageEventContent

HS = os.environ.get("DEV_HS", "http://landing:80").rstrip("/")
REG_TOKEN = os.environ["DEV_REG_TOKEN"]
ADMIN_ROOM = os.environ["ADMIN_COMMAND_ROOM"]
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]
ADMIN_MXID = os.environ["ADMIN_MXID"]

REFUSAL = "cross-signing-verified device"

results = []
def log(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    results.append((name, ok))


def req(method, path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(HS + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def bump_pl(user_mxid, level=100):
    url = f"/_matrix/client/v3/rooms/{urllib.parse.quote(ADMIN_ROOM)}/state/m.room.power_levels"
    s, cur = req("GET", url, ADMIN_TOKEN)
    assert s == 200, f"read power_levels: {s} {cur}"
    cur.setdefault("users", {})[user_mxid] = level
    s, r = req("PUT", url, ADMIN_TOKEN, cur)
    assert s == 200, f"write power_levels: {s} {r}"


def invite_and_join(user_mxid, user_token):
    s, r = req("POST", f"/_matrix/client/v3/rooms/{urllib.parse.quote(ADMIN_ROOM)}/invite",
               ADMIN_TOKEN, {"user_id": user_mxid})
    log(f"admin invited {user_mxid} to admin room", s == 200, f"status={s}")
    s, r = req("POST", f"/_matrix/client/v3/rooms/{urllib.parse.quote(ADMIN_ROOM)}/join",
               user_token, {})
    log(f"{user_mxid} joined admin room", s == 200, f"status={s}")


def crosssign(user_token, password=""):
    body = {"access_token": user_token}
    if password:
        body["password"] = password
    r = urllib.request.Request(f"{HS}/signup/api/crosssign",
                               data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            out = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"crosssign HTTP {e.code}: {e.read().decode()[:300]}")
    if not out.get("device_signed"):
        raise RuntimeError(f"crosssign did not sign the device: {out}")
    return out


async def send_encrypted(client, body):
    return await client.send_message_event(
        ADMIN_ROOM, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=body))


def send_cleartext(user_token, body):
    s, r = req("PUT", f"/_matrix/client/v3/rooms/{urllib.parse.quote(ADMIN_ROOM)}/send/m.room.message/"
                      f"cleartext{secrets.token_hex(3)}",
               user_token, {"msgtype": "m.text", "body": body})
    return s, r


async def await_reply(client, ss, self_mxid, want, avoid, label, deadline_s=120):
    """Poll sync until a non-self message in ADMIN_ROOM matches `want` and not
    `avoid` (or `avoid` first, reported). Returns (matched, bodies)."""
    got = {"avoid": None, "want": None}
    bodies = []

    async def on_msg(evt):
        if str(evt.room_id) != ADMIN_ROOM:
            return
        sender, body = str(evt.sender), (getattr(evt.content, "body", "") or "")
        if sender == self_mxid:
            return
        bodies.append((sender, body))
        print(f"    recv {sender}: {body[:110]!r}", flush=True)
        if avoid and avoid in body and got["avoid"] is None:
            got["avoid"] = body
        if want(body):
            got["want"] = body

    client.add_event_handler(EventType.ROOM_MESSAGE, on_msg)
    deadline = time.time() + deadline_s
    while time.time() < deadline and got["want"] is None and got["avoid"] is None:
        await sync_once(client, ss, timeout=2000)
    client.remove_event_handler(EventType.ROOM_MESSAGE, on_msg)
    return got, bodies


def minted_with_label(bodies, label):
    return any("join?code=" in b and label in b for _, b in bodies)


async def main():
    # --- operator identity: fresh user, PL 100 in the admin room ----------
    op_name = f"op70_{secrets.token_hex(3)}"
    op_device = f"OP70{secrets.token_hex(2)}"
    op_pw = secrets.token_urlsafe(32)
    op_mxid, op_token = register(op_name, op_pw, op_device)
    print(f"[flow] operator: {op_mxid} device={op_device}", flush=True)
    bump_pl(op_mxid, 100)
    invite_and_join(op_mxid, op_token)

    op_client, op_cs, op_ss, op_db = await make_client(
        op_mxid, op_token, op_device,
        db_path=f"/tmp/op70_{secrets.token_hex(4)}.db")
    await op_client.crypto.share_keys()
    for _ in range(5):
        await sync_once(op_client, op_ss, timeout=2000, first=True)
    enc = await op_ss.is_encrypted(ADMIN_ROOM)
    log("admin room reports encrypted", bool(enc))

    # --- 1. cleartext refusal (pre-cross-sign: also unverified) -----------
    label_a = f"cl-a-{secrets.token_hex(2)}"
    s, r = send_cleartext(op_token, f"!mint --uses 1 {label_a}")
    log("cleartext !mint sent over HTTP (no encryption)", s == 200, f"status={s}")
    got, bodies = await await_reply(op_client, op_ss, op_mxid,
                                    want=lambda b: REFUSAL in b, avoid=None, label=label_a)
    log("cleartext !mint refused", got["want"] is not None,
        f"body[:110]={(got['want'] or '')[:110]!r}")
    log("cleartext !mint minted no code", not minted_with_label(bodies, label_a))

    # --- 2. cross-sign the operator (Paste B, server-side bootstrap) ------
    cs_out = crosssign(op_token)
    log("operator cross-signed via /signup/api/crosssign", True,
        f"msk={cs_out['msk_public'][:16]}… device={cs_out['device_id']}")
    # --- positive path: encrypted !mint from the cross-signed operator ----
    label_b = f"pos-b-{secrets.token_hex(2)}"
    attempts = []
    for attempt in range(3):
        sent = await send_encrypted(op_client, f"!mint --uses 2 {label_b}")
        print(f"[flow] attempt {attempt+1}: encrypted !mint {label_b} ({sent})", flush=True)
        got, bodies = await await_reply(
            op_client, op_ss, op_mxid,
            want=lambda b: "join?code=" in b and label_b in b,
            avoid=REFUSAL, label=label_b, deadline_s=90)
        attempts.append((got["avoid"] is not None, got["want"] is not None))
        if got["want"] is not None or got["avoid"] is None:
            break
        # refused -> the bot may not have re-fetched our signing keys yet;
        # more sync rounds give it another chance before we call it failed.
        for _ in range(3):
            await sync_once(op_client, op_ss, timeout=2000)
    log("cross-signed encrypted !mint ACCEPTED (code minted)",
        got["want"] is not None,
        f"attempts={attempts} body[:110]={(got['want'] or '')[:110]!r}")
    log("no refusal sent to the cross-signed operator", got["avoid"] is None,
        f"refusal={(got['avoid'] or '')[:80]!r}")

    # --- 3. unverified third party (PL 100, no cross-signing) -------------
    unv_name = f"unv70_{secrets.token_hex(3)}"
    unv_device = f"UNV70{secrets.token_hex(2)}"
    unv_mxid, unv_token = register(unv_name, secrets.token_urlsafe(32), unv_device)
    bump_pl(unv_mxid, 100)
    invite_and_join(unv_mxid, unv_token)
    unv_client, unv_cs, unv_ss, unv_db = await make_client(
        unv_mxid, unv_token, unv_device,
        db_path=f"/tmp/unv70_{secrets.token_hex(4)}.db")
    await unv_client.crypto.share_keys()
    for _ in range(5):
        await sync_once(unv_client, unv_ss, timeout=2000, first=True)
    label_c = f"neg-c-{secrets.token_hex(2)}"
    await send_encrypted(unv_client, f"!mint --uses 1 {label_c}")
    got, bodies = await await_reply(unv_client, unv_ss, unv_mxid,
                                    want=lambda b: REFUSAL in b, avoid=None, label=label_c)
    log("unverified encrypted !mint refused", got["want"] is not None,
        f"body[:110]={(got['want'] or '')[:110]!r}")
    log("unverified !mint minted no code", not minted_with_label(bodies, label_c))

    # --- 4. rotated master: cross-sign AGAIN -> new MSK/SSK ---------------
    # Replacing existing cross-signing keys is UIA-gated (password) — the
    # approver's own error message names this requirement.
    label_d = f"rot-d-{secrets.token_hex(2)}"
    rot_out = crosssign(op_token, op_pw)
    log("operator re-cross-signed (fresh MSK/SSK — rotated master)",
        rot_out["msk_public"] != cs_out["msk_public"],
        f"old={cs_out['msk_public'][:12]}… new={rot_out['msk_public'][:12]}…")
    await send_encrypted(op_client, f"!mint --uses 1 {label_d}")
    got, bodies = await await_reply(op_client, op_ss, op_mxid,
                                    want=lambda b: (REFUSAL in b and op_mxid in b)
                                                   or ("join?code=" in b and label_d in b),
                                    avoid=None, label=label_d)
    refused = got["want"] is not None and REFUSAL in got["want"] and op_mxid in got["want"]
    minted = minted_with_label(bodies, label_d)
    log("rotated-master !mint fails closed (refused)", refused,
        f"body[:110]={(got['want'] or '')[:110]!r}")
    log("rotated-master !mint minted no code", not minted)

    await op_db.stop()
    await unv_db.stop()

    failed = [n for n, ok in results if not ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} pass ===", flush=True)
    if failed:
        print("FAILED: " + ", ".join(failed), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
