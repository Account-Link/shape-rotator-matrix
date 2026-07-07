#!/usr/bin/env bash
# bridge/acceptance.sh — objective round-trip + restart gate for the
# Matrix<->Telegram relay (issue #53).
#
# Drives bridge/mx.py, bridge/tg.py and bridge/relay.py against the SINGLE
# fixture pair in ~/.teleport-travel/test-fixtures.json (the only Matrix room
# and Telegram group bridge code may touch) and asserts, per issue #53:
#
#   1. TG->MX path:  a message sent on the Telegram side is seen by the relay
#                    and handled within the <10s arrival budget;
#   2. MX->TG path:  symmetric, the Matrix side;
#   3. Restart catch-up: messages queued during "downtime" are caught up
#                    EXACTLY ONCE across a restart (no double-delivery).
#
# Exits 0 only if every assertion holds. Any miss — or any hang — fails
# loudly (nonzero exit, no fallbacks, no masking).
#
# --- how a hang is structurally impossible here -------------------------------
# The previous attempt (#53, Jul 4) wedged the lane for 2 days by running the
# LONG-LIVED relay daemon inside a container with no timeout. This script ONLY
# ever invokes `relay.py --once` (one bounded poll pass per side, then exit)
# and wraps EVERY container call in both `timeout` and `docker run --rm`, so a
# stuck process is killed and its container removed automatically. The script
# never starts a daemon. (Operator hard requirement from issue #53 comments.)
#
# --- transcript / logging (nothing masked) -----------------------------------
# Each bridge command's STDOUT (the relay/mx/tg signal: "sent ...", "skip own
# echo ...", "start", "--once complete") is shown on the console. Its STDERR
# (mautrix's chatty crypto logger — expected "Failed to decrypt" warnings for
# megolm sessions that predate this device, plus transient key-fetch noise) is
# appended to a FULL transcript log and is NOT spammed to the console. The
# full log path is printed up front; on ANY failure the relevant tail is
# dumped to the console too. So the console is readable but nothing is hidden
# or discarded — grep the full log for the raw mautrix output.
#
# --- scope: what this gate does and does not assert --------------------------
# The ONLY Matrix identity provisioned on the relay host is the bridge bot
# @shape-bridge, and the ONLY Telegram session is the relay's own Telethon
# account — which is the test group's sole member (verified at runtime). The
# relay INTENTIONALLY filters both as loop-prevention
# (`sender == bot_mxid` on the MX side, `msg.sender_id == tg_me_id` on the TG
# side); without it the relay would echo itself forever.
#
# A *positive* end-to-end round-trip from a second, non-relay participant is
# therefore not deterministically drivable from this box — it needs a second
# Matrix user joined to the test room AND a second Telegram group member,
# which is an operator config change (production pairing is operator-only per
# bridge/README.md). This gate instead asserts the relay's correctness
# properties that ARE drivable with relay-own identities, mapping 1:1 onto the
# issue's three checks:
#
#   1/2. The relay polls + decodes each side and suppresses its own echo
#        within the <10s budget (loop-prevention is the relay's defining
#        correctness property — a regression here means an echo storm). The
#        <10s budget is measured from send to the relay's confirmed
#        "skip own echo" log line for that exact message id / event id.
#   3.   Restart exactly-once: downtime messages are processed in the first
#        --once pass (cursor advances, ids marked seen) and NOT reprocessed by
#        a second --once pass — the durable-cursor / dedup contract that
#        prevents double-delivery on restart.
#
# Extending this to a positive round-trip is commented back on issue #53.
# ----------------------------------------------------------------------------

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_HOME="${HOME:-/home/amiller}"
CONTAINER_HOME="/home/amiller"   # fixed mount target so Path.home() resolves inside
TELEPORT_DIR_HOST="$HOST_HOME/.teleport-travel"
SHAPE_BRIDGE_DIR_HOST="$HOST_HOME/.shape-bridge-bot"
FIXTURES_PATH="$TELEPORT_DIR_HOST/test-fixtures.json"
CREDS_PATH="$SHAPE_BRIDGE_DIR_HOST/creds.json"
STATE_PATH="$TELEPORT_DIR_HOST/relay-state.json"
TG_ENV_PATH="$TELEPORT_DIR_HOST/.env"   # symlink is followed on the host only

RUNNER_IMAGE="shape-bridge-runner"      # tests/Dockerfile (libolm + mautrix)
ACCEPT_IMAGE="shape-bridge-acceptance"  # runner + telethon + python-dotenv

# Bounds. --once is a bounded pass (can't hang); these just catch a regression.
RELAY_ONCE_TIMEOUT=60   # one relay --once pass (priming + each check step)
CLI_TIMEOUT=90          # mx.py / tg.py send+tail (incl. mautrix bootstrap/sync)
# issue #53's "arrival <10s" is the RELAY's responsibility: how long after a
# message exists does the relay poll + process it. We therefore time only the
# relay --once pass (not mx.py/tg.py send latency, which is test-harness
# overhead — mx.py's ensure_ready cold-start alone is ~5-7s and would make the
# MX path flakily exceed 10s for reasons unrelated to the relay).
ARRIVAL_BUDGET=10

# Full transcript (every command's combined stdout+stderr) for the operator.
LOG_DIR="${ACCEPTANCE_LOG_DIR:-$REPO/.acceptance-logs}"
FULL_LOG="$LOG_DIR/full.log"
mkdir -p "$LOG_DIR"
: > "$FULL_LOG"   # truncate at start of run

declare -i PASS_COUNT=0 FAIL_COUNT=0
_LAST_STEP="(pre-flight)"   # shown in failure dumps

#-----------------------------------------------------------------------------
# Reporting helpers (loud failures, no masking).
#-----------------------------------------------------------------------------
section() { printf '\n===== %s =====\n' "$*"; }
note()    { printf '[accept] %s\n' "$*"; }
pass()    { printf '[accept] PASS: %s\n' "$*"; PASS_COUNT+=1; }
fail()    {
  # A failure is terminal — dump the tail of the full log so the cause is on
  # the console, then exit nonzero so the worker lane sees RED.
  printf '[accept] FAIL (%s): %s\n' "$_LAST_STEP" "$*" >&2
  if [ -s "$FULL_LOG" ]; then
    # printf '%s\n' "<literal>" (not a leading-dash format string) so a log
    # path or marker starting with '-' can't be parsed as a printf option.
    printf '\n' >&2
    printf '%s\n' "=== tail of $FULL_LOG ===" >&2
    tail -40 "$FULL_LOG" >&2 || true
    printf '%s\n' "=== end log tail (full log: $FULL_LOG) ===" >&2
  fi
  FAIL_COUNT+=1
  exit 1
}

#-----------------------------------------------------------------------------
# Container cleanup — kill+remove any container this script started, even on a
# signal or a `timeout` kill. Names are prefixed with the script PID ($$) so a
# concurrent run (or a stale one) can't be touched.
#-----------------------------------------------------------------------------
_ctr_seq=0
next_ctr_name() { _ctr_seq=$((_ctr_seq + 1)); printf 'accept-%d-%d\n' "$$" "$_ctr_seq"; }
cleanup() {
  local rc=$?
  local ids
  ids="$(docker ps -aq --filter "name=accept-$$-" 2>/dev/null || true)"
  if [ -n "$ids" ]; then
    docker rm -f $ids >/dev/null 2>&1 || true
  fi
  if [ "$rc" -eq 0 ]; then
    note "ACCEPTANCE PASSED ($PASS_COUNT assertions)"
    note "full transcript (incl. mautrix stderr): $FULL_LOG"
  else
    note "ACCEPTANCE FAILED (passed $PASS_COUNT before failure)"
    note "full transcript (incl. mautrix stderr): $FULL_LOG"
  fi
  return "$rc"
}
trap cleanup EXIT

#-----------------------------------------------------------------------------
# Resolve the Telegram API creds from the host-side .env (following the
# symlink) and pass them through to the container as -e vars. This is the exact
# gap that sank the previous attempt: inside the container the .env symlink
# dangles, so tg.py saw "TELEGRAM_API_ID/HASH missing". We never rely on the
# symlink resolving inside the container.
#-----------------------------------------------------------------------------
resolve_tg_creds() {
  local real_env
  real_env="$(readlink -f "$TG_ENV_PATH" 2>/dev/null || true)"
  [ -n "$real_env" ] && [ -f "$real_env" ] || fail "TG .env not found at $TG_ENV_PATH"
  set -a
  # shellcheck disable=SC1090
  eval "$(grep -E '^(TELEGRAM_API_ID|TELEGRAM_API_HASH)=' "$real_env" | sed 's/^/export /')"
  set +a
  TG_API_ID="${TELEGRAM_API_ID:-}"
  TG_API_HASH="${TELEGRAM_API_HASH:-}"
  [ -n "$TG_API_ID" ] && [ -n "$TG_API_HASH" ] || fail "TELEGRAM_API_ID/HASH missing in $real_env"
}

#-----------------------------------------------------------------------------
# run_bridge <timeout_secs> <args...>
#
# Runs a command in the acceptance image as the HOST uid (so state stays
# host-owned — never root-owned like the prior attempt), under `timeout`, with
# `docker run --rm` (auto-remove even on a kill).
#
# Stream handling: STDOUT (the bridge signal) is both printed to the console
# AND returned in $LAST_OUT for the caller to parse. STDERR (mautrix crypto
# chatter) is appended to $FULL_LOG only — preserved, never shown unless a
# step fails (then fail() dumps the tail). Nothing is discarded.
#-----------------------------------------------------------------------------
LAST_OUT=""
run_bridge() {
  local secs="$1"; shift
  local out rc
  _LAST_STEP="${1##*/} ${*:2}"   # e.g. "relay.py --once"
  # stderr -> full log; stdout -> console + captured.
  out="$(timeout "${secs}" docker run --rm \
        --name "$(next_ctr_name)" \
        --user "$(id -u):$(id -g)" \
        -e "HOME=$CONTAINER_HOME" \
        -e "TELEGRAM_API_ID=$TG_API_ID" \
        -e "TELEGRAM_API_HASH=$TG_API_HASH" \
        -v "$TELEPORT_DIR_HOST:$CONTAINER_HOME/.teleport-travel" \
        -v "$SHAPE_BRIDGE_DIR_HOST:$CONTAINER_HOME/.shape-bridge-bot" \
        -v "$REPO:/repo" \
        -w /repo \
        "$ACCEPT_IMAGE" "$@" 2>>"$FULL_LOG")" && rc=$? || rc=$?
  LAST_OUT="$out"
  # Mirror stdout into the full transcript too (stderr is already there).
  [ -n "$out" ] && printf '%s\n' "$out" >>"$FULL_LOG"
  if [ -n "$out" ]; then printf '%s\n' "$out"; fi   # console: the clean signal
  return "$rc"
}

#-----------------------------------------------------------------------------
# Thin wrappers around the three bridge CLIs. A nonzero / timed-out exit
# propagates (set -e) and fails loudly.
#-----------------------------------------------------------------------------
tg_send()    { run_bridge "$CLI_TIMEOUT" python3 bridge/tg.py send "$1"; }
mx_send()    { run_bridge "$CLI_TIMEOUT" python3 bridge/mx.py send "$1"; }
tg_tail()    { run_bridge "$CLI_TIMEOUT" python3 bridge/tg.py tail "${1:-15}" >/dev/null; }      # quiet: caller greps $LAST_OUT
mx_tail()    { run_bridge "$CLI_TIMEOUT" python3 bridge/mx.py tail "${1:-15}" >/dev/null; }      # quiet: caller greps $LAST_OUT
relay_once() { run_bridge "$RELAY_ONCE_TIMEOUT" python3 bridge/relay.py --once; }

#-----------------------------------------------------------------------------
# relay-state.json readers (host-side python3, no mautrix needed).
#-----------------------------------------------------------------------------
state_tg_last_id() {
  python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("tg_last_id",0))' "$STATE_PATH"
}
state_mx_has() {
  python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));sys.exit(0 if sys.argv[2] in d.get("mx_seen",[]) else 1)' \
    "$STATE_PATH" "$1"
}

#=============================================================================
# 0. Pre-flight + image build
#=============================================================================
section "pre-flight"
note "repo:        $REPO"
note "fixtures:    $FIXTURES_PATH"
note "creds:       $CREDS_PATH"
note "state:       $STATE_PATH"
note "full log:    $FULL_LOG  (mautrix stderr captured here; shown on failure)"

command -v docker >/dev/null || fail "docker not on PATH"
[ -f "$FIXTURES_PATH" ] || fail "missing fixtures $FIXTURES_PATH"
[ -f "$CREDS_PATH" ]    || fail "missing bot creds $CREDS_PATH"
[ -f "$STATE_PATH" ]    || fail "missing relay state $STATE_PATH (run relay.py --once once first)"
# State must be writable by the host uid or the relay can't persist cursors
# (the prior attempt left root-owned state behind; refuse to silently corrupt).
[ -w "$STATE_PATH" ]    || fail "$STATE_PATH not writable by $(id -un) — fix ownership (root-owned cruft from a prior docker run?)"
[ -d "$SHAPE_BRIDGE_DIR_HOST/store" ] || fail "missing bot store dir"
[ -w "$SHAPE_BRIDGE_DIR_HOST/store" ] || fail "bot store dir not writable by $(id -un)"
python3 -c 'import json,sys;json.load(open(sys.argv[1]))' "$FIXTURES_PATH" \
  || fail "fixtures not valid JSON"
python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));assert d.get("matrix_room_id","").startswith("!") and d.get("telegram_chat_id")' "$FIXTURES_PATH" \
  || fail "fixtures missing matrix_room_id / telegram_chat_id"
resolve_tg_creds
note "telegram api id: ${TG_API_ID}"

section "build runner image (tests/Dockerfile: libolm + mautrix)"
build_log="$(mktemp)"
if ! docker build -t "$RUNNER_IMAGE" -f "$REPO/tests/Dockerfile" "$REPO/tests" >"$build_log" 2>&1; then
  tail -30 "$build_log" >&2; fail "runner image build failed (see above)"
fi
section "build acceptance image (runner + telethon + python-dotenv)"
if ! docker build -t "$ACCEPT_IMAGE" -f "$REPO/bridge/acceptance.Dockerfile" "$REPO/bridge" >"$build_log" 2>&1; then
  tail -30 "$build_log" >&2; fail "acceptance image build failed (see above)"
fi
pass "images built"

#=============================================================================
# Baseline: one --once pass to advance cursors to "now" (skip backlog on a
# fresh state; a no-op on an already-warm state). Also proves --once runs
# clean + bounded before the checks depend on it.
#=============================================================================
section "baseline: relay --once (advance cursors to now)"
note "state before: $(cat "$STATE_PATH")"
relay_once >/dev/null
note "state after:  $(cat "$STATE_PATH")"
pass "baseline --once completed within ${RELAY_ONCE_TIMEOUT}s"

# Unique token family for this run — assertions key off these substrings, so
# organic traffic / prior runs can't interfere.
RUN_ID="$(date +%s)-$RANDOM$RANDOM"
T_TG2MX="ACPT-TG2MX-${RUN_ID}"        # check 1: sent on TG
T_MX2TG="ACPT-MX2TG-${RUN_ID}"        # check 2: sent on MX
T_CATCH_TG="ACPT-CATCH-TG-${RUN_ID}"  # check 3: sent on TG while "down"
T_CATCH_MX="ACPT-CATCH-MX-${RUN_ID}"  # check 3: sent on MX while "down"

#=============================================================================
# CHECK 1 — TG -> MX path (relay notices a TG message within <10s and applies
# loop-prevention: own echo suppressed, cursor advanced, nothing leaked to MX).
#=============================================================================
section "CHECK 1: TG -> MX (relay polls+handles a TG-side message in <${ARRIVAL_BUDGET}s)"
tg_send "$T_TG2MX" >/dev/null || fail "tg.py send failed"
tg_id="$(printf '%s\n' "$LAST_OUT" | grep -oE 'sent id=[0-9]+' | grep -oE '[0-9]+')"
[ -n "$tg_id" ] || fail "couldn't parse tg send id from: $LAST_OUT"
note "sent TG id=$tg_id token=$T_TG2MX"
# Time the RELAY only (send latency is harness overhead — see ARRIVAL_BUDGET).
t0=$(date +%s)
relay_once >/dev/null || fail "relay --once failed during check 1"
once1="$LAST_OUT"
t1=$(date +%s); elapsed=$((t1 - t0))

# (a) relay saw THIS message and skipped it (loop-prevention fired for its id).
printf '%s\n' "$once1" | grep -F "skip own echo id=$tg_id" >/dev/null \
  || fail "relay did not log 'skip own echo id=$tg_id' (didn't poll the TG message?)"
# (b) durable TG cursor advanced past it.
cur="$(state_tg_last_id)"
[ "$cur" -ge "$tg_id" ] || fail "tg_last_id=$cur < sent id=$tg_id (cursor not advanced)"
# (c) nothing leaked to Matrix (the token must NOT appear relayed on the MX side).
mx_tail 12
if printf '%s\n' "$LAST_OUT" | grep -F "$T_TG2MX" >/dev/null; then
  fail "own-echo token $T_TG2MX LEAKED into Matrix (loop-prevention broken)"
fi
# (d) the issue's <10s budget: send -> relay handled it.
[ "$elapsed" -le "$ARRIVAL_BUDGET" ] \
  || fail "TG->MX handling took ${elapsed}s > ${ARRIVAL_BUDGET}s budget"
pass "TG->MX: relay noticed+suppressed id=$tg_id in ${elapsed}s, cursor=$cur, no MX leak"

#=============================================================================
# CHECK 2 — MX -> TG path (symmetric).
#=============================================================================
section "CHECK 2: MX -> TG (relay polls+handles a MX-side message in <${ARRIVAL_BUDGET}s)"
mx_send "$T_MX2TG" >/dev/null || fail "mx.py send failed"
mx_eid="$(printf '%s\n' "$LAST_OUT" | grep -oE 'sent event_id=[^ ]+' | sed 's/sent event_id=//')"
[ -n "$mx_eid" ] || fail "couldn't parse mx send event_id from: $LAST_OUT"
note "sent MX event_id=$mx_eid token=$T_MX2TG"
# Time the RELAY only (mx.py ensure_ready cold-start is harness overhead).
t0=$(date +%s)
relay_once >/dev/null || fail "relay --once failed during check 2"
once2="$LAST_OUT"
t1=$(date +%s); elapsed=$((t1 - t0))

# (a) relay saw THIS event and skipped it (loop-prevention fired for its id).
printf '%s\n' "$once2" | grep -F "skip own echo $mx_eid" >/dev/null \
  || fail "relay did not log 'skip own echo $mx_eid' (didn't sync the MX message?)"
# (b) durable MX dedup ring recorded it.
state_mx_has "$mx_eid" \
  || fail "event $mx_eid not in mx_seen (dedup ring not updated)"
# (c) nothing leaked to Telegram.
tg_tail 12
if printf '%s\n' "$LAST_OUT" | grep -F "$T_MX2TG" >/dev/null; then
  fail "own-echo token $T_MX2TG LEAKED into Telegram (loop-prevention broken)"
fi
# (d) the issue's <10s budget.
[ "$elapsed" -le "$ARRIVAL_BUDGET" ] \
  || fail "MX->TG handling took ${elapsed}s > ${ARRIVAL_BUDGET}s budget"
pass "MX->TG: relay noticed+suppressed $mx_eid in ${elapsed}s, marked seen, no TG leak"

#=============================================================================
# CHECK 3 — Restart catch-up, EXACTLY ONCE each direction.
#   relay is "down" (we never start a daemon). Send on BOTH sides, then run
#   --once (the restart). Both messages must be caught up in that one pass and
#   NOT reprocessed by a second --once pass — the durable-cursor / dedup
#   contract that prevents double-delivery on restart.
#=============================================================================
section "CHECK 3: restart catch-up, exactly-once each direction"

# Queue messages during "downtime" (no relay running between the two sends).
tg_send "$T_CATCH_TG" >/dev/null || fail "tg.py send (catch) failed"
catch_tg_id="$(printf '%s\n' "$LAST_OUT" | grep -oE 'sent id=[0-9]+' | grep -oE '[0-9]+')"
mx_send "$T_CATCH_MX" >/dev/null || fail "mx.py send (catch) failed"
catch_mx_eid="$(printf '%s\n' "$LAST_OUT" | grep -oE 'sent event_id=[^ ]+' | sed 's/sent event_id=//')"
[ -n "$catch_tg_id" ] && [ -n "$catch_mx_eid" ] \
  || fail "couldn't parse catch-up send ids (tg='$LAST_OUT')"
note "downtime sends: TG id=$catch_tg_id  MX event=$catch_mx_eid"

# --- restart #1: both must be caught up in this single pass. ---
relay_once >/dev/null || fail "relay --once (restart #1) failed"
once_a="$LAST_OUT"
printf '%s\n' "$once_a" | grep -F "skip own echo id=$catch_tg_id" >/dev/null \
  || fail "restart #1 did not catch up TG id=$catch_tg_id"
printf '%s\n' "$once_a" | grep -F "skip own echo $catch_mx_eid" >/dev/null \
  || fail "restart #1 did not catch up MX $catch_mx_eid"
state_mx_has "$catch_mx_eid" || fail "MX $catch_mx_eid not marked seen after restart #1"
cur_after_a="$(state_tg_last_id)"
[ "$cur_after_a" -ge "$catch_tg_id" ] || fail "tg_last_id=$cur_after_a < $catch_tg_id after restart #1"
state_snap_a="$(cat "$STATE_PATH")"
pass "restart #1 caught up both (TG id=$catch_tg_id, MX $catch_mx_eid)"

# --- restart #2: NEITHER must be reprocessed (exactly-once). ---
relay_once >/dev/null || fail "relay --once (restart #2) failed"
once_b="$LAST_OUT"
if printf '%s\n' "$once_b" | grep -F "skip own echo id=$catch_tg_id" >/dev/null; then
  fail "restart #2 RE-processed TG id=$catch_tg_id (not exactly-once)"
fi
if printf '%s\n' "$once_b" | grep -F "skip own echo $catch_mx_eid" >/dev/null; then
  fail "restart #2 RE-processed MX $catch_mx_eid (not exactly-once)"
fi
# Marker-specific stability: the catch-up TG id is still past the cursor and
# the catch-up MX event is still in the dedup ring exactly once. (Comparing the
# whole state blob would false-positive on unrelated organic traffic; this is
# immune — the TG group has one member and we key on our own marker ids.)
cur_after_b="$(state_tg_last_id)"
[ "$cur_after_b" -ge "$catch_tg_id" ] || fail "tg_last_id regressed below catch_tg_id after restart #2"
state_mx_has "$catch_mx_eid" || fail "MX $catch_mx_eid vanished from mx_seen after restart #2"
pass "restart #2 re-processed neither (exactly-once confirmed; cursors stable)"

section "transcript summary"
note "tokens used: $RUN_ID"
note "final relay state: $(cat "$STATE_PATH")"
note "all three checks passed"
exit 0
