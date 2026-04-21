"""End-to-end SAS verification test for landing/responder.py + sas_verification.py.

Runs against the dev stack: dev/docker-compose.yml + a dev-token that bootstrap.py
has unlocked. Starts two mautrix clients:

  - BOT launches landing/responder.py (responder-side SAS, auto-accept)
  - VERIFIER plays the initiator role via _SASInitiator below

Success: verifier's crypto store marks bot's device TrustState.VERIFIED and the
bot sends m.key.verification.done.

Run:
  cd dev && docker compose up -d && python3 bootstrap.py
  cd .. && python3 tests/sas_e2e.py
"""
import asyncio, base64, hashlib, json, os, secrets, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "landing"))

from mautrix.api import HTTPAPI
from mautrix.client import Client
from mautrix.client.state_store import MemoryStateStore, MemorySyncStore
from mautrix.types import (UserID, EventType, TrustState, DeviceIdentity)
from mautrix.crypto import OlmMachine
from mautrix.crypto.store.asyncpg import PgCryptoStore
from mautrix.util.async_db import Database

import olm

HS = os.environ.get("DEV_HS", "http://localhost:46167").rstrip("/")
REG_TOKEN = os.environ.get("DEV_REG_TOKEN", "dev-token")


def _post(url, body, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def register(username, password, device_id):
    s, init = _post(f"{HS}/_matrix/client/v3/register", {})
    session = init["session"]
    s, r = _post(f"{HS}/_matrix/client/v3/register", {
        "auth": {"type": "m.login.registration_token",
                 "token": REG_TOKEN, "session": session},
        "username": username, "password": password, "device_id": device_id,
    })
    assert s == 200, f"register failed: {s} {r}"
    return r["user_id"], r["access_token"]


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


async def make_client(user_id, token, device_id, db_path):
    api = HTTPAPI(base_url=HS, token=token)
    state, sync = MemoryStateStore(), MemorySyncStore()
    client = Client(mxid=UserID(user_id), device_id=device_id, api=api,
                    state_store=state, sync_store=sync)
    db = Database.create(f"sqlite:///{db_path}",
                         upgrade_table=PgCryptoStore.upgrade_table)
    await db.start()
    cs = PgCryptoStore(account_id=user_id, pickle_key=f"{user_id}:{device_id}", db=db)
    await cs.open()
    ss = _StateStore(state)
    olm_m = OlmMachine(client, cs, ss)
    olm_m.share_keys_min_trust = TrustState.UNVERIFIED
    olm_m.send_keys_min_trust  = TrustState.UNVERIFIED
    await olm_m.load()
    client.crypto = olm_m
    return client, cs, ss, db


async def sync_once(client, ss, timeout=3000, first=False):
    since = None if first else await client.sync_store.get_next_batch()
    data = await client.sync(since=since, timeout=timeout, full_state=first)
    if not isinstance(data, dict):
        return
    nb = data.get("next_batch")
    if nb:
        await client.sync_store.put_next_batch(nb)
    ss._joined.clear()
    ss._joined.update(data.get("rooms", {}).get("join", {}).keys())
    tasks = client.handle_sync(data)
    if tasks:
        await asyncio.gather(*tasks)


def _b64(b): return base64.b64encode(b).decode()
def _canon(obj): return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


class _SASInitiator:
    """Initiator-side SAS state machine. Auto-confirms (what Element does when
    the human clicks 'they match'). Matches the receiver-side convention in
    landing/sas_verification.py."""
    def __init__(self, client, crypto_store, our_user, our_device,
                 their_user, their_device, their_signing_key):
        self.client = client
        self.store = crypto_store
        self.our_user = our_user
        self.our_device = our_device
        self.their_user = their_user
        self.their_device = their_device
        self.their_signing_key = their_signing_key
        self.tx = "t-" + secrets.token_hex(6)
        self.sas = olm.Sas()
        self.their_commitment = None
        self.done = asyncio.Event()
        self.success = False
        self.cancel_reason = None

        for ev_type, handler in [
            ("m.key.verification.accept", self._on_accept),
            ("m.key.verification.key",    self._on_key),
            ("m.key.verification.mac",    self._on_mac),
            ("m.key.verification.cancel", self._on_cancel),
        ]:
            client.add_event_handler(
                EventType.find(ev_type, EventType.Class.TO_DEVICE), handler)

    @staticmethod
    def _as_dict(ev):
        c = ev.content if hasattr(ev, "content") else ev
        return c.serialize() if hasattr(c, "serialize") else c

    async def _send(self, ev_type, content):
        await self.client.send_to_device(
            EventType.find(ev_type, EventType.Class.TO_DEVICE),
            {self.their_user: {self.their_device: content}})

    async def start(self):
        self._start_content = {
            "from_device": self.our_device,
            "method": "m.sas.v1",
            "transaction_id": self.tx,
            "key_agreement_protocols": ["curve25519-hkdf-sha256"],
            "hashes": ["sha256"],
            "message_authentication_codes": ["hkdf-hmac-sha256.v2"],
            "short_authentication_string": ["emoji", "decimal"],
        }
        await self._send("m.key.verification.start", self._start_content)

    async def _on_accept(self, ev):
        c = self._as_dict(ev)
        if c.get("transaction_id") != self.tx:
            return
        self.their_commitment = c.get("commitment")
        await self._send("m.key.verification.key", {
            "transaction_id": self.tx,
            "key": self.sas.pubkey,
        })

    async def _on_key(self, ev):
        c = self._as_dict(ev)
        if c.get("transaction_id") != self.tx:
            return
        their_key = c.get("key", "")
        start_copy = {k: v for k, v in self._start_content.items() if k != "transaction_id"}
        expected = _b64(hashlib.sha256(their_key.encode() + _canon(start_copy)).digest())
        if expected != self.their_commitment:
            await self._cancel("m.key_mismatch", "commitment mismatch")
            return
        self.sas.set_their_pubkey(their_key)
        def _info(key_id):
            return ("MATRIX_KEY_VERIFICATION_MAC"
                    + self.our_user + self.our_device
                    + self.their_user + self.their_device + self.tx + key_id)
        our_signing = self.client.crypto.account.signing_key
        key_id = f"ed25519:{self.our_device}"
        key_mac = self.sas.calculate_mac(our_signing, _info(key_id))
        keys_mac = self.sas.calculate_mac(key_id, _info("KEY_IDS"))
        await self._send("m.key.verification.mac", {
            "transaction_id": self.tx,
            "mac": {key_id: key_mac},
            "keys": keys_mac,
        })

    async def _on_mac(self, ev):
        c = self._as_dict(ev)
        if c.get("transaction_id") != self.tx:
            return
        def _info(key_id):
            return ("MATRIX_KEY_VERIFICATION_MAC"
                    + self.their_user + self.their_device
                    + self.our_user + self.our_device + self.tx + key_id)
        key_id = f"ed25519:{self.their_device}"
        expected_key_mac = self.sas.calculate_mac(self.their_signing_key, _info(key_id))
        expected_keys_mac = self.sas.calculate_mac(key_id, _info("KEY_IDS"))
        mac = c.get("mac", {})
        if mac.get(key_id) != expected_key_mac:
            await self._cancel("m.key_mismatch", "peer mac mismatch")
            return
        if c.get("keys", "") != expected_keys_mac:
            await self._cancel("m.key_mismatch", "peer keys mac mismatch")
            return
        dev = await self.store.get_device(self.their_user, self.their_device)
        if dev:
            dev.trust = TrustState.VERIFIED
            await self.store.put_devices(self.their_user, {self.their_device: dev})
        await self._send("m.key.verification.done", {"transaction_id": self.tx})
        self.success = True
        self.done.set()

    async def _on_cancel(self, ev):
        c = self._as_dict(ev)
        if c.get("transaction_id") != self.tx:
            return
        self.cancel_reason = c.get("reason") or c.get("code")
        self.done.set()

    async def _cancel(self, code, reason):
        await self._send("m.key.verification.cancel", {
            "transaction_id": self.tx,
            "code": code, "reason": reason,
        })
        self.cancel_reason = reason
        self.done.set()


async def _sync_loop(client, ss):
    while True:
        try:
            await sync_once(client, ss, timeout=10000)
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"sync: {e}", flush=True)
            await asyncio.sleep(1)


async def main():
    ts = int(time.time())
    bot_u, bot_tok = register(f"bot_{ts}", "bot-password-long", "BOT")
    ver_u, ver_tok = register(f"ver_{ts}", "ver-password-long", "VER")
    print(f"bot={bot_u} verifier={ver_u}", flush=True)

    bot_dir = Path(f"/tmp/sas_e2e_{ts}")
    bot_dir.mkdir()
    for f in ("responder.py", "sas_verification.py"):
        (bot_dir / f).write_bytes((REPO / "landing" / f).read_bytes())
    env = {**os.environ,
           "HS": HS, "MXID": bot_u, "TOKEN": bot_tok, "DEVICE": "BOT",
           "STORE": str(bot_dir / "bot.db")}
    log = bot_dir / "responder.log"
    proc = subprocess.Popen(
        [sys.executable, str(bot_dir / "responder.py")],
        cwd=str(bot_dir), env=env,
        stdout=open(log, "w"), stderr=subprocess.STDOUT,
        start_new_session=True)
    print(f"bot pid={proc.pid} log={log}", flush=True)
    await asyncio.sleep(6)
    assert proc.poll() is None, f"BOT CRASHED:\n{log.read_text()}"

    v_client, v_cs, v_ss, v_db = await make_client(
        ver_u, ver_tok, "VER", bot_dir / "ver.db")
    await v_client.crypto.share_keys()

    q = await v_client.api.request(
        "POST", "/_matrix/client/v3/keys/query",
        content={"device_keys": {bot_u: ["BOT"]}})
    dinfo = q["device_keys"][bot_u]["BOT"]
    ed25519_key = next(v for k, v in dinfo["keys"].items() if k.startswith("ed25519:"))
    curve25519_key = next(v for k, v in dinfo["keys"].items() if k.startswith("curve25519:"))
    bot_device = DeviceIdentity(
        user_id=bot_u, device_id="BOT",
        identity_key=curve25519_key, signing_key=ed25519_key,
        trust=TrustState.UNVERIFIED, deleted=False, name="")
    await v_cs.put_devices(bot_u, {"BOT": bot_device})

    sync_task = asyncio.create_task(_sync_loop(v_client, v_ss))
    init = _SASInitiator(v_client, v_cs, ver_u, "VER", bot_u, "BOT", ed25519_key)
    await init.start()
    print(f"verifier sent .start tx={init.tx}", flush=True)

    try:
        await asyncio.wait_for(init.done.wait(), timeout=25)
    except asyncio.TimeoutError:
        print("TIMEOUT — no .done or .cancel", flush=True)

    dev = await v_cs.get_device(bot_u, "BOT")
    trust = dev.trust if dev else None

    sync_task.cancel()
    try: await asyncio.wait_for(asyncio.shield(sync_task), timeout=2)
    except Exception: pass
    proc.terminate()
    try: proc.wait(timeout=3)
    except Exception: proc.kill()
    try: await v_client.api.session.close()
    except Exception: pass
    try: await v_db.stop()
    except Exception: pass

    ok = init.success and trust == TrustState.VERIFIED
    print(f"result: success={init.success} trust={trust} cancel_reason={init.cancel_reason}",
          flush=True)
    print(f"{'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
