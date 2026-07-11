"""Repro for the "new members see history but can't decrypt it" prod symptom.

Scenario (mirrors prod onboarding):
  1. alice creates an E2EE room with history_visibility=shared and sends a
     message while she's the only member.
  2. bob is invited and joins AFTER that message.
  3. bob receives the old event from the server (history visibility works)
     but decryption fails with SessionNotFound — the prod symptom
     ("no session with given id found").
  4. bob's m.room_key_request to alice is refused (mautrix default policy
     rejects cross-user requests), so the failure is permanent.
  5. Control: a message sent after bob joined decrypts fine.
  6. Fix preview: alice exports the megolm session, bob imports it, and the
     old message decrypts — what a key-escrow bot would do on vetted joins.

Run against the dev stack:
  cd dev && docker compose up -d && python3 bootstrap.py
  cd .. && python3 tests/history_e2ee_repro.py
"""
import asyncio, os, secrets, sys, time, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

from sas_e2e import HS, _post, make_client, register, sync_once

from mautrix.errors import SessionNotFound
from mautrix.types import (Event, EventType, MessageType, SessionID,
                           TextMessageEventContent, UserID)
from mautrix.crypto.sessions import InboundGroupSession

results = []
def log(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    results.append((name, ok))


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    import json
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def fetch_event(room_id, event_id, token):
    """Back-paginate /messages until event_id is found (new joiners get old
    events this way because history_visibility=shared)."""
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
    alice_dev, bob_dev = f"ALICE{secrets.token_hex(2)}", f"BOB{secrets.token_hex(2)}"
    alice_mxid, alice_token = register(f"hist_alice_{suffix}", secrets.token_urlsafe(32), alice_dev)
    bob_mxid, bob_token = register(f"hist_bob_{suffix}", secrets.token_urlsafe(32), bob_dev)
    print(f"[repro] alice={alice_mxid} bob={bob_mxid}", flush=True)

    s, r = _post(f"{HS}/_matrix/client/v3/createRoom", {
        "name": "history repro",
        "preset": "private_chat",
        "initial_state": [
            {"type": "m.room.history_visibility", "state_key": "",
             "content": {"history_visibility": "shared"}},
            {"type": "m.room.encryption", "state_key": "",
             "content": {"algorithm": "m.megolm.v1.aes-sha2"}},
        ],
    }, token=alice_token)
    assert s == 200, f"createRoom: {s} {r}"
    room_id = r["room_id"]
    print(f"[repro] room={room_id}", flush=True)

    tmp = Path(os.environ.get("REPRO_TMP", "/tmp")) / f"hist_repro_{suffix}"
    tmp.mkdir(parents=True, exist_ok=True)
    alice, alice_cs, alice_ss, alice_db = await make_client(alice_mxid, alice_token, alice_dev, tmp / "alice.db")
    await alice.crypto.share_keys()
    await sync_once(alice, alice_ss, first=True)

    # 1. alice sends while sole member (send_message_event auto-encrypts)
    msg1 = f"secret-before-join {secrets.token_hex(4)}"
    msg1_id = await alice.send_message_event(
        room_id, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=msg1))
    print(f"[repro] pre-join msg: {msg1_id}", flush=True)

    # 2. bob invited + joins after the fact
    s, r = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/invite",
                 {"user_id": bob_mxid}, token=alice_token)
    assert s == 200, f"invite: {s} {r}"
    s, r = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join",
                 {}, token=bob_token)
    assert s == 200, f"join: {s} {r}"

    bob, bob_cs, bob_ss, bob_db = await make_client(bob_mxid, bob_token, bob_dev, tmp / "bob.db")
    await bob.crypto.share_keys()
    await sync_once(bob, bob_ss, first=True)

    # 3. bob receives the old event but can't decrypt it
    raw1 = fetch_event(room_id, str(msg1_id), bob_token)
    log("server serves pre-join event to new member (history_visibility works)",
        raw1 is not None and raw1["type"] == "m.room.encrypted")
    evt1 = Event.deserialize(raw1)
    session_id = raw1["content"]["session_id"]
    sender_key = raw1["content"]["sender_key"]
    try:
        await bob.crypto.decrypt_megolm_event(evt1)
        log("pre-join msg undecryptable (SessionNotFound)", False, "decrypted?!")
    except SessionNotFound:
        log("pre-join msg undecryptable (SessionNotFound)", True,
            "prod symptom: 'no session with given id found'")

    # 4. bob asks alice for the key; mautrix default policy refuses cross-user
    stop = asyncio.Event()
    async def pump(client, ss):
        while not stop.is_set():
            await sync_once(client, ss, timeout=1000)
    pumps = [asyncio.create_task(pump(alice, alice_ss)),
             asyncio.create_task(pump(bob, bob_ss))]
    got = await bob.crypto.request_room_key(
        room_id, sender_key, SessionID(session_id),
        {UserID(alice_mxid): [alice_dev]},
        timeout=15)
    log("key request from new member is refused", not got,
        "mautrix: 'Ignoring key request from a different user'")
    stop.set()
    await asyncio.gather(*pumps, return_exceptions=True)

    # 5. control: post-join message decrypts fine
    await sync_once(alice, alice_ss)
    msg2 = f"after-join {secrets.token_hex(4)}"
    msg2_id = await alice.send_message_event(
        room_id, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=msg2))
    await sync_once(bob, bob_ss, timeout=5000)
    evt2 = Event.deserialize(fetch_event(room_id, str(msg2_id), bob_token))
    dec2 = await bob.crypto.decrypt_megolm_event(evt2)
    log("post-join msg decrypts (E2EE itself works)", dec2.content.body == msg2)

    # 6. fix preview: alice exports the session, bob imports it -> history opens
    sess = await alice_cs.get_group_session(room_id, SessionID(session_id))
    exported = sess.export_session(sess.first_known_index)
    imported = InboundGroupSession.import_session(
        exported, signing_key=sess.signing_key, sender_key=sess.sender_key,
        room_id=sess.room_id)
    await bob_cs.put_group_session(sess.room_id, sess.sender_key,
                                   SessionID(session_id), imported)
    dec1 = await bob.crypto.decrypt_megolm_event(evt1)
    log("escrowed key transfer unlocks pre-join history", dec1.content.body == msg1)

    await alice_db.stop()
    await bob_db.stop()

    failed = [n for n, ok in results if not ok]
    print(f"\n[repro] {len(results) - len(failed)}/{len(results)} checks passed", flush=True)
    sys.exit(1 if failed else 0)


asyncio.run(main())
