"""Exercise MSC4268 delivery through the production responder handler."""
import asyncio
import json
import os
import secrets
import sys
import urllib.parse
from types import SimpleNamespace

from mautrix.crypto import attachments
from mautrix.types import Event, EventType, TextMessageEventContent, UserID

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))
from sas_e2e import HS, _post, make_client, register


def get_json(path, token):
    import urllib.request
    req = urllib.request.Request(f"{HS}{path}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read() or b"{}")


async def main():
    sys.path.insert(0, os.path.join(REPO, "landing"))
    os.environ.setdefault("HS", HS)

    suffix = secrets.token_hex(4)
    alice_mxid, alice_token = register(f"bundle_alice_{suffix}", secrets.token_urlsafe(24), f"AL{suffix}")
    bot_mxid, bot_token = register(f"bundle_bot_{suffix}", secrets.token_urlsafe(24), f"BO{suffix}")
    bob_mxid, bob_token = register(f"bundle_bob_{suffix}", secrets.token_urlsafe(24), f"BB{suffix}")
    os.environ.update(MXID=bob_mxid, TOKEN=bob_token, DEVICE=f"BB{suffix}")
    from responder import _StateStore, import_room_key_bundle, register_room_key_bundle_handler, sync_once

    status, room = _post(f"{HS}/_matrix/client/v3/createRoom", {
        "preset": "private_chat",
        "initial_state": [
            {"type": "m.room.history_visibility", "state_key": "",
             "content": {"history_visibility": "shared"}},
            {"type": "m.room.encryption", "state_key": "",
             "content": {"algorithm": "m.megolm.v1.aes-sha2"}},
        ],
    }, token=alice_token)
    assert status == 200, room
    room_id = room["room_id"]
    status, body = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/invite",
                         {"user_id": bot_mxid}, token=alice_token)
    assert status == 200, body
    status, body = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join",
                         {}, token=bot_token)
    assert status == 200, body

    alice, _, alice_state, alice_db = await make_client(alice_mxid, alice_token, f"AL{suffix}", f"/tmp/{suffix}-a.db")
    bot, bot_store, bot_state, bot_db = await make_client(bot_mxid, bot_token, f"BO{suffix}", f"/tmp/{suffix}-bot.db")
    bob, bob_store, _, bob_db = await make_client(bob_mxid, bob_token, f"BB{suffix}", f"/tmp/{suffix}-b.db")
    await alice.crypto.share_keys()
    await bot.crypto.share_keys()
    await bob.crypto.share_keys()
    await sync_once(alice, alice_state, first=True)
    await sync_once(bot, bot_state, first=True)

    secret = f"pre-join {suffix}"
    event_id = await alice.send_message_event(
        room_id, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype="m.text", body=secret))
    await sync_once(bot, bot_state)

    # Build the same attachment shape emitted by chip 3, from the inviter's
    # actual persistent inbound sessions.
    rows = await bot_store.db.fetch(
        "SELECT session_id, sender_key FROM crypto_megolm_inbound_session WHERE room_id=$1 AND account_id=$2",
        room_id, bot_store.account_id)
    room_keys = []
    for row in rows:
        session = await bot_store.get_group_session(room_id, str(row["session_id"]))
        room_keys.append({
            "algorithm": "m.megolm.v1.aes-sha2", "room_id": room_id,
            "sender_claimed_keys": {"ed25519": str(session.signing_key)},
            "sender_key": str(row["sender_key"]), "session_id": str(row["session_id"]),
            "session_key": session.export_session(session.first_known_index),
        })
    plaintext = json.dumps({"room_keys": room_keys, "withheld": []}, separators=(",", ":"), sort_keys=True).encode()
    ciphertext, encrypted_file = attachments.encrypt_attachment(plaintext)
    import aiohttp
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {bot_token}"}) as http:
        async with http.post(f"{HS}/_matrix/media/v3/upload?filename=room-key-bundle.json",
                             data=ciphertext, headers={"Content-Type": "application/octet-stream"}) as response:
            uploaded = await response.json()
    file_info = encrypted_file.serialize()
    file_info["url"] = uploaded["content_uri"]
    content = {"room_id": room_id, "file": file_info}

    status, body = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/invite",
                         {"user_id": bob_mxid}, token=bot_token)
    assert status == 200, body
    bob_state = _StateStore(bob.state_store)
    register_room_key_bundle_handler(bob, bob_store, bob_state)
    await sync_once(bob, bob_state, first=True)

    devices = await bot.crypto._fetch_keys([UserID(bob_mxid)], include_untracked=True)
    event_type = EventType.find("m.room_key_bundle", EventType.Class.TO_DEVICE)
    for device in devices[UserID(bob_mxid)].values():
        await bot.crypto.send_encrypted_to_device(device, event_type, content)
    await sync_once(bob, bob_state)

    outsider = SimpleNamespace(sender="@outsider:localhost", content=content)
    assert not await import_room_key_bundle(outsider, bob_store, bob_state)

    status, body = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join",
                         {}, token=bob_token)
    assert status == 200, body
    raw = get_json(f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/messages?dir=b&limit=100", bob_token)
    event = next(e for e in raw["chunk"] if e.get("event_id") == str(event_id))
    decrypted = await bob.crypto.decrypt_megolm_event(Event.deserialize(event))
    assert decrypted.content.body == secret
    print("MSC4268 responder E2E: inviter bundle imported; outsider rejected; pre-join message decrypted")
    for db in (alice_db, bot_db, bob_db):
        await db.stop()


if __name__ == "__main__":
    asyncio.run(main())
