"""Send the pre-deploy heads-up message into the (E2EE) admin room.

Runs in GH Actions; unpacks CI_DEPLOY_BOT_BUNDLE_B64 (minted by
bootstrap_ci_device.py), initialises mautrix Client + OlmMachine against
the bundled sqlite store, sends an encrypted m.room.message via
send_message_event (mautrix auto-encrypts for E2EE rooms).

Usage:
  send_heads_up.py <ref> <short_sha> <delay_seconds> <run_id> <commit_msg>
"""
import asyncio, base64, io, json, os, sys, tarfile, tempfile
from pathlib import Path

from mautrix.client import Client
from mautrix.client.state_store import MemoryStateStore
from mautrix.crypto import OlmMachine
from mautrix.crypto.store.asyncpg import PgCryptoStore
from mautrix.util.async_db import Database
from mautrix.types import (UserID, EventType, TextMessageEventContent,
                            MessageType, TrustState, RoomID)


class _StateStore(MemoryStateStore):
    """Wraps MemoryStateStore with find_shared_rooms() — mautrix's OlmMachine
    needs it during share_group_session to decide which devices to key-share
    with. We only ever send to one room, so return it for everyone."""
    def __init__(self, room_id):
        super().__init__()
        self._only_room = room_id
    async def find_shared_rooms(self, user_id):
        return [self._only_room]


async def main():
    ref, sha, delay, run_id, *rest = sys.argv[1:]
    commit_msg = rest[0] if rest else ""
    room_id = os.environ["ADMIN_COMMAND_ROOM"]
    bundle_b64 = os.environ["CI_DEPLOY_BOT_BUNDLE_B64"]

    tmp = Path(tempfile.mkdtemp(prefix="ci-deploy-"))
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(bundle_b64)),
                      mode="r:gz") as tar:
        tar.extractall(tmp)
    meta = json.loads((tmp / "meta.json").read_text())

    # Client wired to the bundled token + device.
    client = Client(mxid=UserID(meta["mxid"]),
                    base_url=meta["homeserver"],
                    token=meta["access_token"],
                    device_id=meta["device_id"],
                    state_store=MemoryStateStore())

    # sqlite crypto store from the bundled .db (same setup the bot uses).
    db = Database.create(f"sqlite:///{tmp / 'ci-store.db'}",
                         upgrade_table=PgCryptoStore.upgrade_table)
    await db.start()
    cs_store = PgCryptoStore(account_id=meta["mxid"],
                             pickle_key=meta["pickle_key"], db=db)
    await cs_store.open()
    olm = OlmMachine(client, cs_store, _StateStore(RoomID(room_id)))
    olm.share_keys_min_trust = TrustState.UNVERIFIED
    olm.send_keys_min_trust = TrustState.UNVERIFIED
    await olm.load()
    client.crypto = olm
    client.crypto_store = cs_store

    # Refresh recipient device lists so the megolm session shares with
    # everyone currently in the room.
    await client.sync(timeout=3000)

    minutes = max(1, int(delay) // 60)
    body = (f"🚧 deploying {ref} ({sha}) in ~{minutes} min — "
            f"mtrx.shaperotator.xyz briefly unreachable while the CVM "
            f"restarts.\n\n"
            f"{commit_msg or '(no commit message)'}\n\n"
            f"to cancel: https://github.com/teleport-computer/"
            f"shape-rotator-matrix/actions/runs/{run_id}")
    content = TextMessageEventContent(msgtype=MessageType.TEXT, body=body)
    evt = await client.send_message_event(RoomID(room_id),
                                           EventType.ROOM_MESSAGE, content)
    print(f"[heads-up] sent event_id={evt}", flush=True)

    await cs_store.close()
    await db.stop()
    if client.api.session:
        await client.api.session.close()


asyncio.run(main())
