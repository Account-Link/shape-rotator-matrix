"""End-to-end Paste A+B+C test against prod.

Runs the full chain: /signup/api → /signup/api/crosssign → launch prod
responder.py → initiate SAS from a second mautrix client → confirm bot's
device flips to TrustState.VERIFIED.

This is the programmatic equivalent of the Element user-facing demo: it
proves that someone clicking "Verify" on the bot in Element would complete
the SAS dance.

Env:
  HOMESERVER    default https://mtrx.shaperotator.xyz
  SIGNUP_CODE   required — a code with >= 2 uses (one for bot, one for verifier)
  ADMIN_TOKEN   optional — used to kick the test users at the end

Run:
  SIGNUP_CODE=... ADMIN_TOKEN=... python3 tests/sas_prod.py
"""
import asyncio, base64, hashlib, json, os, secrets, subprocess, sys, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "landing"))

from sas_e2e import _SASInitiator, make_client, _sync_loop

from mautrix.types import EventType, TrustState, DeviceIdentity

HS = os.environ.get("HOMESERVER", "https://mtrx.shaperotator.xyz").rstrip("/")
CODE = os.environ["SIGNUP_CODE"]
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def http(method, url, body=None, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def signup(prefix):
    username = f"{prefix}_{int(time.time())}_{secrets.token_hex(2)}"
    password = secrets.token_urlsafe(24)
    st, r = http("POST", f"{HS}/signup/api", {
        "code": CODE, "username": username, "password": password,
        "display_name": prefix,
        "intro": "prod SAS test — kick me",
    })
    assert st == 200, f"signup failed: {st} {r}"
    return r


async def main():
    # 1. Create bot + verifier accounts via signup.
    bot = signup("bot")
    ver = signup("ver")
    print(f"bot={bot['user_id']} device={bot['device_id']}", flush=True)
    print(f"ver={ver['user_id']} device={ver['device_id']}", flush=True)

    # 2. Launch prod responder.py for the bot. Use the files served from prod.
    bot_dir = Path(f"/tmp/sas_prod_{int(time.time())}")
    bot_dir.mkdir()
    for f in ("responder.py", "sas_verification.py"):
        resp = urllib.request.urlopen(f"{HS}/{f}", timeout=10).read()
        (bot_dir / f).write_bytes(resp)
    env = {**os.environ,
           "HS": HS,
           "MXID": bot["user_id"],
           "TOKEN": bot["access_token"],
           "DEVICE": bot["device_id"],
           "STORE": str(bot_dir / "bot.db")}
    log = bot_dir / "responder.log"
    proc = subprocess.Popen(
        [sys.executable, str(bot_dir / "responder.py")],
        cwd=str(bot_dir), env=env,
        stdout=open(log, "w"), stderr=subprocess.STDOUT,
        start_new_session=True)
    print(f"bot responder pid={proc.pid} log={log}", flush=True)
    await asyncio.sleep(10)
    assert proc.poll() is None, f"bot crashed:\n{log.read_text()}"

    # 3. Paste B: cross-signing bootstrap for the bot (so Element would stop
    #    showing the yellow shield). Not required for SAS itself but proves the
    #    full A+B+C chain works against prod.
    st, cs_resp = http("POST", f"{HS}/signup/api/crosssign",
                        {"access_token": bot["access_token"]})
    assert st == 200, f"crosssign failed: {st} {cs_resp}"
    assert cs_resp.get("device_signed"), f"crosssign didn't sign device: {cs_resp}"
    print(f"crosssign ok msk={cs_resp['msk_public'][:16]}... signed=True", flush=True)

    # 4. Stand up the verifier as a second mautrix client.
    v_client, v_cs, v_ss, v_db = await make_client(
        ver["user_id"], ver["access_token"], ver["device_id"], bot_dir / "ver.db")
    await v_client.crypto.share_keys()

    q = await v_client.api.request(
        "POST", "/_matrix/client/v3/keys/query",
        content={"device_keys": {bot["user_id"]: [bot["device_id"]]}})
    dinfo = q["device_keys"][bot["user_id"]][bot["device_id"]]
    ed25519_key = next(v for k, v in dinfo["keys"].items() if k.startswith("ed25519:"))
    curve25519_key = next(v for k, v in dinfo["keys"].items() if k.startswith("curve25519:"))
    bot_device = DeviceIdentity(
        user_id=bot["user_id"], device_id=bot["device_id"],
        identity_key=curve25519_key, signing_key=ed25519_key,
        trust=TrustState.UNVERIFIED, deleted=False, name="")
    await v_cs.put_devices(bot["user_id"], {bot["device_id"]: bot_device})

    # 5. Initiate SAS from verifier; wait for completion.
    sync_task = asyncio.create_task(_sync_loop(v_client, v_ss))
    init = _SASInitiator(
        v_client, v_cs, ver["user_id"], ver["device_id"],
        bot["user_id"], bot["device_id"], ed25519_key)
    await init.start()
    print(f"verifier sent .start tx={init.tx}", flush=True)

    try:
        await asyncio.wait_for(init.done.wait(), timeout=30)
    except asyncio.TimeoutError:
        print("TIMEOUT — verification didn't complete", flush=True)

    dev = await v_cs.get_device(bot["user_id"], bot["device_id"])
    trust = dev.trust if dev else None

    # 6. Teardown
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

    # Kick test users if we have admin
    if ADMIN_TOKEN:
        for mxid in (bot["user_id"], ver["user_id"]):
            for rid in (os.environ.get("SPACE_ID", "!4FL8uL5OEYLATG1VH4wC2CD3pfIV6BMFId9VT7rmm-g"),):
                try:
                    http("POST",
                         f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(rid)}/kick",
                         body={"user_id": mxid, "reason": "sas test cleanup"},
                         token=ADMIN_TOKEN)
                except Exception:
                    pass

    ok = init.success and trust == TrustState.VERIFIED
    print(f"\nresult: success={init.success} trust={trust} cancel_reason={init.cancel_reason}", flush=True)
    print(f"{'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
