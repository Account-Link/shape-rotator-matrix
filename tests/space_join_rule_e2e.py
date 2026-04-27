"""Regression test for the space's join_rule.

Caught after Alexis (an external matrix.org user) hit
"[403] You do not belong to any of the required rooms/spaces to join this
room" when clicking the public room alias. Root cause: the space's
m.room.join_rules had been flipped from `knock` to `restricted` with a
self-referential allow, so nobody outside the space could enter without
already being in the space.

Two assertions:

  1. State assertion — the SPACE's m.room.join_rules is exactly
     {"join_rule": "knock"}. No `allow` list, no `restricted`.
  2. Behavioural — a fresh non-member who POSTs /rooms/{space}/join
     gets 403, and the error text does NOT contain the diagnostic
     phrase "required rooms/spaces" (the verbatim string Alexis saw —
     its presence means we've regressed to `restricted`).

Env (set by run_in_runner.sh):
  DEV_HS, DEV_REG_TOKEN, SPACE_ID, ADMIN_TOKEN
"""
import json, os, secrets, sys, time, urllib.error, urllib.parse, urllib.request

HS        = os.environ.get("DEV_HS", "http://landing:80").rstrip("/")
REG_TOKEN = os.environ["DEV_REG_TOKEN"]
SPACE_ID  = os.environ["SPACE_ID"]
ADMIN_TOK = os.environ.get("ADMIN_TOKEN", "")

results = []
def log(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    results.append((name, ok))

def http(method, path, token=None, body=None, timeout=15):
    headers = {"Content-Type": "application/json"}
    if token: headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{HS}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, {"_raw": raw.decode(errors="replace")}


def register_fresh():
    _, init = http("POST", "/_matrix/client/v3/register", body={})
    sess = init.get("session")
    username = f"alexis_probe_{int(time.time())}_{secrets.token_hex(2)}"
    status, r = http("POST", "/_matrix/client/v3/register", body={
        "auth": {"type": "m.login.registration_token",
                 "token": REG_TOKEN, "session": sess},
        "username": username,
        "password": secrets.token_urlsafe(32),
    })
    if status != 200:
        raise RuntimeError(f"register: {status} {r}")
    return r["user_id"], r["access_token"]


def main():
    # 1. State assertion via ADMIN_TOK (bot has read access).
    status, body = http("GET",
        f"/_matrix/client/v3/rooms/{urllib.parse.quote(SPACE_ID)}/state/m.room.join_rules",
        token=ADMIN_TOK)
    log("space join_rules readable", status == 200, f"status={status}")
    log("space join_rule is 'knock' (not 'restricted')",
        body.get("join_rule") == "knock",
        f"got={body!r}")
    log("space join_rules has no 'allow' list",
        "allow" not in body,
        f"got={body!r}")

    # 2. Behavioural — fresh user, no code, /join the space directly.
    mxid, tok = register_fresh()
    print(f"  [info] probe user: {mxid}", flush=True)
    status, body = http("POST",
        f"/_matrix/client/v3/rooms/{urllib.parse.quote(SPACE_ID)}/join",
        token=tok, body={})
    log("fresh user /join space is rejected", status == 403, f"status={status}")
    err = (body.get("error") or "").lower()
    log("403 is not the restricted-rule error (Alexis-style regression)",
        "required rooms" not in err and "required spaces" not in err,
        f"err={err!r}")

    # Cleanup — kick the probe user from anywhere it might have ended up.
    if ADMIN_TOK:
        http("POST",
            f"/_matrix/client/v3/rooms/{urllib.parse.quote(SPACE_ID)}/kick",
            token=ADMIN_TOK, body={"user_id": mxid, "reason": "regression-test cleanup"})

    failed = [n for n, ok in results if not ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} pass ===")
    if failed:
        print("FAILED: " + ", ".join(failed), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
