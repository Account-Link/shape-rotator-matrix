#!/usr/bin/env bash
# Fail fast — BEFORE the deploy job's 10-minute pre-deploy sleep — if either
# KNOCK_APPROVER_TOKEN or PHALA_API_KEY is stale or invalid.
#
# Why this exists: the v0.7.03 redeploy burned two ~17-min cycles discovering two
# stale GH secrets serially (KNOCK in the heads-up step, then PHALA in phala deploy).
# This step runs both checks up front, in ONE run, and names EVERY bad secret so two
# stale secrets cost one cycle to discover — not two. See issue #43.
#
# Never prints secret values: only HTTP status codes and pass/fail lines.
#
# Fail-closed: if a check is indeterminate (e.g. the homeserver or Phala Cloud is
# unreachable), the deploy still aborts — but the message says "could not validate"
# rather than falsely claiming the secret is stale, so the operator isn't misled into
# rotating a good token. A definitive auth failure (401/403 or a rejected API key) is
# reported as stale.
#
# Required env (set by the workflow step from GitHub secrets):
#   KNOCK_APPROVER_TOKEN  access token of @shape-rotator-2 (must whoami 200)
#   PHALA_API_KEY         phala CLI api token (must be able to read dstack-matrix)
set -uo pipefail

failures=0

# 1. KNOCK_APPROVER_TOKEN must authenticate as a valid account.
#    200 = good; 401/403 = stale/invalid; anything else (000 timeout, 5xx, ...) =
#    indeterminate — fail closed but don't claim the token is stale.
whoami_code=$(curl -sS -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer ${KNOCK_APPROVER_TOKEN:-}" \
  https://mtrx.shaperotator.xyz/_matrix/client/v3/account/whoami || true)
case "$whoami_code" in
  200)
    echo "KNOCK_APPROVER_TOKEN: whoami 200 OK"
    ;;
  401|403)
    echo "::error::KNOCK_APPROVER_TOKEN is stale or invalid — whoami returned ${whoami_code} (expected 200)."
    echo "::error::Rotate it: ssh the CVM, read the persisted bot token, then 'gh secret set KNOCK_APPROVER_TOKEN'."
    failures=$((failures + 1))
    ;;
  *)
    echo "::error::KNOCK_APPROVER_TOKEN could not be validated — whoami returned ${whoami_code} (expected 200)."
    echo "::error::This is probably NOT a stale token: is mtrx.shaperotator.xyz reachable from the runner? Deploy aborted (fail-closed)."
    failures=$((failures + 1))
    ;;
esac

# 2. PHALA_API_KEY must be able to read the target CVM.
#    phala exits nonzero on a rejected key ("Invalid API key"), but also on a
#    renamed/removed CVM or a Phala Cloud outage — capture stderr to distinguish.
# No `set -e`, so a failing phala won't abort; capture its real rc + stderr.
# `2>&1 >/dev/null` inside $() captures STDERR (discards stdout) into phala_err.
phala_err=$(phala cvms get dstack-matrix --api-token "${PHALA_API_KEY:-}" 2>&1 >/dev/null)
phala_rc=$?
if [ "$phala_rc" -eq 0 ]; then
  echo "PHALA_API_KEY: can read dstack-matrix"
else
  if printf '%s' "$phala_err" | grep -qi 'invalid api key'; then
    echo "::error::PHALA_API_KEY is stale or invalid — 'phala cvms get dstack-matrix' rejected the key."
    echo "::error::Rotate it at cloud.phala.com → API keys, then 'gh secret set PHALA_API_KEY'."
  else
    echo "::error::PHALA_API_KEY could not be validated — 'phala cvms get dstack-matrix' failed (rc=${phala_rc})."
    echo "::error::This may not be a stale key (CVM renamed/removed, or Phala Cloud unreachable): ${phala_err}. Deploy aborted (fail-closed)."
  fi
  failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
  echo "::error::validate-creds: ${failures} check(s) failed — deploy aborted before the pre-deploy sleep."
  exit 1
fi

echo "creds validated"
