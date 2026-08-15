=== TIER 1 TRANSCRIPT — issue #2 — 2026-08-15T22:50:54Z ===

--- Step A: grep -rn socrates1024 skills/ (expect nothing, exit 1) ---
exit=1

--- Step B: Path A paste unmodified inviter (URL filled, <INVITER> left) ---
exit=1 (halt = correct)

--- Step C: inviter substituted (@somebody:matrix.org) ---
    raise KeyError(key) from None
KeyError: 'MATRIX_HOMESERVER'
exit=1 (passes guard, stops at missing MATRIX_HOMESERVER env = pre-credential, no DM attempted)

--- Step D: Path B untouched ---
96:# Set by sync_loop once the persistent Olm store is open.  The inviter-side
140:# Default inviter MXID to DM from the new account when someone signs up.
141:# Per-code override: set "inviter" on the signup_codes.json entry.
642:        "inviter": mxid,
2358:        record_endorsement(entry.get("inviter") or entry.get("minted_by") or OUR_MXID,
 PLAN.md                            | 151 +++----------------------------------
 skills/matrix-invite-join/SKILL.md |   7 +-
 2 files changed, 18 insertions(+), 140 deletions(-)
--- Step E: live GET directory/room (paste step 1, read-only, staging homeserver) ---
alias resolves: !4FL8uL5OEYLATG1VH4wC2CD3pfIV6BMFId9VT7rmm-g servers: ['gitter.im', 'matrix.org', 'mtrx.shaperotator.xyz', 'unredacted.org']

## Assertion vs issue #2 Acceptance
1. `grep -rn socrates1024 skills/` → nothing (Step A above, exit=1). The :356 hit named in the
   issue no longer existed on staging (removed by #67); only :41 remained and is now the
   `<INVITER>` placeholder + adjacent substitution instruction (see diff).
2. Unmodified paste (URL filled, inviter unreplaced) → HALT with substitution instruction
   (Step B); no DM attempted, guard sits before env/credential access and before createRoom.
   Substituted inviter → advances past guard (Step C).
3. Path B untouched: knock-approver/approver.py unchanged in this diff; still derives
   `inviter` from `entry.get("inviter") or ONBOARDING_INVITER_MXID` (per-code override).
Lane gate bridge/acceptance.sh: PASSED, 6 assertions (acceptance.log).
Not verified live: the actual DM send with a substituted inviter (flow step 3) — the fixture
hard rule allows sending only to the single fixture room, while the DM step creates a new room
and invites a user; verified instead up to the credential gate + live read-only alias resolve.
