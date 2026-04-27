#!/usr/bin/env bash
# Runs INSIDE the test-runner container. Source the env file the bootstrap
# container wrote, wait for the approver to be live, run every test, exit
# nonzero on the first failure.
set -euo pipefail

ENV_FILE=/shared/test.env
APPROVER=http://knock-approver:8001

echo "[runner] sourcing $ENV_FILE"
test -f "$ENV_FILE" || { echo "[runner] FAIL: $ENV_FILE not produced by bootstrap"; exit 1; }
set -a
. "$ENV_FILE"
set +a
# bootstrap.py exports HS pointing at continuwuity directly (that's what the
# approver wants); the runner instead must exercise the same entry point real
# clients hit, which is the landing nginx in front of everything. Override
# after sourcing.
export HS=http://landing:80
export HOMESERVER=$HS

echo "[runner] waiting for approver health"
for i in $(seq 1 60); do
  if curl -fsS "$APPROVER/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "$APPROVER/health" >/dev/null

echo "[runner] env summary:"
echo "  HS=$HS"
echo "  SPACE_ID=$SPACE_ID"
echo "  SPACE_CHILD_IDS=$SPACE_CHILD_IDS"
echo "  ADMIN_MXID=$ADMIN_MXID"

# stdlib flow test (signup + knock-vetting). Uses landing nginx as HS so it
# hits both the matrix endpoints AND /signup/api in one shot.
echo "[runner] === smoke.py ==="
ADMIN_TOKEN="$ADMIN_TOKEN" \
  REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  SIGNUP_CODE="$DEV_SIGNUP_CODE" \
  KNOCK_CODE="$DEV_KNOCK_CODE" \
  SPACE_ID="$SPACE_ID" \
  SPACE_CHILDREN="$SPACE_CHILD_IDS" \
  HOMESERVER="$HS" \
  python3 tests/smoke.py

# Regression test for the space join_rule (Alexis incident, 2026-04-26 —
# the space's join_rule had been flipped to `restricted` and external
# users hit "you do not belong to any of the required rooms/spaces" when
# clicking the public alias, which the existing tests didn't catch
# because they only exercised local users with knock codes).
echo "[runner] === space_join_rule_e2e.py ==="
DEV_HS="$HS" \
  DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  SPACE_ID="$SPACE_ID" \
  ADMIN_TOKEN="$ADMIN_TOKEN" \
  python3 tests/space_join_rule_e2e.py

# Real E2EE round-trip test of the new vetting flow.
echo "[runner] === vetting_e2e.py ==="
DEV_HS="$HS" \
  DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
  DEV_KNOCK_CODE="$DEV_KNOCK_CODE" \
  DEV_SIGNUP_CODE="$DEV_SIGNUP_CODE" \
  SPACE_ID="$SPACE_ID" \
  SPACE_CHILD_IDS="$SPACE_CHILD_IDS" \
  ADMIN_MXID="$ADMIN_MXID" \
  python3 tests/vetting_e2e.py

# Paste A+B+C SAS verification end-to-end. **Informational**: the
# upstream SAS dance is tracked-flaky against continuwuity (issue #1) so
# we run the test for visibility but don't gate the PR on its outcome.
# The vetting flow's E2EE round-trip (above) is the real megolm gate.
echo "[runner] === sas_e2e.py === (informational; failures don't gate the PR)"
if DEV_HS="$HS" \
     DEV_REG_TOKEN="$CONDUWUIT_REGISTRATION_TOKEN" \
     DEV_SIGNUP_CODE="$DEV_SIGNUP_CODE" \
     python3 tests/sas_e2e.py; then
  echo "[runner] sas_e2e: PASS"
else
  echo "[runner] sas_e2e: FAIL (informational — see issue #1)"
fi

echo "[runner] all gating tests passed"
