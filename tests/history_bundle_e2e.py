"""MSC4268 bundle round-trip against the dev stack.

The bot receives one Megolm session from each of two senders, builds and
uploads the bundle, and a client that joins afterwards downloads, imports, and
decrypts both pre-join messages.
"""
import asyncio, json, os, secrets, sys, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
from sas_e2e import HS, REG_TOKEN, _post, make_client, register, sync_once

from mautrix.crypto import attachments
from mautrix.crypto.sessions import InboundGroupSession
from mautrix.errors import SessionNotFound
from mautrix.types import Event, EventType, MessageType, SessionID, TextMessageEventContent


def get_json(path, token):
    req = urllib.request.Request(
        f"{HS}{path}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read() or b"{}")


def messages(room_id, token):
    return get_json(
        f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/messages?dir=b&limit=100",
        token).get("chunk", [])


async def main():
    suffix = f"{int(asyncio.get_running_loop().time())}_{secrets.token_hex(2)}"
    senders = []
    for label in ("alice", "carol"):
        senders.append(register(f"bundle_{label}_{suffix}", secrets.token_urlsafe(24),
                                f"B{label.upper()}{secrets.token_hex(2)}"))
    bot_mxid, bot_token = register(f"bundle_bot_{suffix}", secrets.token_urlsafe(24),
                                   f"BBOT{secrets.token_hex(2)}")
    bot_device = get_json("/_matrix/client/v3/account/whoami", bot_token)["device_id"]

    status, created = _post(f"{HS}/_matrix/client/v3/createRoom", {
        "preset": "private_chat",
        "initial_state": [
            {"type": "m.room.history_visibility", "state_key": "",
             "content": {"history_visibility": "shared"}},
            {"type": "m.room.encryption", "state_key": "",
             "content": {"algorithm": "m.megolm.v1.aes-sha2"}},
        ],
    }, token=senders[0][1])
    assert status == 200, (status, created)
    room_id = created["room_id"]
    for mxid, _token in [senders[1], (bot_mxid, bot_token)]:
        status, body = _post(
            f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/invite",
            {"user_id": mxid}, token=senders[0][1])
        assert status == 200, (status, body)
    for mxid, token in [senders[1], (bot_mxid, bot_token)]:
        status, body = _post(
            f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join",
            {}, token=token)
        assert status == 200, (status, body)

    clients = []
    for index, (mxid, token) in enumerate(senders + [(bot_mxid, bot_token)]):
        device = (get_json("/_matrix/client/v3/account/whoami", token)
                  ["device_id"])
        client = await make_client(mxid, token, device,
                                   f"/tmp/bundle_{suffix}_{index}.db")
        clients.append(client)
        await client[0].crypto.share_keys()
        await sync_once(client[0], client[2], first=True)

    events = []
    for index, (client, (mxid, token)) in enumerate(zip(clients[:2], senders)):
        body = f"bundle prejoin {index} {secrets.token_hex(4)}"
        event_id = await client[0].send_message_event(
            room_id, EventType.ROOM_MESSAGE,
            TextMessageEventContent(msgtype=MessageType.TEXT, body=body))
        events.append((str(event_id), body))
    for _ in range(4):
        await sync_once(clients[2][0], clients[2][2])

    # Import the production builder with the test bot's already-open store.
    os.environ.update(HS=HS, SPACE_ID=room_id, MATRIX_TOKEN=bot_token)
    sys.path.insert(0, str(REPO / "knock-approver"))
    import approver
    approver._ROOM_KEY_BUNDLE_STORE = clients[2][1]
    bundle = await approver.build_room_key_bundle(room_id)
    assert bundle["url"] == bundle["file"]["url"]
    assert len(bundle["file"]["hashes"]["sha256"]) > 10

    uri = bundle["url"].split("://", 1)[1]
    server, media_id = uri.split("/", 1)
    ciphertext = urllib.request.urlopen(
        urllib.request.Request(
            f"{HS}/_matrix/media/v3/download/{server}/{media_id}",
            headers={"Authorization": f"Bearer {bot_token}"}), timeout=15).read()
    info = bundle["file"]
    plaintext = attachments.decrypt_attachment(
        ciphertext, info["key"]["k"], info["hashes"]["sha256"], info["iv"])
    exported = json.loads(plaintext)
    assert len(exported["room_keys"]) >= 2
    assert not exported["withheld"]

    # Bob joins after both messages and cannot decrypt until the bundle is imported.
    bob_mxid, bob_token = register(f"bundle_bob_{suffix}", secrets.token_urlsafe(24),
                                   f"BBOB{secrets.token_hex(2)}")
    status, body = _post(
        f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/invite",
        {"user_id": bob_mxid}, token=senders[0][1])
    assert status == 200, (status, body)
    status, body = _post(
        f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join",
        {}, token=bob_token)
    assert status == 200, (status, body)
    bob_device = get_json("/_matrix/client/v3/account/whoami", bob_token)["device_id"]
    bob, bob_store, bob_state, bob_db = await make_client(
        bob_mxid, bob_token, bob_device, f"/tmp/bundle_{suffix}_bob.db")
    await bob.crypto.share_keys()
    await sync_once(bob, bob_state, first=True)
    raw_events = {str(event["event_id"]): event for event in messages(room_id, bob_token)}
    for event_id, _body in events:
        try:
            await bob.crypto.decrypt_megolm_event(Event.deserialize(raw_events[event_id]))
        except SessionNotFound:
            pass
        else:
            raise AssertionError("pre-join event decrypted before bundle import")

    for item in exported["room_keys"]:
        imported = InboundGroupSession.import_session(
            item["session_key"], signing_key=item["sender_claimed_keys"]["ed25519"],
            sender_key=item["sender_key"], room_id=room_id)
        await bob_store.put_group_session(
            room_id, item["sender_key"], SessionID(item["session_id"]), imported)
    for event_id, body in events:
        decrypted = await bob.crypto.decrypt_megolm_event(Event.deserialize(raw_events[event_id]))
        assert decrypted.content.body == body
    print("MSC4268 bundle round-trip: 2 sessions exported, uploaded, downloaded, imported, decrypted")
    for _client, _store, _state, db in clients:
        await db.stop()
    await bob_db.stop()


if __name__ == "__main__":
    asyncio.run(main())
