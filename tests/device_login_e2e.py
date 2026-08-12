#!/usr/bin/env python3
"""End-to-end test for bot-mediated device login (issue #46).

Boots the locally-built patched continuwuity, registers a user, then drives the
approver's actual relay functions: start -> (simulated DM from the user) ->
mint -> poll -> redeem the m.login.token and confirm whoami. Run after building
the patched binary in ../continuwuity-fork.
"""
import asyncio, json, os, subprocess, sys, tempfile, time, urllib.request, shutil
from pathlib import Path

PORT = 6178
BASE = f"http://127.0.0.1:{PORT}"
SECRET = "testsecret123"
REG = "testreg"
FORK = Path(__file__).resolve().parents[3] / "continuwuity-fork"
BIN = next((p for p in [FORK / "target/debug/conduwuit", FORK / "target/release/conduwuit"]
            if p.exists()), None)

# approver.py needs these in the env at import time.
os.environ.update(HS=BASE, MATRIX_TOKEN="", MINT_TOKEN_SECRET=SECRET,
                  DEVICE_LOGIN_TTL="300", SPACE_ID="!dummy:localhost",
                  LOG_PATH=tempfile.mktemp(prefix="audit-", suffix=".jsonl"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "knock-approver"))


def post(path, body, token=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, None


def get(path, token=None):
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as r:
        return r.status, json.load(r)


async def main():
    assert BIN, "no built conduwuit binary in ../continuwuity-fork/target"
    data_dir = tempfile.mkdtemp(prefix="cw-dl-")
    env = {**os.environ, "CONDUWUIT_SERVER_NAME": "localhost",
           "CONDUWUIT_DATABASE_BACKEND": "rocksdb", "CONDUWUIT_DATABASE_PATH": data_dir,
           "CONDUWUIT_ADDRESS": "127.0.0.1", "CONDUWUIT_PORT": str(PORT),
           "CONDUWUIT_ALLOW_REGISTRATION": "true", "CONDUWUIT_REGISTRATION_TOKEN": REG,
           "CONDUWUIT_MINT_LOGIN_TOKEN_SECRET": SECRET, "CONDUWUIT_LOG": "warn"}
    logf = open(os.path.join(data_dir, "log"), "w+")
    srv = subprocess.Popen([str(BIN)], env=env, stdout=logf, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                get("/_matrix/client/versions"); break
            except Exception:
                if srv.poll() is not None:
                    raise SystemExit("server died")
                time.sleep(1)

        # Fresh DB: grab the one-time first-account setup token from the banner.
        import re
        reg_token = None
        for _ in range(20):
            banner = re.sub(r"\x1b\[[0-9;]*m", "", open(os.path.join(data_dir, "log")).read())
            m = re.search(r"registration token ([A-Za-z0-9]+)", banner)
            if m:
                reg_token = m.group(1); break
            time.sleep(0.5)
        assert reg_token, "no first-account token in server log:\n" + banner[-2000:]

        # register @alice
        _, init = post("/_matrix/client/v3/register", {})
        _, reg = post("/_matrix/client/v3/register", {
            "username": "alice", "password": "hunter2hunter2",
            "auth": {"type": "m.login.registration_token", "token": reg_token,
                     "session": init["session"]}})
        alice = reg["user_id"]

        # import the approver's relay logic now that the server is up
        import approver
        from aiohttp.test_utils import make_mocked_request
        approver.OUR_MXID = "@bot:localhost"

        npass = nfail = 0
        def check(name, ok):
            nonlocal npass, nfail
            print(("  ok: " if ok else "  FAIL: ") + name)
            npass += ok; nfail += (not ok)

        # 1. desktop starts a login -> gets a code
        resp = await approver.device_login_start(make_mocked_request("POST", "/device-login/start"))
        code = json.loads(resp.text)["code"]
        check("start returns a code", code.startswith("SRDL-"))
        check("code is pending", approver.DEVICE_LOGINS[code]["status"] == "pending")

        # 2. poll before approval -> still pending
        resp = await approver.device_login_poll(
            make_mocked_request("GET", f"/device-login/poll?code={code}"))
        check("poll pending before DM", json.loads(resp.text)["status"] == "pending")

        # 3. the user DMs the code -> bot mints for the verified sender, then
        #    leaves the DM room (hardening: don't accumulate membership)
        left = []
        approver._leave_room = lambda rid: (left.append(rid), asyncio.sleep(0))[1]
        await approver.maybe_approve_device_login(alice, f"hi {code}", "!dm:localhost")
        check("approved after DM", approver.DEVICE_LOGINS[code]["status"] == "approved")
        check("bot leaves DM after approval", left == ["!dm:localhost"])
        lt = approver.DEVICE_LOGINS[code]["login_token"]
        check("login_token minted", bool(lt))

        # 4. desktop polls -> gets the token (once)
        resp = await approver.device_login_poll(
            make_mocked_request("GET", f"/device-login/poll?code={code}"))
        polled = json.loads(resp.text)
        check("poll returns token", polled.get("login_token") == lt)
        resp2 = await approver.device_login_poll(
            make_mocked_request("GET", f"/device-login/poll?code={code}"))
        check("token is one-time (consumed)", json.loads(resp2.text)["status"] == "consumed")

        # 5. redeem the m.login.token -> logged in as alice, no password
        st, login = post("/_matrix/client/v3/login", {"type": "m.login.token", "token": lt})
        _, who = get("/_matrix/client/v3/account/whoami", token=login["access_token"])
        check("redeemed token logs in as alice", who["user_id"] == alice)

        # 6. a stranger's code is ignored
        await approver.maybe_approve_device_login("@mallory:localhost", "SRDL-DEADBEEF", "!x:localhost")
        check("unknown code ignored", "SRDL-DEADBEEF" not in approver.DEVICE_LOGINS)

        print(f"----\nPASS={npass} FAIL={nfail}")
        sys.exit(1 if nfail else 0)
    finally:
        srv.terminate()
        shutil.rmtree(data_dir, ignore_errors=True)


asyncio.run(main())
