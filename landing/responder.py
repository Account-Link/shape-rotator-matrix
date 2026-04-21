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
import asyncio, os, sys
from pathlib import Path

from mautrix.api import HTTPAPI
from mautrix.client import Client
from mautrix.client.state_store import MemoryStateStore, MemorySyncStore
from mautrix.types import (UserID, EventType, MessageType,
                           TextMessageEventContent, TrustState)
from mautrix.crypto import OlmMachine
from mautrix.crypto.store.asyncpg import PgCryptoStore
from mautrix.util.async_db import Database

from sas_verification import SASVerificationManager

HS      = os.environ["HS"].rstrip("/")
MXID    = os.environ["MXID"]
TOKEN   = os.environ["TOKEN"]
DEVICE  = os.environ["DEVICE"]
DM_ROOM = os.environ.get("DM_ROOM", "").strip()
INTRO   = os.environ.get("INTRO", "").strip()
STORE   = Path(os.environ.get("STORE", "./bot_crypto.db"))

COMMANDS = {
    "!ping":   lambda arg: "pong",
    "!whoami": lambda arg: f"I am {MXID}",
}
COMMANDS["!help"] = lambda arg: "commands: " + ", ".join(sorted(COMMANDS))


class _StateStore:
    def __init__(self, inner):
        self._inner, self._joined = inner, set()
    async def is_encrypted(self, room_id):
        return (await self.get_encryption_info(room_id)) is not None
    async def get_encryption_info(self, room_id):
        if hasattr(self._inner, "get_encryption_info"):
            return await self._inner.get_encryption_info(room_id)
        return None
    async def find_shared_rooms(self, user_id):
        return list(self._joined)


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
    SASVerificationManager(client, cs, MXID, DEVICE)

    async def on_msg(evt):
        if evt.sender == MXID:
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
        print(f"POSTED:{resp.event_id}", flush=True)

    while True:
        try:
            await sync_once(client, ss)
        except Exception as e:
            print(f"sync error: {e}", flush=True)
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
