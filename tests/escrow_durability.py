"""Issue #60 — escrow durability: megolm inbound sessions survive self-heal wipe.

Scenario (the prod failure this pins down):
  1. alice creates an E2EE room (history_visibility=shared) and invites the
     bot. The bot joins and receives (and decrypts) an encrypted message —
     proving its crypto store holds the inbound megolm session.
  2. Simulate a device re-mint the way the self-heal path does it:
       a. approver.export_inbound_sessions(bot_store)   -> ESCROW_PATH
       b. approver._wipe_crypto_store()                 -> bot_crypto.db gone
       c. re-/login the SAME bot user under a NEW device_id, open a FRESH
          store (new pickle_key, fresh sqlite file — exactly post-wipe state)
       d. approver.import_inbound_sessions(bot_store)   -> restore from escrow
  3. The re-minted bot fetches the OLD encrypted event from /messages and
     decrypts it. Before #60 the wipe nuked the inbound session and this
     failed with SessionNotFound; with the escrow it decrypts.

This runs the ACTUAL approver export/wipe/import functions — not a hand-rolled
imitation — against a NEW device_id + fresh store, per the issue acceptance.

Run against the dev stack:
  cd dev && docker compose up -d && python3 bootstrap.py   # activates dev-token
  cd .. && python3 tests/escrow_durability.py

Tier 1: this transcript is the evidence (no user-visible surface).
"""
import asyncio, json, os, secrets, sys, time, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# --- Import approver (set the env it reads at module load). ---
os.environ.setdefault("HS", os.environ.get("DEV_HS", "http://localhost:46167"))
os.environ.setdefault("SPACE_ID", "!space:localhost")
os.environ.setdefault("SPACE_CHILD_IDS", "")
os.environ.setdefault("REG_TOKEN", "unused")
os.environ.setdefault("ADMIN_COMMAND_ROOM", "!admin:localhost")
os.environ.setdefault("CONDUWUIT_REGISTRATION_TOKEN",
                      os.environ.get("DEV_REG_TOKEN", "dev-token"))
sys.path.insert(0, str(REPO / "knock-approver"))
sys.path.insert(0, str(REPO / "tests"))

import approver  # noqa: E402  (must be after env setup)
from sas_e2e import HS, _post, make_client, register, sync_once  # noqa: E402
from mautrix.errors import SessionNotFound  # noqa: E402
from mautrix.types import (Event, EventType, MessageType,  # noqa: E402
                           TextMessageEventContent)

results = []
def log(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    results.append((name, ok))


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def fetch_event(room_id, event_id, token):
    """Back-paginate /messages until event_id is found (history_visibility=
    shared lets a fresh device see old events this way)."""
    frm = ""
    for _ in range(10):
        url = (f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
               f"/messages?dir=b&limit=50" + (f"&from={frm}" if frm else ""))
        page = _get(url, token)
        for raw in page.get("chunk", []):
            if raw.get("event_id") == event_id:
                return raw
        frm = page.get("end")
        if not frm:
            break
    return None


async def main():
    suffix = f"{int(time.time())}_{secrets.token_hex(2)}"
    # Two distinct devices for the SAME bot user, to faithfully simulate a
    # self-heal re-mint (new device_id -> new pickle_key -> fresh store).
    bot_user = f"esc_bot_{suffix}"
    bot_pw = secrets.token_urlsafe(32)
    bot_dev_A, bot_dev_B = f"BOT_A_{secrets.token_hex(2)}", f"BOT_B_{secrets.token_hex(2)}"
    bot_mxid, bot_tok_A = register(bot_user, bot_pw, bot_dev_A)
    alice_dev = f"ALICE{secrets.token_hex(2)}"
    alice_mxid, alice_tok = register(f"esc_alice_{suffix}",
                                     secrets.token_urlsafe(32), alice_dev)
    print(f"[escrow] bot={bot_mxid} (devs {bot_dev_A} -> {bot_dev_B}) "
          f"alice={alice_mxid}", flush=True)

    # --- Temp crypto store + escrow paths, wired into the module globals so
    #     the ACTUAL approver functions (_wipe_crypto_store, export/import)
    #     operate on them. ---
    tmp = Path(os.environ.get("ESCROW_TMP", "/tmp")) / f"escrow_{suffix}"
    tmp.mkdir(parents=True, exist_ok=True)
    crypto_db = tmp / "bot_crypto.db"
    escrow = tmp / "inbound_sessions.escrow.json"
    approver.CRYPTO_DB = crypto_db
    approver.ESCROW_PATH = escrow
    approver.SYNC_STATE = tmp / "sync_since.txt"
    assert not crypto_db.exists(), "fresh workdir must not have a store yet"

    # --- 1. alice makes an E2EE room and invites the bot. ---
    s, r = _post(f"{HS}/_matrix/client/v3/createRoom", {
        "name": "escrow durability",
        "preset": "private_chat",
        "invite": [bot_mxid],
        "initial_state": [
            {"type": "m.room.history_visibility", "state_key": "",
             "content": {"history_visibility": "shared"}},
            {"type": "m.room.encryption", "state_key": "",
             "content": {"algorithm": "m.megolm.v1.aes-sha2"}},
        ],
    }, token=alice_tok)
    assert s == 200, f"createRoom: {s} {r}"
    room_id = r["room_id"]
    s, r = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join",
                 {}, token=bot_tok_A)
    assert s == 200, f"bot join: {s} {r}"
    print(f"[escrow] room={room_id}", flush=True)

    # --- 2. bring both clients up with full OlmMachine + crypto store. ---
    alice, alice_cs, alice_ss, alice_db = await make_client(
        alice_mxid, alice_tok, alice_dev, tmp / "alice.db")
    await alice.crypto.share_keys()
    await sync_once(alice, alice_ss, first=True)

    botA, botA_cs, botA_ss, botA_db = await make_client(
        bot_mxid, bot_tok_A, bot_dev_A, crypto_db)
    await botA.crypto.share_keys()
    await sync_once(botA, botA_ss, first=True)
    await sync_once(alice, alice_ss)  # alice sees bot's device keys

    # --- 3. alice sends the encrypted message (auto-encrypts to the room). ---
    secret = f"history-must-survive-{secrets.token_hex(4)}"
    msg_id = await alice.send_message_event(
        room_id, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=secret))
    print(f"[escrow] pre-wipe msg id={msg_id} body={secret!r}", flush=True)
    await sync_once(botA, botA_ss, timeout=5000)

    raw_evt = fetch_event(room_id, str(msg_id), bot_tok_A)
    dec = await botA.crypto.decrypt_megolm_event(Event.deserialize(raw_evt))
    log("bot device A decrypts the message (inbound session present)",
        dec.content.body == secret)
    if dec.content.body != secret:
        print(f"        got body={dec.content.body!r}", flush=True)

    # --- 4. SIMULATE RE-MINT using the actual approver functions. ---
    # 4a. export BEFORE the wipe
    n_exported = await approver.export_inbound_sessions(botA_cs)
    log("export_inbound_sessions dumps the inbound megolm session(s)",
        n_exported >= 1 and escrow.exists(),
        f"{n_exported} session(s) -> {escrow.name}")
    await botA_db.stop()  # close bot A's store so the file isn't held open
    await botA.api.session.close()

    # 4b. the wipe itself
    assert crypto_db.exists(), "store must exist before wipe"
    approver._wipe_crypto_store()
    log("_wipe_crypto_store deletes bot_crypto.db",
        not crypto_db.exists(), "fresh store on next open")
    # sanity: confirm the session is GONE without the escrow (the prod bug)
    botA2, botA2_cs, botA2_ss, botA2_db = await make_client(
        bot_mxid, bot_tok_A, bot_dev_A, crypto_db)  # re-open the wiped file
    wiped_undecryptable = False
    try:
        await botA2.crypto.decrypt_megolm_event(Event.deserialize(raw_evt))
    except SessionNotFound:
        wiped_undecryptable = True
    log("without escrow, wiped store cannot decrypt (the bug #60 fixes)",
        wiped_undecryptable, "SessionNotFound after wipe")
    await botA2_db.stop()
    await botA2.api.session.close()
    approver._wipe_crypto_store()  # leave the file empty again for the real re-mint

    # 4c. re-mint: /login the SAME bot user under a NEW device_id
    s, r = _post(f"{HS}/_matrix/client/v3/login", {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": bot_user},
        "password": bot_pw,
        "device_id": bot_dev_B,
    })
    assert s == 200, f"re-mint /login: {s} {r}"
    bot_tok_B = r["access_token"]
    new_device = r["device_id"]
    log("re-mint logs the bot in under a NEW device_id",
        new_device == bot_dev_B and new_device != bot_dev_A,
        f"{bot_dev_A} -> {new_device}")

    # 4d. open a FRESH store (new pickle_key {mxid}:{devB}, empty sqlite file)
    botB, botB_cs, botB_ss, botB_db = await make_client(
        bot_mxid, bot_tok_B, bot_dev_B, crypto_db)
    # 4e. import the escrow into the fresh store
    n_imported = await approver.import_inbound_sessions(botB_cs)
    log("import_inbound_sessions restores the escrowed session(s)",
        n_imported == n_exported and n_imported >= 1,
        f"{n_imported}/{n_exported} restored")

    # --- 5. the re-minted bot still decrypts the OLD message. ---
    raw_old = fetch_event(room_id, str(msg_id), bot_tok_B)
    dec_old = await botB.crypto.decrypt_megolm_event(Event.deserialize(raw_old))
    log("re-minted bot (new device) decrypts the pre-wipe message",
        dec_old.content.body == secret, f"body={dec_old.content.body!r}")

    # escrow file is intentionally retained (durable across future wipes)
    log("escrow file is retained after import (durable across wipes)",
        escrow.exists())

    await botB_db.stop()
    await botB.api.session.close()
    await alice_db.stop()
    await alice.api.session.close()

    failed = [n for n, ok in results if not ok]
    print(f"\n[escrow] {len(results) - len(failed)}/{len(results)} checks passed",
          flush=True)
    sys.exit(1 if failed else 0)


asyncio.run(main())
