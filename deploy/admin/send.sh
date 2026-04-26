#!/usr/bin/env bash
# Wrapper that runs deploy/admin/post_message.py inside the test-runner
# image (libolm + mautrix + python-olm + everything else preinstalled).
#
# Usage:
#   bash deploy/admin/send.sh '<room>' '<body>'
#   bash deploy/admin/send.sh '<room>' --file path/to/text.md
set -euo pipefail
cd "$(dirname "$0")/../.."

IMAGE=shape-rotator-admin-runner:latest
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[send] building $IMAGE from tests/Dockerfile..." >&2
  docker build -t "$IMAGE" tests/ >&2
fi

# Bind-mount the repo rw so the persistent crypto store at
# deploy/admin/.sr2-crypto.db survives across calls.
exec docker run --rm \
  -v "$(pwd):/repo" \
  -v /tmp:/tmp:ro \
  -w /repo \
  -e PYTHONUNBUFFERED=1 \
  "$IMAGE" \
  python3 deploy/admin/post_message.py "$@"
