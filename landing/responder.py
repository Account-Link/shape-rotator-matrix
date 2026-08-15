"""Shape Rotator Matrix bot — mautrix-python responder with SAS auto-verify.

Companion to landing/sas_verification.py. Drop both in a directory, set env
vars from /signup/api's response, run this:

    pip install 'mautrix[e2be]' asyncpg aiosqlite python-olm unpaddedbase64

    export HS=https://mtrx.shaperotator.xyz
    export MXID=@you:mtrx.shaperotator.xyz
    export TOKEN=...
    export DEVICE=...
    # optional: post an intro on startup and exit-once semantics
    export DM_ROOM=!...:mtrx.shaperotator.xyz
    export INTRO="hi, I'm live"
    python3 responder.py

Commands in any E2EE room the bot is in:
    !ping, !whoami, !help

Verification: open Element → bot's profile → "Verify" → the bot auto-accepts
and auto-completes SAS. No emoji confirmation required from the bot side.
"""
import asyncio, json, logging, os, sys
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp

from mautrix.api import HTTPAPI
from mautrix.client import Client
from mautrix.client.state_store import MemoryStateStore, MemorySyncStore
from mautrix.types import (UserID, EventType, MessageType, SessionID,
                           TextMessageEventContent, TrustState)
from mautrix.crypto import OlmMachine
from mautrix.crypto.attachments import decrypt_attachment
from mautrix.crypto.sessions import InboundGroupSession
from mautrix.crypto.store.asyncpg import PgCryptoStore
from mautrix.util.async_db import Database

from sas_verification import SASVerificationManager

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(name)s: %(message)s",
)

HS      = os.environ["HS"].rstrip("/")
MXID    = os.environ["MXID"]
TOKEN   = os.environ["TOKEN"]
DEVICE  = os.environ["DEVICE"]
DM_ROOM = os.environ.get("DM_ROOM", "").strip()
INTRO   = os.environ.get("INTRO", "").strip()
STORE   = Path(os.environ.get("STORE", "./bot_crypto.db"))

log = logging.getLogger("shape-rotator.responder")

COMMANDS = {
    "!ping":   lambda arg: "pong",
    "!whoami": lambda arg: f"I am {MXID}",
}
COMMANDS["!help"] = lambda arg: "commands: " + ", ".join(sorted(COMMANDS))


class _StateStore:
    def __init__(self, inner):
        self._inner, self._joined = inner, set()
        self._inviters = {}
    async def is_encrypted(self, room_id):
        return (await self.get_encryption_info(room_id)) is not None
    async def get_encryption_info(self, room_id):
        if hasattr(self._inner, "get_encryption_info"):
            return await self._inner.get_encryption_info(room_id)
        return None
    async def find_shared_rooms(self, user_id):
        return list(self._joined)


def _event_content(event):
    content = event.content if hasattr(event, "content") else event.get("content", {})
    if hasattr(content, "serialize"):
        content = content.serialize()
    return content


def _remember_inviters(ss, rooms):
    """Remember each invite's sender before the responder joins the room."""
    for room_id, invite in rooms.items():
        for event in invite.get("invite_state", {}).get("events", []):
            if (event.get("type") == "m.room.member"
                    and event.get("state_key") == MXID
                    and event.get("content", {}).get("membership") == "invite"):
                inviter = event.get("sender")
                if inviter:
                    ss._inviters[room_id] = inviter
                    break


async def _download_mxc(content_uri):
    parsed = urlsplit(content_uri)
    if parsed.scheme != "mxc" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError("bundle file url is not a valid mxc URI")
    media_id = parsed.path.lstrip("/")
    url = (f"{HS}/_matrix/media/v3/download/{quote(parsed.netloc, safe='')}/"
           f"{quote(media_id, safe='/')}")
    async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {TOKEN}"}) as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise ValueError(f"bundle media download returned HTTP {response.status}")
            return await response.read()


async def import_room_key_bundle(event, crypto_store, state_store):
    """Import an MSC4268 bundle only when its sender issued this invite."""
    content = _event_content(event)
    room_id = content.get("room_id")
    inviter = state_store._inviters.get(room_id)
    sender = str(getattr(event, "sender", ""))
    if not room_id or not inviter or sender != inviter:
        log.error("rejected m.room_key_bundle from non-inviter sender=%s room=%s inviter=%s",
                  sender, room_id, inviter)
        return False

    file_info = content.get("file") or {}
    if not isinstance(file_info.get("url"), str) or not file_info["url"].startswith("mxc://"):
        log.error("rejected m.room_key_bundle with invalid attachment room=%s", room_id)
        return False
    try:
        plaintext = decrypt_attachment(
            await _download_mxc(file_info["url"]),
            file_info["key"]["k"], file_info["hashes"]["sha256"], file_info["iv"])
        bundle = json.loads(plaintext)
        room_keys = bundle.get("room_keys")
        if not isinstance(room_keys, list):
            raise ValueError("room_keys is not a list")
        imported = 0
        for item in room_keys:
            if (item.get("algorithm") != "m.megolm.v1.aes-sha2"
                    or item.get("room_id") != room_id):
                raise ValueError("bundle contains a room or algorithm mismatch")
            session = InboundGroupSession.import_session(
                item["session_key"],
                signing_key=item["sender_claimed_keys"]["ed25519"],
                sender_key=item["sender_key"], room_id=room_id)
            await crypto_store.put_group_session(
                room_id, item["sender_key"], SessionID(item["session_id"]), session)
            imported += 1
        log.info("imported %d room key sessions from inviter=%s room=%s",
                 imported, inviter, room_id)
        return True
    except Exception as exc:
        log.error("rejected m.room_key_bundle room=%s: %s", room_id, exc)
        return False


def register_room_key_bundle_handler(client, crypto_store, state_store):
    bundle_type = EventType.find("m.room_key_bundle", EventType.Class.TO_DEVICE)

    # OlmMachine consumes unknown decrypted to-device events instead of
    # dispatching them. Replace its encrypted-event hook so custom bundles
    # still go through the normal Olm decryption path; retain its built-in
    # room-key handling for all standard events.
    original_handler = client.crypto.handle_to_device_event
    client.remove_event_handler(EventType.TO_DEVICE_ENCRYPTED, original_handler)

    async def on_encrypted(event):
        decrypted = await client.crypto._decrypt_olm_event(event)
        if decrypted.type == bundle_type:
            await import_room_key_bundle(decrypted, crypto_store, state_store)
        elif decrypted.type == EventType.ROOM_KEY:
            await client.crypto._receive_room_key(decrypted)
        elif decrypted.type == EventType.FORWARDED_ROOM_KEY:
            await client.crypto._receive_forwarded_room_key(decrypted)

    client.add_event_handler(EventType.TO_DEVICE_ENCRYPTED, on_encrypted, wait_sync=True)


async def make_client():
    api = HTTPAPI(base_url=HS, token=TOKEN)
    state, sync = MemoryStateStore(), MemorySyncStore()
    client = Client(mxid=UserID(MXID), device_id=DEVICE, api=api,
                    state_store=state, sync_store=sync)
    db = Database.create(f"sqlite:///{STORE}",
                         upgrade_table=PgCryptoStore.upgrade_table)
    await db.start()
    cs = PgCryptoStore(account_id=MXID, pickle_key=f"{MXID}:{DEVICE}", db=db)
    await cs.open()
    ss = _StateStore(state)
    olm = OlmMachine(client, cs, ss)
    olm.share_keys_min_trust = TrustState.UNVERIFIED
    olm.send_keys_min_trust  = TrustState.UNVERIFIED
    await olm.load()
    client.crypto = olm
    client.crypto_store = cs
    return client, cs, ss


async def sync_once(client, ss, timeout=30000, first=False):
    since = None if first else await client.sync_store.get_next_batch()
    data = await client.sync(since=since, timeout=timeout, full_state=first)
    if not isinstance(data, dict):
        return
    nb = data.get("next_batch")
    if nb:
        await client.sync_store.put_next_batch(nb)
    rooms = data.get("rooms", {})
    _remember_inviters(ss, rooms.get("invite", {}))
    ss._joined.clear()
    ss._joined.update(rooms.get("join", {}).keys())
    tasks = client.handle_sync(data)
    if tasks:
        await asyncio.gather(*tasks)
    for rid in rooms.get("invite", {}).keys():
        try:
            await client.api.request(
                "POST", f"/_matrix/client/v3/rooms/{rid}/join", content={})
            print(f"joined invite {rid}", flush=True)
        except Exception as e:
            print(f"join failed: {e}", flush=True)


async def main():
    client, cs, ss = await make_client()
    await client.crypto.share_keys()
    print(f"responder up as {MXID} device={DEVICE} ident={client.crypto.account.identity_key[:12]}...", flush=True)
    sas_manager = SASVerificationManager(client, cs, MXID, DEVICE)
    register_room_key_bundle_handler(client, cs, ss)

    async def on_msg(evt):
        if evt.sender == MXID:
            return
        if await sas_manager.handle_room_message(evt):
            return
        body = (getattr(evt.content, "body", "") or "").strip()
        cmd = body.split()[0] if body else ""
        fn = COMMANDS.get(cmd)
        if not fn:
            return
        arg = body[len(cmd):].strip()
        reply = TextMessageEventContent(msgtype=MessageType.TEXT, body=fn(arg))
        await client.send_message_event(evt.room_id, EventType.ROOM_MESSAGE, reply)

    client.add_event_handler(EventType.ROOM_MESSAGE, on_msg)

    await sync_once(client, ss, timeout=3000, first=True)
    if DM_ROOM and INTRO:
        content = TextMessageEventContent(msgtype=MessageType.TEXT, body=INTRO)
        resp = await client.send_message_event(DM_ROOM, EventType.ROOM_MESSAGE, content)
        print(f"POSTED:{resp}", flush=True)

    while True:
        try:
            await sync_once(client, ss)
        except Exception as e:
            print(f"sync error: {e}", flush=True)
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
