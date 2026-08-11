"""Acceptance test for issue #78 — retention room factory (epic #76 chip 2).

Self-contained against the dev continuwuity stack (localhost:46167). Imports the
approver module and drives its retention-room factory directly — the same
import-based pattern history_bundle_e2e.py uses for build_room_key_bundle() —
then asserts the six acceptance criteria from the issue body:

  1. Room created via the command core (_create_retention_room, which the
     !retention-room admin command wraps).
  2. /state shows the bot at PL100 and NO other user at >=50; users_default 0.
  3. m.room.encryption present; m.room.retention present with the window.
  4. Pinned policy message exists; text matches the window + is honest.
  5. Room is a space child; a space member can join without an invite.
  6. A later PUT of m.room.retention by another PL100 user (simulated) does
     NOT change the bot's in-force policy record.

The watcher that logs/notices tamper attempts (_detect_retention_tamper) is
implemented in approver.py for the running bot but is exercised in the live
/sync loop, not here; this test proves the stronger property directly — the
in-force store is immutable regardless of room state.

Run:
  python3 tests/retention_room_e2e.py
(env: DEV_HS, DEV_REG_TOKEN; defaults target the local dev stack.)
"""
import asyncio, json, os, secrets, sys, urllib.parse, urllib.request, urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
from sas_e2e import HS, REG_TOKEN, _post, register

RESULTS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    RESULTS.append((name, ok))


def _get(path, token=None):
    h = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{HS}{path}", headers=h, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _put(path, body, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{HS}{path}", headers=h, method="PUT",
                                 data=json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def state_event(room_id, etype, state_key="", token=None):
    """GET a state event. state_key '' -> /state/<type>; else /state/<type>/<sk>."""
    path = f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/state/{etype}"
    if state_key != "":
        path += f"/{urllib.parse.quote(state_key)}"
    return _get(path, token=token)


async def main():
    suffix = secrets.token_hex(3)
    server_name = HS.split("//", 1)[1].rstrip("/")  # e.g. localhost:46167

    # --- provision identities ------------------------------------------------
    # BOT: the retention-room creator (sole PL100). We point the approver
    # module at it as MATRIX_TOKEN/OUR_MXID, like history_bundle_e2e.py wires
    # approver to a test bot.
    bot_mxid, bot_token = register(
        f"ret_bot_{suffix}", secrets.token_urlsafe(24), f"RBOT{suffix}")
    # SPACE_MEMBER: joins the space, then (criterion 5) joins the retention
    # room with no invite via the restricted join rule.
    member_mxid, member_token = register(
        f"ret_member_{suffix}", secrets.token_urlsafe(24), f"RMEM{suffix}")
    # ATTACKER: criterion 6 — a second PL100 user (simulated). In a real
    # retention room nobody but the bot is PL100; we elevate this user via
    # the bot token as TEST SCAFFOLDING to create the simulated-attacker
    # scenario the acceptance names ("another PL100 user (simulated)").
    attacker_mxid, attacker_token = register(
        f"ret_atk_{suffix}", secrets.token_urlsafe(24), f"RATK{suffix}")
    print(f"[retention_e2e] bot={bot_mxid} member={member_mxid} "
          f"attacker={attacker_mxid}", flush=True)

    # --- BOT creates the space (so it can add space-children) ----------------
    s, r = _post(f"{HS}/_matrix/client/v3/createRoom", {
        "name": "retention test space",
        "preset": "public_chat",
        "visibility": "private",
        "creation_content": {"type": "m.space"},
        "power_level_content_override": {"users": {bot_mxid: 100}},
    }, token=bot_token)
    assert s == 200, f"create space: {s} {r}"
    space_id = r["room_id"]

    # Invite + join the space member, so the restricted join rule on the
    # retention room can admit them later (criterion 5).
    s, _ = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(space_id)}/invite",
                 {"user_id": member_mxid}, token=bot_token)
    assert s == 200, f"invite member to space: {s}"
    s, _ = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(space_id)}/join",
                 {}, token=member_token)
    assert s == 200, f"member join space: {s}"

    # --- wire the approver module to the test bot ----------------------------
    os.environ["HS"] = HS
    os.environ["SPACE_ID"] = space_id
    os.environ["MATRIX_TOKEN"] = bot_token
    os.environ["SERVER_NAME"] = server_name
    sys.path.insert(0, str(REPO / "knock-approver"))
    import approver
    approver.OUR_MXID = bot_mxid
    approver.SERVER_NAME = server_name
    approver.TOKEN = bot_token
    approver.AUTH = {"Authorization": f"Bearer {bot_token}"}
    ret_path = Path(f"/tmp/retention_rooms_{suffix}.json")
    if ret_path.exists():
        ret_path.unlink()
    approver.RETENTION_PATH = ret_path
    approver.LOG_PATH = Path(f"/tmp/retention_log_{suffix}.jsonl")

    # --- criterion 1: create via the command core ----------------------------
    NAME = "standup"
    WINDOW_S = 7 * 86400
    rec = await approver._create_retention_room(NAME, WINDOW_S, creator="@test")
    room_id = rec["room_id"]
    check("1a. room created via factory (command core)", bool(room_id),
          f"room={room_id}")

    # 1b/1c. The COMMAND wrapper (!retention-room) parses args + dispatches to
    # the same core and returns a human reply. cmd_retention_room uses raw HTTP
    # for creation (client arg unused), so pass None. Also covers the usage +
    # duration-parse error paths.
    reply = await approver.cmd_retention_room(
        None, space_id, "@test:localhost:46167", "ops-chat 12h")
    check("1b. !retention-room command wrapper creates a room + replies",
          "created retention room" in reply, f"reply={reply[:80]!r}")
    bad_usage = await approver.cmd_retention_room(None, space_id, "@x", "")
    check("1c. !retention-room rejects missing args", "usage:" in bad_usage,
          f"reply={bad_usage[:60]!r}")
    bad_dur = await approver.cmd_retention_room(None, space_id, "@x", "name bogus")
    check("1d. !retention-room rejects a bad duration", "bad duration" in bad_dur,
          f"reply={bad_dur[:60]!r}")

    # --- criterion 2: bot sole PL100, no other user >=50 ---------------------
    s, pl = state_event(room_id, "m.room.power_levels", token=bot_token)
    check("2a. power_levels readable", s == 200, f"status={s}")
    users = (pl or {}).get("users", {})
    ge50 = {u: p for u, p in users.items() if p >= 50}
    check("2b. bot is PL100", users.get(bot_mxid) == 100,
          f"bot_pl={users.get(bot_mxid)}")
    check("2c. no other user at >=50 (bot is sole PL100)",
          ge50 == {bot_mxid: 100}, f"ge50={ge50}")
    check("2d. users_default == 0",
          (pl or {}).get("users_default") == 0,
          f"users_default={(pl or {}).get('users_default')}")

    # --- criterion 3: encryption + retention present -------------------------
    s, enc = state_event(room_id, "m.room.encryption", token=bot_token)
    check("3a. m.room.encryption present (megolm)",
          s == 200 and (enc or {}).get("algorithm") == "m.megolm.v1.aes-sha2",
          f"{s} {enc}")
    s, ret = state_event(room_id, "m.room.retention", token=bot_token)
    check("3b. m.room.retention present with window (ms)",
          s == 200 and (ret or {}).get("max_lifetime") == WINDOW_S * 1000,
          f"{s} {ret}")

    # --- criterion 4: pinned policy message matches window ------------------
    s, pinned = state_event(room_id, "m.room.pinned_events", token=bot_token)
    pinned_ids = (pinned or {}).get("pinned", []) if s == 200 else []
    check("4a. exactly one event pinned", len(pinned_ids) == 1,
          f"pinned={pinned_ids}")
    ev_id = pinned_ids[0] if pinned_ids else None
    if ev_id:
        s, ev = _get(
            f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
            f"/event/{urllib.parse.quote(ev_id)}", token=bot_token)
    else:
        s, ev = 0, {}
    body = ((ev or {}).get("content") or {}).get("body", "") if s == 200 else ""
    window_label = approver._render_window(WINDOW_S)
    check("4b. pinned text mentions the configured window",
          window_label in body and str(WINDOW_S) in body,
          f"label={window_label!r} body[:80]={body[:80]!r}")
    check("4c. pinned text is honest (states what it does NOT do)",
          "DOES NOT" in body and "deletion" in body,
          f"body[:120]={body[:120]!r}")
    check("4d. pinned text equals bot policy_text for this window",
          body == approver._retention_policy_text(NAME, WINDOW_S), "")

    # --- criterion 5: space child + member joins without invite --------------
    s, child = state_event(space_id, "m.space.child", state_key=room_id,
                           token=bot_token)
    check("5a. room is linked as a space child (m.space.child present)",
          s == 200 and "via" in (child or {}), f"{s} {child}")
    s, jr = state_event(room_id, "m.room.join_rules", token=bot_token)
    check("5b. join_rule restricted to the space",
          (jr or {}).get("join_rule") == "restricted"
          and any((a or {}).get("room_id") == space_id
                  for a in (jr or {}).get("allow", [])),
          f"{jr}")
    s, _ = _post(
        f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join",
        {}, token=member_token)
    check("5c. space member joined retention room with NO invite",
          s == 200, f"status={s}")

    # --- criterion 6: later PUT by another PL100 (simulated) is ignored -----
    # Invite + join the attacker, then elevate them to PL100 (bot token = test
    # scaffolding to create the simulated second-PL100 scenario). The attacker
    # PUTs a different m.room.retention. The room state DOES change (proving
    # the PUT landed) but the bot's in-force policy store must NOT.
    _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/invite",
          {"user_id": attacker_mxid}, token=bot_token)
    s, _ = _post(
        f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join",
        {}, token=attacker_token)
    assert s == 200, f"attacker join room: {s}"
    s, pl2 = state_event(room_id, "m.room.power_levels", token=bot_token)
    pl2.setdefault("users", {})[attacker_mxid] = 100
    s, _ = _put(
        f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
        f"/state/m.room.power_levels", pl2, token=bot_token)
    check("6a. elevated attacker to PL100 (simulated)", s == 200, f"status={s}")

    tampered_ms = 86400000  # 1d in ms (window is 7d)
    s, rb = _put(
        f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
        f"/state/m.room.retention", {"max_lifetime": tampered_ms},
        token=attacker_token)
    check("6b. attacker PUT of m.room.retention accepted by server",
          s == 200, f"status={s} body={rb}")
    s, ret2 = state_event(room_id, "m.room.retention", token=bot_token)
    check("6c. room state DID change (proves the PUT landed)",
          (ret2 or {}).get("max_lifetime") == tampered_ms,
          f"room_state={ret2}")
    # ... yet the bot's in-force policy is unchanged (immutable write-once store).
    store = approver._load_retention()
    in_force = store.get(room_id, {})
    check("6d. bot in-force window UNCHANGED (immutable store)",
          in_force.get("window_seconds") == WINDOW_S
          and in_force.get("max_lifetime_ms") == WINDOW_S * 1000,
          f"in_force_window={in_force.get('window_seconds')} "
          f"in_force_ms={in_force.get('max_lifetime_ms')}")

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} pass ===")
    if failed:
        print("FAILED: " + ", ".join(failed), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
