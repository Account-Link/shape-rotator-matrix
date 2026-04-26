#!/usr/bin/env bash
# Host-side wrapper that runs the full e2e stack.
#
# What it does:
#   1. Brings up continuwuity by itself.
#   2. Polls the container logs for the one-time bootstrap registration
#      token continuwuity prints on first boot.
#   3. Brings up the rest of the stack with that token in env, so the
#      bootstrap container (which has no docker-socket access) can use it.
#   4. Streams logs and exits with the test-runner's exit code.
#
# CI runs this. Dev can also run it: `bash tests/run_e2e.sh`.
set -euo pipefail

cd "$(dirname "$0")"

COMPOSE=(docker compose -f docker-compose.test.yml -p shape-rotator-e2e)

cleanup() {
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "[run_e2e] FAIL — capturing service logs before teardown" >&2
    for svc in continuwuity bootstrap knock-approver landing; do
      echo "=== $svc ===" >&2
      "${COMPOSE[@]}" logs --no-color --tail=100 "$svc" >&2 2>&1 || true
    done
  fi
  echo "[run_e2e] tearing down stack" >&2
  "${COMPOSE[@]}" down -v --remove-orphans >&2 || true
}
trap cleanup EXIT

# Fresh start so the bootstrap-token dance is reproducible.
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true

echo "[run_e2e] starting continuwuity" >&2
"${COMPOSE[@]}" up -d --build continuwuity

echo "[run_e2e] waiting for bootstrap token to appear in continuwuity logs" >&2
TOKEN=""
for i in $(seq 1 60); do
  # continuwuity wraps the token in ANSI color codes even when --no-color is
  # passed (the codes come from the container's own output, not compose).
  # Strip them with sed before grepping.
  TOKEN=$(
    "${COMPOSE[@]}" logs --no-color continuwuity 2>&1 \
      | sed -E 's/\x1b\[[0-9;]*m//g' \
      | grep -oE 'using the registration token [A-Za-z0-9]+' \
      | awk '{print $NF}' \
      | head -n1 \
      || true
  )
  if [ -n "$TOKEN" ]; then break; fi
  sleep 1
done
if [ -z "$TOKEN" ]; then
  echo "[run_e2e] FAIL: never saw a bootstrap token. Recent logs:" >&2
  "${COMPOSE[@]}" logs --tail=80 continuwuity >&2 || true
  exit 1
fi
echo "[run_e2e] bootstrap token: $TOKEN" >&2

export CONDUWUIT_BOOTSTRAP_TOKEN="$TOKEN"

echo "[run_e2e] building all images" >&2
"${COMPOSE[@]}" build

echo "[run_e2e] bringing up bootstrap + approver + landing" >&2
# `up -d` blocks here until bootstrap exits successfully, because
# knock-approver depends_on bootstrap with service_completed_successfully.
# If bootstrap fails, `up -d` returns nonzero and we exit.
if ! "${COMPOSE[@]}" up -d bootstrap knock-approver landing; then
  echo "[run_e2e] FAIL: bring-up failed" >&2
  "${COMPOSE[@]}" logs --tail=80 bootstrap >&2 || true
  exit 1
fi

echo "[run_e2e] running test-runner" >&2
# `run --rm` blocks foreground, returns the runner's own exit code, and
# leaves the rest of the stack up so logs from a failure are inspectable.
"${COMPOSE[@]}" run --rm test-runner bash tests/run_in_runner.sh
