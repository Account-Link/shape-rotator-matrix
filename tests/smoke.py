#!/usr/bin/env python3
"""End-to-end smoke test for shape-rotator-matrix.

Runs against a configured homeserver (default: the deployed prod instance) and
verifies both onboarding paths end-to-end:

  1. POST /signup/api -> new account, all "steps" true, user is joined to
     space + all children, and the DM room to the configured inviter exists.
  2. /join?code=... flow -> knock with code as reason; approver auto-approves
     within a few seconds; new user ends up invited.

Leaves the homeserver in its original state by kicking both test users at
the end. Uses stdlib only; run with plain `python3 tests/smoke.py`.

Env:
  HOMESERVER       default https://mtrx.shaperotator.xyz
  ADMIN_TOKEN      required — used to kick test users after the test
  SIGNUP_CODE      required — a signup code with >= 1 use remaining
  KNOCK_CODE       required — a knock (invite) code with >= 1 use remaining
  REG_TOKEN        required — continuwuity server-wide registration token
                   (used only for the knock path, to create the federated
                   "guest" account that does the knocking)
  SPACE_ID         default !4FL8uL5OEYLATG1VH4wC2CD3pfIV6BMFId9VT7rmm-g
  SPACE_CHILDREN   comma-separated, default: the three prod children

Exit code 0 on all-pass, nonzero otherwise.
"""
import json, os, secrets, sys, time, urllib.error, urllib.parse, urllib.request

HS            = os.environ.get("HOMESERVER", "https://mtrx.shaperotator.xyz").rstrip("/")
ADMIN_TOKEN   = os.environ.get("ADMIN_TOKEN", "")
SIGNUP_CODE   = os.environ.get("SIGNUP_CODE", "")
KNOCK_CODE    = os.environ.get("KNOCK_CODE", "")
REG_TOKEN     = os.environ.get("REG_TOKEN", "")
SPACE_ID      = os.environ.get("SPACE_ID", "!4FL8uL5OEYLATG1VH4wC2CD3pfIV6BMFId9VT7rmm-g")
SPACE_CHILDREN = [c.strip() for c in os.environ.get(
    "SPACE_CHILDREN",
    "!z85RFatK8w0f04i8yVOCidnYRKXlZuRjK4kYkdXVhUc,"
    "!9p9ZAr8CFo8WjD8g0hKv_1sOewNWt0zTBCWMAkWnLxo,"
    "!a8L-8zCDgQZhddUWkb4FYkCVjPBu0lY6QwtLVBXIRXc"
).split(",") if c.strip()]

results = []

def log(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    results.append((name, ok))

def http(method, url, token=None, body=None, timeout=15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw.decode("utf-8", errors="replace")}

def admin_kick(mxid, rooms):
    for rid in rooms:
        http("POST",
             f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(rid)}/kick",
             token=ADMIN_TOKEN,
             body={"user_id": mxid, "reason": "smoke-test cleanup"})


# --- Path A: signup ---

def test_signup_path():
    username = f"smoke_signup_{int(time.time())}_{secrets.token_hex(2)}"
    print(f"\n[signup] username={username}", flush=True)

    status, r = http("POST", f"{HS}/signup/api", body={
        "code":         SIGNUP_CODE,
        "username":     username,
        "password":     secrets.token_urlsafe(32),
        "display_name": "smoke test",
        "intro":        "automated smoke test — please ignore",
    })
    log("signup returns 200", status == 200, f"status={status} body={r}")
    if status != 200:
        return None

    mxid  = r.get("user_id", "")
    token = r.get("access_token", "")
    steps = r.get("steps", {})
    log("register step",       steps.get("register") is True)
    log("space_invited step",  steps.get("space_invited") is True)
    log("space_joined step",   steps.get("space_joined") is True)
    log("inviter_dm step",     steps.get("inviter_dm") is True)
    children_joined = steps.get("children_joined") or []
    log(f"children_joined ({len(children_joined)}/{len(SPACE_CHILDREN)})",
        len(children_joined) == len(SPACE_CHILDREN))
    log("dm_room returned",    bool(r.get("dm_room")))

    # Verify from the new user's own sync
    time.sleep(1)
    _s, sync = http("GET", f"{HS}/_matrix/client/v3/sync?timeout=0", token=token)
    joined = list(sync.get("rooms", {}).get("join", {}).keys())
    log(f"user sees space in join set",
        any(rid.split(":")[0] == SPACE_ID.split(":")[0] for rid in joined))
    log(f"user joined all {len(SPACE_CHILDREN)} children",
        all(any(rid.split(":")[0] == c.split(":")[0] for rid in joined)
            for c in SPACE_CHILDREN))
    log("user sees DM room", any(rid == r.get("dm_room") for rid in joined))

    return mxid


# --- Path B: knock (federated-guest style) ---

def test_knock_path():
    # Register a throwaway account on THIS server to simulate a federated guest.
    # (In real federation the guest's account lives elsewhere, but the knock
    # endpoint behavior is the same.)
    username = f"smoke_knock_{int(time.time())}_{secrets.token_hex(2)}"
    print(f"\n[knock] test account: {username}", flush=True)

    _s, init = http("POST", f"{HS}/_matrix/client/v3/register", body={})
    session = init.get("session")
    if not session:
        log("register init", False, f"body={init}")
        return None

    status, r = http("POST", f"{HS}/_matrix/client/v3/register", body={
        "auth": {"type": "m.login.registration_token",
                 "token": REG_TOKEN, "session": session},
        "username": username,
        "password": secrets.token_urlsafe(32),
    })
    log("knock-test account registered", status == 200, f"status={status}")
    if status != 200:
        return None
    token = r["access_token"]
    mxid  = r["user_id"]

    status, _ = http(
        "POST",
        f"{HS}/_matrix/client/v3/knock/{urllib.parse.quote(SPACE_ID)}",
        token=token, body={"reason": KNOCK_CODE})
    log("knock posted", status == 200, f"status={status}")

    # Poll for auto-approval (should arrive within a few seconds)
    deadline = time.time() + 15
    got_invite = False
    while time.time() < deadline:
        _s, sync = http("GET", f"{HS}/_matrix/client/v3/sync?timeout=0", token=token)
        invites = list(sync.get("rooms", {}).get("invite", {}).keys())
        if any(rid.split(":")[0] == SPACE_ID.split(":")[0] for rid in invites):
            got_invite = True
            break
        time.sleep(1)
    log("auto-approved within 15s", got_invite)

    # Try a knock with a bogus code; should stay unapproved
    username2 = f"smoke_badcode_{int(time.time())}_{secrets.token_hex(2)}"
    _s, init = http("POST", f"{HS}/_matrix/client/v3/register", body={})
    sess2 = init["session"]
    _s, r2 = http("POST", f"{HS}/_matrix/client/v3/register", body={
        "auth": {"type": "m.login.registration_token",
                 "token": REG_TOKEN, "session": sess2},
        "username": username2,
        "password": secrets.token_urlsafe(32),
    })
    bad_token = r2["access_token"]
    bad_mxid  = r2["user_id"]
    http("POST",
         f"{HS}/_matrix/client/v3/knock/{urllib.parse.quote(SPACE_ID)}",
         token=bad_token, body={"reason": "definitely-not-a-real-code-" + secrets.token_hex(4)})
    time.sleep(8)
    _s, sync = http("GET", f"{HS}/_matrix/client/v3/sync?timeout=0", token=bad_token)
    invites = list(sync.get("rooms", {}).get("invite", {}).keys())
    log("bad code stays unapproved",
        not any(rid.split(":")[0] == SPACE_ID.split(":")[0] for rid in invites))

    return [mxid, bad_mxid]


def main():
    missing = [k for k in ("ADMIN_TOKEN", "SIGNUP_CODE", "KNOCK_CODE", "REG_TOKEN")
               if not globals()[k]]
    if missing:
        print(f"missing env vars: {missing}", file=sys.stderr)
        return 2

    print(f"smoke test against {HS}", flush=True)

    to_cleanup = []
    signup_mxid = test_signup_path()
    if signup_mxid:
        to_cleanup.append(signup_mxid)
    knock_mxids = test_knock_path()
    if knock_mxids:
        to_cleanup.extend(knock_mxids)

    # Cleanup
    if to_cleanup:
        print(f"\n[cleanup] kicking {len(to_cleanup)} test user(s)", flush=True)
        for mxid in to_cleanup:
            admin_kick(mxid, [SPACE_ID] + SPACE_CHILDREN)

    failed = [n for n, ok in results if not ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} passed ===", flush=True)
    if failed:
        print(f"failed: {failed}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
