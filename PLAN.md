# PLAN — issue #60: escrow durability (megolm inbound sessions survive self-heal wipe)

Branch: `ready-60` (base `staging`). Tier 1 (backend/self-heal; no UI surface).

## Acceptance (from issue #60)
A test in `tests/` (dev stack, style of `history_e2ee_repro.py`) showing:
bot client receives an encrypted message → simulate re-mint (run the ACTUAL
export/wipe/import functions with a NEW device_id + fresh store, not a
hand-rolled imitation) → bot still decrypts the old message. Test run output
in the PR. Tier 1: test transcript is the evidence.

## Tasks
- [x] Read MATRIX_ONBOARDING.md (required), self_heal_unit.py, PR #59 primitive.
- [x] Confirm mautrix API: `export_session(idx)->str`, `import_session(session_key,...)`,
      `put_group_session`, enumerate via SQL on `crypto_megolm_inbound_session`.
- [ ] **approver.py**: add `ESCROW_PATH`, `export_inbound_sessions(cs)`,
      `import_inbound_sessions(cs)` (raises on per-session failure, no skip),
      `_export_escrow_for_wipe(mxid, old_device_id)` helper.
- [ ] **approver.py main()**: export BEFORE `_wipe_crypto_store()`.
- [ ] **approver.py sync_loop()**: import AFTER fresh store opens.
- [ ] **tests/escrow_durability.py**: bot devA receives+decrypts → export → wipe →
      fresh store w/ NEW device_id → import → old msg still decrypts. Uses the
      ACTUAL approver functions + sas_e2e helpers.
- [ ] Run the test against the dev stack (continuwuity in docker; test in the
      `tests/Dockerfile` runner, `--network host`). Capture transcript.
- [ ] Commit (incl. transcript), push, open PR to staging. Remove `ready` label.

## Notes (issue constraints)
- plaintext in /data is fine (same trust domain as bot_crypto.db).
- imported sessions carry forwarding chain / lower trust — expected.
- keep the escrow file after import (it IS the durable escrow; replaced next wipe).
- import MUST raise, never silently skip (no fallbacks).
