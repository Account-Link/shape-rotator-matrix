"""One-shot E2EE message sender as @shape-rotator-2.

Reads `KNOCK_APPROVER_TOKEN` from `.env`, resolves the bot's mxid + device
via /whoami, brings up a mautrix client + OlmMachine on a persistent
crypto store, syncs once to learn room state + peer device keys, then
sends a single encrypted message via send_message_event.

Usage (run inside the test-runner image — the wrapper deploy/admin/send.sh
handles that):
    python3 deploy/admin/post_message.py <room-id-or-alias> '<body>'
    python3 deploy/admin/post_message.py <room> --file <path-to-text-file>

The crypto store at deploy/admin/.sr2-crypto.db is reused across calls so
device-key bootstrap is amortised. Don't commit it (gitignored).
"""
import asyncio, json, os, sys, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENV_LOCAL = REPO / ".env"
STORE = REPO / "deploy" / "admin" / ".sr2-crypto.db"
HS = "https://mtrx.shaperotator.xyz"


def env(key):
    for line in ENV_LOCAL.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return None


def matrix_get(path, token):
    req = urllib.request.Request(f"{HS}{path}",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


async def main():
    if len(sys.argv) < 3:
        print("usage: post_message.py <room-id-or-alias> '<body>'", file=sys.stderr)
        print("       post_message.py <room-id-or-alias> --file <path>", file=sys.stderr)
        sys.exit(1)
    room_arg = sys.argv[1]
    if sys.argv[2] == "--file":
        body = Path(sys.argv[3]).read_text()
    else:
        body = sys.argv[2]

    token = env("KNOCK_APPROVER_TOKEN")
    if not token:
        print("FAIL: no KNOCK_APPROVER_TOKEN in .env", file=sys.stderr)
        sys.exit(1)

    me = matrix_get("/_matrix/client/v3/account/whoami", token)
    mxid, device = me["user_id"], me["device_id"]
    print(f"identity: {mxid} device={device}", flush=True)

    if room_arg.startswith("#"):
        info = matrix_get(
            f"/_matrix/client/v3/directory/room/{urllib.parse.quote(room_arg)}",
            token)
        room_id = info["room_id"]
        print(f"alias {room_arg} -> {room_id}", flush=True)
    else:
        room_id = room_arg

    # Imports are deferred so the script's --help / arg parsing works without
    # mautrix installed (helpful for syntax-only checks outside the container).
    from mautrix.api import HTTPAPI
    from mautrix.client import Client
    from mautrix.client.state_store import MemoryStateStore, MemorySyncStore
    from mautrix.types import (UserID, EventType, MessageType,
                               TextMessageEventContent, TrustState)
    from mautrix.crypto import OlmMachine
    from mautrix.crypto.store.asyncpg import PgCryptoStore
    from mautrix.util.async_db import Database

    class _StateStore:
        def __init__(self, inner):
            self._inner, self._joined = inner, set()
        async def is_encrypted(self, rid):
            return (await self.get_encryption_info(rid)) is not None
        async def get_encryption_info(self, rid):
            if hasattr(self._inner, "get_encryption_info"):
                return await self._inner.get_encryption_info(rid)
            return None
        async def find_shared_rooms(self, uid):
            return list(self._joined)

    api = HTTPAPI(base_url=HS, token=token)
    state, sync = MemoryStateStore(), MemorySyncStore()
    client = Client(mxid=UserID(mxid), device_id=device, api=api,
                    state_store=state, sync_store=sync)
    STORE.parent.mkdir(parents=True, exist_ok=True)
    db = Database.create(f"sqlite:///{STORE}",
                         upgrade_table=PgCryptoStore.upgrade_table)
    await db.start()
    cs = PgCryptoStore(account_id=mxid, pickle_key=f"{mxid}:{device}", db=db)
    await cs.open()
    ss = _StateStore(state)
    olm = OlmMachine(client, cs, ss)
    olm.share_keys_min_trust = TrustState.UNVERIFIED
    olm.send_keys_min_trust  = TrustState.UNVERIFIED
    await olm.load()
    client.crypto = olm
    client.crypto_store = cs

    print("syncing room state + member devices...", flush=True)
    data = await client.sync(timeout=3000, full_state=True)
    if isinstance(data, dict):
        ss._joined.clear()
        ss._joined.update(data.get("rooms", {}).get("join", {}).keys())
        nb = data.get("next_batch")
        if nb:
            await client.sync_store.put_next_batch(nb)
        tasks = client.handle_sync(data)
        if tasks:
            await asyncio.gather(*tasks)
    await client.crypto.share_keys()

    if room_id not in ss._joined:
        print(f"WARN: {room_id} not in our /sync join set; OlmMachine may have stale device list",
              file=sys.stderr, flush=True)

    print(f"sending to {room_id}...", flush=True)
    content = TextMessageEventContent(msgtype=MessageType.TEXT, body=body)
    event_id = await client.send_message_event(
        room_id, EventType.ROOM_MESSAGE, content)
    print(f"sent: {event_id}", flush=True)

    await db.stop()


if __name__ == "__main__":
    asyncio.run(main())
