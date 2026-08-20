#!/usr/bin/env python3
"""bridge/mx.py — E2EE Matrix bot CLI (send / tail) for the shape bridge.

Sends a plain-text message into the bridge test room and tails its recent
decrypted transcript, fully end-to-end-encrypted via mautrix-python.

Credentials & fixtures are read from operator-provisioned paths and are NEVER
committed:

  ~/.shape-bridge-bot/creds.json             mxid, access_token, device_id, homeserver
  ~/.shape-bridge-bot/store/                 crypto.db + /sync cursor (created 1st run)
  ~/.shape-bridge-bot/store/recovery_key.txt master-key recovery (written 1st run)
  ~/.teleport-travel/test-fixtures.json      matrix_room_id — the ONLY room

HARD RULE: this tool may only talk to the single Matrix room named in the
test-fixtures file. There is no CLI flag to override the target room — by
design. Production pairing is an operator-only config change.

Follows MATRIX_ONBOARDING.md "mautrix-python known bugs" (non-negotiables):

  - wraps ``MemoryStateStore`` with ``is_encrypted`` / ``find_shared_rooms`` /
    ``get_encryption_info`` (OlmMachine silently misbehaves without them)
  - persists ``next_batch`` via the sync store on every sync and gathers the
    tasks ``handle_sync`` returns (skipping either drops dispatch)
  - uses the unsuffixed Continuwuity room id (the ``!foo:server`` form 404s)
  - first run: cross-signs via ``OlmMachine.generate_recovery_key()`` and
    persists the recovery key, then posts exactly one outgoing message after
    the first sync so Element rotates/shares megolm keys to this device
"""
import argparse
import asyncio
import json
import os
import stat
import sys
import time
import urllib.request
from pathlib import Path

from mautrix.api import HTTPAPI
from mautrix.client import Client
from mautrix.client.state_store import MemoryStateStore, SyncStore
from mautrix.crypto import OlmMachine
from mautrix.crypto.store.asyncpg import PgCryptoStore
from mautrix.types import (
    EncryptedEvent,
    Event,
    EventType,
    MessageType,
    TextMessageEventContent,
    TrustState,
    UserID,
)
from mautrix.util.async_db import Database

CREDS_PATH = Path(
    os.environ.get("MX_CREDS", str(Path.home() / ".shape-bridge-bot/creds.json"))
)
STORE_DIR = Path(
    os.environ.get("MX_STORE", str(Path.home() / ".shape-bridge-bot/store"))
)
CRYPTO_DB = STORE_DIR / "crypto.db"
SYNC_CURSOR = STORE_DIR / "next_batch"
RECOVERY_PATH = STORE_DIR / "recovery_key.txt"
WOKE_MARKER = STORE_DIR / ".woke"
# Set once this device has been signed by the account's existing cross-signing
# key. Separate from .woke: waking is about megolm shares, this is about the
# trust shield.
VERIFIED_MARKER = STORE_DIR / ".verified"
FIXTURES_PATH = Path(
    os.environ.get(
        "MX_FIXTURES", str(Path.home() / ".teleport-travel/test-fixtures.json")
    )
)

# Posted once, on first run, to make other clients share megolm keys to this
# device. MATRIX_ONBOARDING.md "Element client behavior".
WAKE_TEXT = "shape-bridge E2EE bootstrapped — megolm rotation trigger"


class ConfigError(Exception):
    """Raised when operator-provisioned creds/fixtures are missing/invalid."""


def bootstrap_creds_from_password():
    """Mint creds.json by logging in with a password from the environment.

    For container deployments (the dstack pod) where no creds.json can be
    provisioned: the sealed env carries MATRIX_BRIDGE_USER + _PASSWORD, and the
    bot logs in once to mint its OWN access_token and device_id, writing them
    into the persistent volume. Subsequent boots find creds.json and skip this.

    This deliberately mints a NEW device. That is safe only because the bridge
    room is created fresh alongside it — a new device cannot decrypt megolm
    history that predates it, so pointing this at a room with history you care
    about would lose that history. Same reason mx.py never regenerates
    crypto.db under an existing device_id.

    Mirrors knock-approver/approver.py's `_login_with_password`."""
    user = os.environ.get("MATRIX_BRIDGE_USER")
    password = os.environ.get("MATRIX_BRIDGE_PASSWORD")
    hs = os.environ.get("MATRIX_HOMESERVER")
    if not (user and password and hs):
        return None
    body = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user},
        "password": password,
        "initial_device_display_name": "shape-bridge relay (pod)",
    }
    req = urllib.request.Request(
        f"{hs}/_matrix/client/v3/login",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)
    creds = {
        "user_id": res["user_id"],
        "access_token": res["access_token"],
        "device_id": res["device_id"],
        "homeserver": hs,
    }
    CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CREDS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(creds, indent=2))
    tmp.chmod(0o600)
    os.replace(tmp, CREDS_PATH)
    print(f"minted creds for {creds['user_id']} device={creds['device_id']}", flush=True)
    return creds


def assert_test_venue():
    """Same rule as tg.py: only a declared test venue may be driven by a send CLI."""
    if not FIXTURES_PATH.exists():
        raise ConfigError(f"missing test fixtures: {FIXTURES_PATH}")
    fixtures = json.loads(FIXTURES_PATH.read_text())
    if fixtures.get("test_venue") is not True:
        raise ConfigError(
            f"{FIXTURES_PATH} is a production pairing, not a test venue "
            '(needs \'"test_venue": true\'). Point TEST_FIXTURES_PATH at one.'
        )

def load_config():
    """Return (creds_dict, matrix_room_id) from the operator-provisioned paths."""
    if not CREDS_PATH.exists() and bootstrap_creds_from_password() is None:
        raise ConfigError(
            f"missing bot credentials: {CREDS_PATH} (and no MATRIX_BRIDGE_USER/"
            "_PASSWORD/MATRIX_HOMESERVER in the environment to mint them)"
        )
    if not FIXTURES_PATH.exists():
        raise ConfigError(f"missing test fixtures: {FIXTURES_PATH}")
    creds = json.loads(CREDS_PATH.read_text())
    fixtures = json.loads(FIXTURES_PATH.read_text())
    for key in ("user_id", "access_token", "device_id", "homeserver"):
        if not creds.get(key):
            raise ConfigError(f"creds.json missing field: {key}")
    room = fixtures.get("matrix_room_id")
    if not room:
        raise ConfigError("test-fixtures.json missing matrix_room_id")
    # Continuwuity room ids are unsuffixed (!foo, not !foo:server) — using the
    # suffixed form 404s. Reject anything that doesn't start with the bare sigil.
    if not room.startswith("!"):
        raise ConfigError(f"refusing suspicious matrix_room_id: {room!r}")
    return creds, room


class _CryptoStateStore:
    """Wraps ``MemoryStateStore`` with the three methods OlmMachine needs.

    See MATRIX_ONBOARDING.md "MemoryStateStore lacks is_encrypted /
    find_shared_rooms / get_encryption_info — OlmMachine silently misbehaves."
    """

    def __init__(self, inner):
        self._inner = inner
        # Joined-room set, refreshed each sync, so find_shared_rooms can answer.
        self._joined = set()

    async def is_encrypted(self, room_id):
        return (await self.get_encryption_info(room_id)) is not None

    async def get_encryption_info(self, room_id):
        if hasattr(self._inner, "get_encryption_info"):
            return await self._inner.get_encryption_info(room_id)
        return None

    async def find_shared_rooms(self, user_id):
        return list(self._joined)


class _FileSyncStore(SyncStore):
    """File-backed /sync cursor so the CLI's OlmMachine survives between runs.

    mautrix requires ``put_next_batch`` every sync or dispatch silently drops.
    The in-memory default is lost when the process exits, which a CLI does
    every invocation — so we persist it next to the crypto.db."""

    def __init__(self, path):
        self._path = Path(path)
        self._nb = self._path.read_text().strip() if self._path.exists() else None

    async def get_next_batch(self):
        return self._nb or None

    async def put_next_batch(self, next_batch):
        self._nb = next_batch
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(next_batch or "")


async def make_client(creds, *, sync_cursor_path=SYNC_CURSOR):
    """Build a mautrix Client with a live OlmMachine backed by the on-disk
    crypto store. The crypto.db is created on first run and NEVER regenerated
    under the same device_id (re-using a device_id with a fresh olm store
    orphans identity keys — see MATRIX_ONBOARDING.md "Migrating an E2EE bot").

    ``sync_cursor_path`` selects which file backs the ``/sync`` pagination
    cursor. It defaults to ``SYNC_CURSOR`` (``store/next_batch``) so the
    one-shot CLI resumes where it left off. A long-running consumer (e.g.
    ``relay.py``) passes its OWN path so its cursor is independent of this
    debug CLI — otherwise running ``mx.py tail`` between daemon restarts
    advances the shared cursor and the daemon silently misses events.
    Both still share ``crypto.db`` (the OlmMachine identity), which is fine:
    the cursor is only a /sync bookmark, not crypto state."""
    STORE_DIR.mkdir(parents=True, exist_ok=True)

    api = HTTPAPI(base_url=creds["homeserver"], token=creds["access_token"])
    state_store = MemoryStateStore()
    sync_store = _FileSyncStore(sync_cursor_path)

    client = Client(
        mxid=UserID(creds["user_id"]),
        device_id=creds["device_id"],
        api=api,
        state_store=state_store,
        sync_store=sync_store,
    )

    db = Database.create(
        f"sqlite:///{CRYPTO_DB}", upgrade_table=PgCryptoStore.upgrade_table
    )
    await db.start()
    crypto_store = PgCryptoStore(
        account_id=creds["user_id"],
        # pickle_key is deterministically {mxid}:{device_id} (see MATRIX_ONBOARDING).
        pickle_key=f"{creds['user_id']}:{creds['device_id']}",
        db=db,
    )
    await crypto_store.open()

    state = _CryptoStateStore(state_store)
    olm = OlmMachine(client, crypto_store, state)
    # Bots can't expect peer devices to be cross-signed; relax both directions
    # or megolm keys are withheld (MATRIX_ONBOARDING.md "Trust relaxation").
    olm.share_keys_min_trust = TrustState.UNVERIFIED
    olm.send_keys_min_trust = TrustState.UNVERIFIED
    await olm.load()

    client.crypto = olm
    client.crypto_store = crypto_store
    client._mx_db = db
    client._mx_state = state
    return client


async def sync_once(client, *, full_state=False, timeout=5000):
    """One /sync cycle: persist next_batch, refresh joined-room tracking, and
    await every handle_sync task (decrypt + dispatch)."""
    since = None if full_state else await client.sync_store.get_next_batch()
    data = await client.sync(since=since, timeout=timeout, full_state=full_state)
    if not isinstance(data, dict):
        return
    nb = data.get("next_batch")
    if nb:
        await client.sync_store.put_next_batch(nb)
    client._mx_state._joined.clear()
    client._mx_state._joined.update(
        data.get("rooms", {}).get("join", {}).keys()
    )
    tasks = client.handle_sync(data)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def bootstrap(client):
    """One-time-per-device E2EE bootstrap. Idempotent.

    Returns True if cross-signing was just generated this call (i.e. first
    run), False if the account already had cross-signing keys. Uses the
    recovery-key path which does generate-seeds -> upload-to-SSSS ->
    publish-publics -> sign-own-device in one call (MATRIX_ONBOARDING.md
    "Cross-signing auto-path via olm.generate_recovery_key()")."""
    olm = client.crypto
    # Upload our olm identity + one-time keys if the server has none yet.
    await olm.share_keys()
    if await olm.get_own_cross_signing_public_keys() is not None:
        # The ACCOUNT is already cross-signed, but THIS device may not be —
        # which is the case for any re-minted device (see
        # bootstrap_creds_from_password). generate_recovery_key() would be a
        # no-op here, so the new device stays unsigned and Element shows
        # "Encrypted by a device not verified by its owner" forever.
        # verify_with_recovery_key() unlocks SSSS with the account's existing
        # recovery key and signs this device with the existing SSK.
        # MATRIX_ONBOARDING.md: "recovery-key path on next start with
        # MATRIX_RECOVERY_KEY set will recover."
        await _verify_this_device(olm)
        return False
    recovery_key = await olm.generate_recovery_key()
    RECOVERY_PATH.write_text(recovery_key + "\n")
    os.chmod(RECOVERY_PATH, stat.S_IRUSR | stat.S_IWUSR)
    return True


async def _verify_this_device(olm) -> bool:
    """Self-verify this device against the account's existing cross-signing.

    No-op once the marker exists, so it runs once per device rather than every
    start. Absent a recovery key we say so loudly rather than leaving the
    operator to discover the yellow shield in Element."""
    if VERIFIED_MARKER.exists():
        return False
    recovery_key = os.environ.get("MATRIX_RECOVERY_KEY") or (
        RECOVERY_PATH.read_text().strip() if RECOVERY_PATH.exists() else ""
    )
    if not recovery_key:
        print(
            "WARNING: account is cross-signed but this device is not, and no "
            "MATRIX_RECOVERY_KEY (or store/recovery_key.txt) is available to "
            "sign it. Element will show 'Encrypted by a device not verified by "
            "its owner' for every message this device sends.",
            file=sys.stderr, flush=True,
        )
        return False
    await olm.verify_with_recovery_key(recovery_key)
    VERIFIED_MARKER.write_text(str(int(time.time())))
    print("cross-signed this device via recovery key", flush=True)
    return True


async def ensure_ready(client, room):
    """Bootstrap + initial sync, and a wake message only if one is needed.

    Element withholds megolm keys from an untrusted new device until it speaks
    in the room, so an un-cross-signed device must post once or it syncs fine
    and decrypts nothing. A CROSS-SIGNED device is trusted without speaking, so
    the wake is skipped — it is a visible line in a human's room, not a
    free-standing diagnostic, and posting it unnecessarily is just litter.
    Marker-guarded either way, so at most one per device."""
    just_bootstrapped = await bootstrap(client)
    await sync_once(client, full_state=True, timeout=5000)
    # A cross-signed device does NOT need to speak first: MATRIX_ONBOARDING.md
    # "Cross-signing with the recovery-key path lets Element trust-without-speak."
    # The wake message is a visible line in a human's room, so send it only when
    # it is actually load-bearing — i.e. this device could not be cross-signed.
    if VERIFIED_MARKER.exists() or just_bootstrapped:
        WOKE_MARKER.write_text(str(int(time.time())))
        return
    if not WOKE_MARKER.exists():
        await client.send_message_event(
            room,
            EventType.ROOM_MESSAGE,
            # NOTICE, not TEXT: clients de-emphasise notices and bots are
            # expected to use them, so the fallback path is as quiet as it can
            # be while still being a real message.
            TextMessageEventContent(msgtype=MessageType.NOTICE, body=WAKE_TEXT),
        )
        WOKE_MARKER.write_text(str(int(time.time())))
        # Drain the wake's to-device fanout (room key shares back to us) so the
        # immediately-following send/tail sees a consistent crypto state.
        await sync_once(client, timeout=2000)


async def _shutdown(client):
    """Tear down network + crypto store without orphan-task races.

    OlmMachine spawns background tasks (device-key fetches, key sharing) that
    aren't part of handle_sync's returned list. Closing the aiohttp session,
    cancelling anything still pending, then stopping the DB avoids the
    "database pool has been stopped" traceback a naïve db.stop() leaves."""
    try:
        await client.api.session.close()
    except Exception:
        pass
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    try:
        await client._mx_db.stop()
    except Exception:
        pass


async def cmd_send(args, creds, room):
    client = await make_client(creds)
    try:
        await ensure_ready(client, room)
        event_id = await client.send_message_event(
            room,
            EventType.ROOM_MESSAGE,
            TextMessageEventContent(msgtype=MessageType.TEXT, body=args.text),
        )
        # Advance the cursor past our own send so the next tail sees a stable
        # window and any to-device room-key shares are drained.
        await sync_once(client, timeout=1000)
        print(f"sent event_id={event_id} room={room}")
    finally:
        await _shutdown(client)


async def cmd_tail(args, creds, room):
    client = await make_client(creds)
    n = args.n
    try:
        await ensure_ready(client, room)
        # Backpaginate the most recent timeline. /messages may include state
        # events + membership noise; we filter to m.room.message after decrypt.
        data = await client.api.request(
            "GET",
            f"/_matrix/client/v3/rooms/{room}/messages",
            query_params={"dir": "b", "limit": str(max(n * 4, 20))},
        )
        rows = []
        for raw in reversed(data.get("chunk", [])):  # oldest-first
            try:
                evt = Event.deserialize(raw)
            except Exception:
                continue
            # mautrix exposes the ms-epoch as evt.timestamp (not origin_server_ts).
            ts = getattr(evt, "timestamp", 0) or 0
            sender = str(getattr(evt, "sender", "?"))
            if evt.type == EventType.ROOM_ENCRYPTED and isinstance(
                evt, EncryptedEvent
            ):
                try:
                    evt = await client.crypto.decrypt_megolm_event(evt)
                except Exception as exc:
                    rows.append(
                        (ts, sender, f"[undecryptable: {type(exc).__name__}]")
                    )
                    continue
            if evt.type != EventType.ROOM_MESSAGE:
                continue
            body = getattr(evt.content, "body", "") or ""
            rows.append((ts, sender, body))
        for ts, sender, body in rows[-n:]:
            when = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000))
                if ts
                else "?"
            )
            print(f"{when}  {sender}\n    {body}")
    finally:
        await _shutdown(client)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mx.py",
        description="shape-bridge E2EE Matrix CLI (send / tail the bridge test room)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="send one plain-text message to the test room")
    s.add_argument("text", help="message body")
    t = sub.add_parser("tail", help="print the last N decrypted messages")
    t.add_argument("n", nargs="?", type=int, default=10, help="message count")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "send":
            assert_test_venue()
        creds, room = load_config()
    except ConfigError as exc:
        print(f"mx.py: config error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.cmd == "send":
            asyncio.run(cmd_send(args, creds, room))
        else:
            asyncio.run(cmd_tail(args, creds, room))
    except Exception as exc:
        print(f"mx.py: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
