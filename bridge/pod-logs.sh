#!/usr/bin/env bash
# bridge/pod-logs.sh — tail a tee-daemon tenant's container logs.
#
#   bash bridge/pod-logs.sh [project] [tail] [CVM]
#
# The daemon exposes GET /_api/projects/<name>/logs (ingress.py:790); docker
# exec is denied, so this is the only window into a running tenant.
set -euo pipefail

NAME="${1:-mx-tg-relay}"
TAIL="${2:-80}"
CVM="${3:-${CVM:-https://pod.dstack.soc1024.com}}"

SECRETS_ENV="$HOME/.oauth3-prod-secrets.env"
TOKEN="${TEE_DAEMON_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f "$SECRETS_ENV" ]; then
  TOKEN="$(grep -m1 '^TEE_DAEMON_TOKEN=' "$SECRETS_ENV" | cut -d= -f2-)"
fi
[ -n "$TOKEN" ] || { echo "pod-logs: no TEE_DAEMON_TOKEN" >&2; exit 1; }

echo "=== $CVM/_api/projects/$NAME (status) ==="
curl -s -m 20 "$CVM/_api/projects/$NAME" -H "Authorization: Bearer $TOKEN" \
  > /tmp/pod-proj.json 2>/dev/null || true
python3 - /tmp/pod-proj.json <<'PY' 2>/dev/null || echo "(status unavailable)"
import json, sys
d = json.load(open(sys.argv[1]))
# Never print env: the daemon returns deploy-time secrets in plaintext.
for k in ("name", "runtime", "mode", "oci_runtime", "container_id",
          "image", "image_digest", "deployed_at", "status", "state"):
    if k in d:
        print("  %-14s %s" % (k, str(d[k])[:90]))
if d.get("env"):
    print("  %-14s <%d vars, redacted>" % ("env", len(d["env"])))
PY

echo
echo "=== logs (tail $TAIL) ==="
curl -s -m 30 "$CVM/_api/projects/$NAME/logs?tail=$TAIL" -H "Authorization: Bearer $TOKEN" | tail -60
