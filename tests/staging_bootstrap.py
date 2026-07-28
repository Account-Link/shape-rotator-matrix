#!/usr/bin/env python3
"""Phase-2 bootstrap for the ephemeral staging CVM (issue #14).

`tests/smoke.py` assumes the homeserver already has the prod space + child rooms
and an admin who can kick test users. A fresh ephemeral CVM has an empty RocksDB,
so before smoke can run we:

  1. wait for the homeserver client API to answer,
  2. register an admin account (native /register with the throwaway registration
     token),
  3. create the space + the three child rooms smoke.py expects,
  4. append the admin token / space ids / allowlist back into the staging env
     file so the subsequent `phala deploy --cvm-id ... -e <env>` redeploy brings
     knock-approver up under that admin identity.

Run inside .github/workflows/staging-validate.yml:

    HOMESERVER=http://<appId>-80.dstack-pha-prod9.phala.network \\
        python3 tests/staging_bootstrap.py .staging.env

Exit 0 on success, nonzero on any step. Uses an unverified TLS context because
the raw phala endpoint's certificate is phala-issued and not necessarily in the
runner's CA bundle (see the workflow's "resolve staging endpoint" step + the PR's
"could not verify" section).
"""
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request

CHILD_ROOM_NAMES = ("general", "announcements", "bot-noise")
CTX = ssl._create_unverified_context()


def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                env[k] = v
    return env


def call(hs, method, path, token=None, body=None, tries=30, sleep=5):
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for a in range(tries):
        req = urllib.request.Request(
            hs + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            # A 4xx/5xx is a real failure, not a "not up yet" — surface it.
            raw = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {raw}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            print(f"  [{a + 1}/{tries}] {method} {path}: {e} (retry in {sleep}s)",
                  flush=True)
            time.sleep(sleep)
    raise SystemExit(f"endpoint never came up: {hs} (last error: {last})")


def main():
    if len(sys.argv) < 2:
        print("usage: staging_bootstrap.py <env-file>", file=sys.stderr)
        return 2
    env_path = sys.argv[1]
    hs = os.environ["HOMESERVER"].rstrip("/")
    env = load_env(env_path)

    reg_token = env.get("REGISTRATION_TOKEN", "")
    admin_pw = env.get("BOOTSTRAP_PASSWORD") or secrets.token_urlsafe(24)
    if not reg_token:
        raise SystemExit("REGISTRATION_TOKEN missing from env file")

    print(f"bootstrap: waiting for client API at {hs}", flush=True)
    versions = call(hs, "GET", "/_matrix/client/versions", tries=40)
    print(f"  /versions ok: {sorted(versions.get('versions', []))[-1:]}",
          flush=True)

    # Native /register with the registration token (same path smoke.py uses).
    init = call(hs, "POST", "/_matrix/client/v3/register", body={})
    admin = call(hs, "POST", "/_matrix/client/v3/register", body={
        "auth": {
            "type": "m.login.registration_token",
            "token": reg_token,
            "session": init["session"],
        },
        "username": "staging_admin_" + secrets.token_hex(4),
        "password": admin_pw,
    })
    token, mxid = admin["access_token"], admin["user_id"]
    print(f"  admin registered: {mxid}", flush=True)

    space = call(hs, "POST", "/_matrix/client/v3/createRoom", token=token, body={
        "name": "staging validation",
        "creation_content": {"type": "m.space"},
        "preset": "private_chat",
    })["room_id"]
    print(f"  space created: {space}", flush=True)

    children = []
    for name in CHILD_ROOM_NAMES:
        rid = call(hs, "POST", "/_matrix/client/v3/createRoom", token=token, body={
            "name": name, "preset": "private_chat",
        })["room_id"]
        children.append(rid)
    print(f"  children created: {children}", flush=True)

    # Fold the bootstrapped identity back into the env file for the phase-2
    # redeploy. knock-approver uses MATRIX_TOKEN == the admin token, so the same
    # account that owns the space (PL 100) also does the inviting/joining.
    append = [
        f"KNOCK_APPROVER_TOKEN={token}",
        f"ONBOARDING_BOT_TOKEN={token}",
        f"ADMIN_ALLOWLIST={mxid}",
        f"ONBOARDING_INVITER_MXID={mxid}",
        f"SHAPEROTATOR_SPACE_ID={space}",
        f"SPACE_CHILD_IDS={','.join(children)}",
        f"ADMIN_COMMAND_ROOM={space}",
    ]
    with open(env_path, "a") as f:
        for line in append:
            f.write(line + "\n")
    print("bootstrap: env file updated for phase-2 redeploy", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
