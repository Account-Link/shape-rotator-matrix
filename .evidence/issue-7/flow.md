# Issue #7 — Matrix admin moderation surface (`!kick` / `!ban` / `!unban` / `!stats`) — PR #68 evidence

Branch `ready-7`, head `e1f6c23` (rebased onto `staging` @ `b0d713d` by the rework lane
2026-08-16; conflicts in `knock-approver/approver.py` + `PLAN.md` resolved preserving both
intents — retention-room factory from #87/#88 kept, this PR's moderation block appended).

## Tier classification (and why not the generic forms)

Changed files: `README.md`, `deploy/admin/README.md`, `knock-approver/approver.py`.

- The approver is a **mautrix-python client** — it serves no HTTP routes. This repo has no
  `/_api/version` endpoint (`grep -rniE '_api/version'` → empty; same finding as merged #67
  and #88), so the generic Tier-1 anchor ("HTTP transcript + `/_api/version` pinned") is
  structurally inapplicable to this repo. What the anchor exists to prove — that the behavior
  ran against the real deployed environment — is pinned here by a **live read-only call
  against the deployed staging homeserver** (`staging_anchor.txt`), the same substitute the
  gate accepted for #88.
- The behavior IS visible to humans (admins see command replies in their Matrix client), but
  the repo owns no web UI to screenshot and this box has no Matrix real-browser rig; the
  gate's file-based tier for this diff is the api/Tier-1 branch, satisfied below by driving
  the commands end-to-end over the Matrix client-server API (the exact HTTP surface the
  change rides on), encrypted, against a real homeserver running this branch's code.
- NOT declared Tier 0: this diff adds real behavior (four new commands). Tier 0 is for
  zero-behavior-change diffs and is not claimed here.

## What was run (all on head `e1f6c23` in a clean worktree)

1. **Standing PR gate** — `bash tests/run_e2e.sh`: **all gating tests passed**
   (`e2e_gate_transcript.txt`; includes `admin_e2ee.py` 7/7 `!mint` E2EE and
   `retention_room_e2e.py` 21/21, the landed block this PR's code now sits beside).
2. **New-command drive** — `.evidence/issue-7/drive_admin_moderation.py` (committed,
   reproducible) spins up the same docker stack, then: three fresh users (PL-50 admin `A`,
   PL-0 `N`, victim `V`), each admin sender a real mautrix client with OlmMachine +
   PgCryptoStore, commands sent **encrypted** into the encrypted admin command room, replies
   decrypted and asserted, and `V`'s space membership verified **independently** over the
   client-server HTTP API. **15/15 pass** (`moderation_e2ee_transcript.txt` + the
   knock-approver container's own `[admin] dispatch … is_admin=…` lines).
3. **Live staging anchor** — read-only GETs vs `https://mtrx.shaperotator.xyz`:
   `/_matrix/client/versions` → HTTP 200, and the space alias resolves to
   `!4FL8uL5OEYLATG1VH4wC2CD3pfIV6BMFId9VT7rmm-g` — the same room ID recorded in merged
   #88's evidence, i.e. the exact room the new `/_matrix/client/v3/rooms/{space}/(kick|ban|unban)`
   calls will act on in the deployed environment (`staging_anchor.txt`).

## Acceptance of issue #7, asserted line by line

- `!mint` (knock + signup) implemented and gates on PL — **pre-existing on staging**, not in
  this diff; re-proven by the gate's `admin_e2ee.py` (7/7) on this head. The PL gate itself
  is additionally proven live for the NEW commands by the `N` (PL 0) refusal below.
- `!codes` and `!revoke` implemented — pre-existing on staging, not in this diff.
- `!ban` / `!kick` / `!unban` implemented — **driven live, encrypted**: bot replies
  `kicked/banned/unbanned @mod_victim…:localhost:46167 from the space`, and the victim's
  membership was verified over the client-server API to go `join → leave` (kick), `→ ban`
  (ban), `→ leave` (unban). Guards proven too: bare `!kick` → usage line; `!kick @admin`
  → "refused: I will not kick myself"; PL-0 user → "refused — need PL >= 50 or be on the
  allowlist" (also visible in the bot's own dispatch log: `is_admin=False`).
- `!stats` reads from audit log, returns aggregate counts + top captcha keywords — driven
  live twice (before/after moderation traffic):
  `last 24h: knocks=0, promoted=0, rejected=0, pending=0` + `top captcha keywords: none`.
  Honest limit: the stack was fresh, so the keyword list is the EMPTY state — aggregation
  over populated keyword events was covered by the PR's focused handler checks, not by this
  live drive.
- E2EE-ness decision recorded + `deploy/admin/README.md` co-admin handoff — doc additions in
  this diff (`README.md`, `deploy/admin/README.md`, +25 lines).

## What could NOT be verified (unchanged from the PR body, still true)

- No Matrix real-browser rig on this box → no signed-in screenshots of a human's Matrix
  client; the E2EE command round-trips above are the same wire behavior a client renders.
- The deployed staging approver runs MERGED staging code only, so unmerged commands cannot
  be driven against the deployment itself (pre-merge); the anchor pins the deployed
  environment instead.

## Repro

```
git worktree add /tmp/rw-68 ready-7 && cd /tmp/rw-68/tests
bash run_e2e.sh                                   # gate
# then the single-bootstrap dance from run_e2e.sh, and:
docker compose -f docker-compose.test.yml -p shape-rotator-e2e run -T --rm test-runner sh -c '
  set -a; . /shared/test.env; set +a; export HS=http://landing:80
  DEV_HS="$HS" DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  ADMIN_COMMAND_ROOM="$ADMIN_COMMAND_ROOM" ADMIN_TOKEN="$ADMIN_TOKEN" \
  ADMIN_MXID="$ADMIN_MXID" SPACE_ID="$SPACE_ID" \
  python3 .evidence/issue-7/drive_admin_moderation.py'
```
