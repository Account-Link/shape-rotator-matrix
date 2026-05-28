"""Mint a new device "ci-deploy" under @shape-rotator-2 for GH Actions.

Runs inside dstack-knock-approver-1 where the password + master keys live.
Logs in with device_id="ci-deploy", uploads device + one-time keys via
mautrix OlmMachine (sqlite-backed), self-signs the new device with the
existing SSK from cross_signing.json so it inherits master trust, then
tars (sqlite store + meta) and prints base64 of the bundle to stdout.

Stash output as GH secret CI_DEPLOY_BOT_BUNDLE_B64.
"""
import asyncio, base64, io, json, os, sys, tarfile, tempfile
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import ed25519
import aiohttp


def _canon(obj) -> bytes:
    """Matrix canonical JSON: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=False).encode("utf-8")

from mautrix.api import HTTPAPI
from mautrix.client import Client
from mautrix.client.state_store import MemoryStateStore
from mautrix.crypto import OlmMachine
from mautrix.crypto.store.asyncpg import PgCryptoStore
from mautrix.util.async_db import Database
from mautrix.types import UserID, TrustState

HS = os.environ.get("HOMESERVER", "https://mtrx.shaperotator.xyz")
MXID = "@shape-rotator-2:mtrx.shaperotator.xyz"
DEVICE_ID = "ci-deploy"
PASSWORD_PATH = Path("/data/shape_rotator_2_password")
CROSS_SIGNING_PATH = Path("/data/shape_rotator_2_cross_signing.json")


def _b64(b): return base64.b64encode(b).decode().rstrip("=")


def _sign_object(obj, signer_priv: bytes, user_id: str, key_id: str) -> dict:
    """Matrix canonical-json signing. Strips signatures/unsigned, attaches sig
    under signatures[user_id][ed25519:key_id]."""
    signer = ed25519.Ed25519PrivateKey.from_private_bytes(signer_priv)
    to_sign = {k: v for k, v in obj.items() if k not in ("signatures", "unsigned")}
    sig = _b64(signer.sign(_canon(to_sign)))
    sigs = dict(obj.get("signatures", {}))
    user_sigs = dict(sigs.get(user_id, {}))
    user_sigs[f"ed25519:{key_id}"] = sig
    sigs[user_id] = user_sigs
    obj["signatures"] = sigs
    return obj


async def main():
    workdir = Path(tempfile.mkdtemp(prefix="ci-bootstrap-"))
    crypto_db = workdir / "ci-store.db"

    pw = PASSWORD_PATH.read_text().strip()
    cs_json = json.loads(CROSS_SIGNING_PATH.read_text())
    ssk_priv = base64.b64decode(cs_json["self_signing_private"] + "==")
    ssk_pub = cs_json["ssk_public"]

    # 1. Login as @shape-rotator-2 with explicit device_id="ci-deploy".
    api = HTTPAPI(base_url=HS)
    client = Client(mxid=UserID(MXID), base_url=HS, device_id=DEVICE_ID,
                    state_store=MemoryStateStore())
    resp = await client.login(password=pw, device_id=DEVICE_ID,
                              device_name="GH Actions ci-deploy",
                              store_access_token=True)
    access_token = resp.access_token
    print(f"[bootstrap] login ok: device={resp.device_id} token=…{access_token[-6:]}",
          file=sys.stderr)

    # 2. Init sqlite-backed crypto store + OlmMachine (mirrors approver.py:1486).
    db = Database.create(f"sqlite:///{crypto_db}",
                         upgrade_table=PgCryptoStore.upgrade_table)
    await db.start()
    cs_store = PgCryptoStore(account_id=MXID,
                             pickle_key=f"{MXID}:{DEVICE_ID}", db=db)
    await cs_store.open()
    olm = OlmMachine(client, cs_store, MemoryStateStore())
    olm.share_keys_min_trust = TrustState.UNVERIFIED
    olm.send_keys_min_trust = TrustState.UNVERIFIED
    await olm.load()
    client.crypto = olm
    client.crypto_store = cs_store

    # 3. Upload device keys + one-time keys.
    await olm.share_keys()
    print("[bootstrap] device + one-time keys uploaded", file=sys.stderr)

    # 4. Fetch our new device's signed key from the server, sign with SSK,
    # upload via /keys/signatures/upload. (Borrowed from approver.py:1900.)
    headers = {"Authorization": f"Bearer {access_token}",
               "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        device_obj = None
        for _ in range(8):
            async with session.post(f"{HS}/_matrix/client/v3/keys/query",
                                    json={"device_keys": {MXID: [DEVICE_ID]}},
                                    headers=headers) as r:
                q = await r.json()
                device_obj = (q.get("device_keys", {})
                                .get(MXID, {}).get(DEVICE_ID))
                if device_obj:
                    break
            await asyncio.sleep(1.0)
        if not device_obj:
            raise RuntimeError("server never returned our ci-deploy device keys")

        signed = _sign_object(device_obj, ssk_priv, MXID, ssk_pub)
        async with session.post(f"{HS}/_matrix/client/v3/keys/signatures/upload",
                                json={MXID: {DEVICE_ID: signed}},
                                headers=headers) as r:
            body = await r.json()
            if r.status != 200 or body.get("failures"):
                raise RuntimeError(f"signatures/upload {r.status}: {body}")
    print(f"[bootstrap] cross-signed ci-deploy with SSK={ssk_pub[:8]}…",
          file=sys.stderr)

    # 5. Close cleanly (flush sqlite).
    await cs_store.close()
    await db.stop()
    await client.api.session.close() if client.api.session else None

    # 6. Bundle = ci-store.db + meta.json, tar.gz, base64 → stdout.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(crypto_db, arcname="ci-store.db")
        meta = json.dumps({"access_token": access_token,
                           "device_id": DEVICE_ID, "mxid": MXID,
                           "homeserver": HS,
                           "pickle_key": f"{MXID}:{DEVICE_ID}"}).encode()
        info = tarfile.TarInfo("meta.json"); info.size = len(meta)
        tar.addfile(info, io.BytesIO(meta))
    sys.stdout.write(base64.b64encode(buf.getvalue()).decode())
    sys.stdout.flush()
    print(f"\n[bootstrap] bundle size: {buf.tell()} bytes", file=sys.stderr)


asyncio.run(main())
