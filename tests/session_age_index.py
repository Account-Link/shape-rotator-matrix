"""Issue #77 — session_id -> age index (chip 1 of the retention epic #76).

`crypto_megolm_inbound_session` has no timestamp, so the bot can't tell how old a
megolm session is. This pins down the fix: a cleartext index
`room_id -> {session_id: earliest origin_server_ts}` built off `m.room.encrypted`
events as they arrive in /sync (no decryption needed) and persisted to /data.

Scenario (mirrors the issue's `## Acceptance`):
  1. The BOT creates an E2EE room and is present from event 0.
  2. alice joins and sends an encrypted message -> session S1.
  3. alice's cached outbound session is dropped, forcing the next send to mint a
     NEW megolm session S2 (mautrix: encrypt -> EncryptionError -> re-share).
  4. The bot /syncs both batches, and the ACTUAL production hook
     (approver.iter_encrypted_events + approver.record_session) records each
     session_id with the earliest origin_server_ts seen, keeping the min.
  5. Every inbound session in crypto_megolm_inbound_session has an index entry,
     and each indexed ts equals the earliest event ts for that session
     (cross-checked against an authoritative /messages back-pagination).
  6. A FRESH python process re-imports approver against the persisted file and
     reads the same index -> survives a restart.

Run against the dev stack:
  cd dev && docker compose up -d && python3 bootstrap.py
  cd .. && python3 tests/session_age_index.py

Tier 1: this transcript is the evidence (no user-visible surface); same
convention as tests/escrow_durability.py (the #60 test the issue cites).
"""
import asyncio, json, os, secrets, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# --- Import approver (set the env it reads at module load), mirroring
#     tests/escrow_durability.py. ---
os.environ.setdefault("HS", os.environ.get("DEV_HS", "http://localhost:46167"))
os.environ.setdefault("SPACE_ID", "!space:localhost")
os.environ.setdefault("SPACE_CHILD_IDS", "")
os.environ.setdefault("REG_TOKEN", "unused")
os.environ.setdefault("ADMIN_COMMAND_ROOM", "!admin:localhost")
os.environ.setdefault("CONDUWUIT_REGISTRATION_TOKEN",
                      os.environ.get("DEV_REG_TOKEN", "dev-token"))
sys.path.insert(0, str(REPO / "knock-approver"))
sys.path.insert(0, str(REPO / "tests"))

import approver  # noqa: E402  (must be after env setup)
from sas_e2e import HS, _post, make_client, register, sync_once  # noqa: E402
from mautrix.types import EventType, MessageType, TextMessageEventContent  # noqa: E402

results = []
def log(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    results.append((name, ok))


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def encrypted_min_ts_by_session(room_id, token):
    """Authoritative ground truth: back-paginate /messages and group every
    m.room.encrypted event by content.session_id -> min(origin_server_ts)."""
    by_sid = {}
    frm = ""
    for _ in range(10):
        url = (f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}"
               f"/messages?dir=b&limit=100" + (f"&from={frm}" if frm else ""))
        page = _get(url, token)
        for raw in page.get("chunk", []):
            if raw.get("type") != "m.room.encrypted":
                continue
            sid = (raw.get("content") or {}).get("session_id")
            ts = raw.get("origin_server_ts")
            if sid is None or ts is None:
                continue
            by_sid.setdefault(sid, ts)
            if ts < by_sid[sid]:
                by_sid[sid] = ts
        frm = page.get("end")
        if not frm:
            break
    return by_sid


async def bot_sync_and_record(bot, bot_ss, first=False, timeout=5000):
    """One bot /sync cycle that ALSO runs the production index hook: iterate
    m.room.encrypted events off the raw sync dict and call approver.record_session
    (the exact code path sync_loop runs), then handle_sync to decrypt + populate
    the inbound store. Returns the raw sync dict."""
    since = None if first else await bot.sync_store.get_next_batch()
    data = await bot.sync(since=since, timeout=timeout, full_state=first)
    if not isinstance(data, dict):
        return None
    nb = data.get("next_batch")
    if nb:
        await bot.sync_store.put_next_batch(nb)
    # The production hook — record session ages BEFORE decrypting (issue #77).
    n_recorded = 0
    for rid, sid, its in approver.iter_encrypted_events(data.get("rooms", {})):
        approver.record_session(rid, sid, its)
        n_recorded += 1
    bot_ss._joined.clear()
    bot_ss._joined.update(data.get("rooms", {}).get("join", {}).keys())
    tasks = bot.handle_sync(data)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return data, n_recorded


async def main():
    suffix = f"{int(time.time())}_{secrets.token_hex(2)}"
    bot_user = f"sai_bot_{suffix}"
    bot_pw = secrets.token_urlsafe(32)
    bot_dev = f"BOT{secrets.token_hex(2)}"
    bot_mxid, bot_tok = register(bot_user, bot_pw, bot_dev)
    alice_dev = f"ALICE{secrets.token_hex(2)}"
    alice_mxid, alice_tok = register(f"sai_alice_{suffix}",
                                     secrets.token_urlsafe(32), alice_dev)
    print(f"[session_index] bot={bot_mxid} alice={alice_mxid}", flush=True)

    # --- Temp /data surrogate so the test is isolated from the real volume. ---
    tmp = Path(os.environ.get("SAI_TMP", "/tmp")) / f"session_index_{suffix}"
    tmp.mkdir(parents=True, exist_ok=True)
    index_path = tmp / "session_age_index.json"
    approver.SESSION_INDEX_PATH = index_path
    assert not index_path.exists(), "fresh workdir must not have an index yet"

    # --- 1. BOT creates an E2EE room (present from event 0). ---
    bot, bot_cs, bot_ss, bot_db = await make_client(
        bot_mxid, bot_tok, bot_dev, tmp / "bot.db")
    await bot.crypto.share_keys()
    await bot_sync_and_record(bot, bot_ss, first=True)

    s, r = _post(f"{HS}/_matrix/client/v3/createRoom", {
        "name": "session age index",
        "preset": "private_chat",
        "initial_state": [
            {"type": "m.room.history_visibility", "state_key": "",
             "content": {"history_visibility": "shared"}},
            {"type": "m.room.encryption", "state_key": "",
             "content": {"algorithm": "m.megolm.v1.aes-sha2"}},
        ],
    }, token=bot_tok)
    assert s == 200, f"createRoom: {s} {r}"
    room_id = r["room_id"]
    print(f"[session_index] room={room_id}", flush=True)

    # --- alice invited + joins; both sync so devices + state propagate. ---
    s, r = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/invite",
                 {"user_id": alice_mxid}, token=bot_tok)
    assert s == 200, f"invite: {s} {r}"
    s, r = _post(f"{HS}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/join",
                 {}, token=alice_tok)
    assert s == 200, f"alice join: {s} {r}"

    alice, alice_cs, alice_ss, alice_db = await make_client(
        alice_mxid, alice_tok, alice_dev, tmp / "alice.db")
    await alice.crypto.share_keys()
    await sync_once(alice, alice_ss, first=True)
    await sync_once(bot, bot_ss)   # bot sees alice join

    # --- 2. alice sends M1 (auto-encrypts to the bot's device). ---
    m1 = f"first-session {secrets.token_hex(4)}"
    await alice.send_message_event(
        room_id, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=m1))
    _, n1 = await bot_sync_and_record(bot, bot_ss, timeout=5000)
    await sync_once(alice, alice_ss, timeout=2000)
    log("bot received + indexed alice's first encrypted message", n1 >= 1,
        f"{n1} encrypted event(s) recorded")

    # --- 3. FORCE a megolm rotation: drop alice's cached outbound session so
    #     the next encrypt mints a NEW session id (mautrix: encrypt raises
    #     EncryptionError -> re-share -> _new_outbound_group_session). ---
    await alice.crypto.crypto_store.remove_outbound_group_sessions([room_id])
    m2 = f"second-session {secrets.token_hex(4)}"
    await alice.send_message_event(
        room_id, EventType.ROOM_MESSAGE,
        TextMessageEventContent(msgtype=MessageType.TEXT, body=m2))
    _, n2 = await bot_sync_and_record(bot, bot_ss, timeout=5000)
    log("rotation produced a second encrypted message after the drop", n2 >= 1,
        f"{n2} encrypted event(s) recorded")

    # --- ground truth from /messages (authoritative, complete room history). ---
    ground = encrypted_min_ts_by_session(room_id, bot_tok)
    log(">1 distinct megolm session exists (rotation worked)",
        len(ground) >= 2, f"{len(ground)} session(s): {sorted(ground)}")

    # --- the bot's inbound megolm sessions for this room. ---
    rows = await bot_cs.db.fetch(
        "SELECT session_id FROM crypto_megolm_inbound_session "
        "WHERE account_id=$1 AND room_id=$2 AND withheld_code IS NULL",
        bot_cs.account_id, room_id)
    inbound = {str(row["session_id"]) for row in rows}
    log("bot has >=2 inbound megolm sessions for the room",
        len(inbound) >= 2, f"{len(inbound)} inbound: {sorted(inbound)}")

    idx = approver.session_age_index(room_id)
    print(f"[session_index] index[{room_id}] = {json.dumps(idx, sort_keys=True)}",
          flush=True)

    # --- Acceptance #3: every inbound session has an index entry. ---
    missing = sorted(inbound - set(idx))
    log("every crypto_megolm_inbound_session has an index entry (#3)",
        not missing, f"missing: {missing}" if missing else
        f"all {len(inbound)} inbound covered")

    # --- Acceptance #4: each indexed ts == earliest event ts for that session. ---
    bad = []
    for sid, ts in idx.items():
        if sid not in ground:
            bad.append((sid, "no encrypted event uses this session", ts))
        elif ts != ground[sid]:
            bad.append((sid, f"index={ts} earliest={ground[sid]}", ts))
    log("each indexed ts == earliest origin_server_ts for its session (#4)",
        not bad, f"{len(idx)} entries checked" if not bad else f"{len(bad)} bad: {bad[:2]}")

    # Index is a superset of ground truth (the bot may index its own outbound
    # echoes too), but every session the bot can actually decrypt is covered +
    # correct. Sanity: ground truth is fully present + correct in the index.
    log("index covers + is correct for every /messages session",
        all(sid in idx and idx[sid] == ts for sid, ts in ground.items()),
        f"{len(ground)} ground-truth session(s)")

    # --- Acceptance #5: survives a restart. A FRESH python process re-imports
    #     approver against the persisted file and reads the same index. ---
    env = {**os.environ, "SESSION_INDEX_PATH": str(index_path)}
    script = (
        "import sys; sys.path.insert(0, %r); import approver, json; "
        "print(json.dumps(approver.session_age_index(%r), sort_keys=True))"
        % (str(REPO / "knock-approver"), room_id))
    try:
        out = subprocess.check_output([sys.executable, "-c", script],
                                      env=env, text=True, timeout=60)
        restarted = json.loads(out.strip())
        log("index survives a process restart (#5)",
            restarted == idx,
            f"{len(restarted)} entry(ies) reloaded from {index_path.name}"
            if restarted == idx else f"reloaded={restarted} inproc={idx}")
    except Exception as e:
        log("index survives a process restart (#5)", False,
            f"subprocess error: {type(e).__name__}: {e}")

    # the persisted file is plain JSON on /data (survives a volume-preserving
    # restart by construction); confirm it exists and is non-empty.
    log("persisted index file exists on disk (the restart source)",
        index_path.exists() and index_path.stat().st_size > 2,
        f"{index_path.name} size={index_path.stat().st_size if index_path.exists() else 0}")

    await bot_db.stop()
    await bot.api.session.close()
    await alice_db.stop()
    await alice.api.session.close()

    failed = [n for n, ok in results if not ok]
    print(f"\n[session_index] {len(results) - len(failed)}/{len(results)} checks passed",
          flush=True)
    sys.exit(1 if failed else 0)


asyncio.run(main())
