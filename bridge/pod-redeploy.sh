#!/usr/bin/env bash
# bridge/pod-redeploy.sh — force a tee-daemon tenant to (re)build its container.
#
#   bash bridge/pod-redeploy.sh [project] [CVM]
#
# A POST /_api/projects that returns 201 only REGISTERS the project; an empty
# container_id means nothing was ever started. /redeploy is what rebuilds it.
# Per tee-daemon/deploy/README.md a redeploy briefly 500s during the rebuild
# and then serves 200 -- so this polls rather than judging the first response.
set -euo pipefail

NAME="${1:-mx-tg-relay}"
CVM="${2:-${CVM:-https://pod.dstack.soc1024.com}}"

SECRETS_ENV="$HOME/.oauth3-prod-secrets.env"
TOKEN="${TEE_DAEMON_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f "$SECRETS_ENV" ]; then
  TOKEN="$(grep -m1 '^TEE_DAEMON_TOKEN=' "$SECRETS_ENV" | cut -d= -f2-)"
fi
[ -n "$TOKEN" ] || { echo "pod-redeploy: no TEE_DAEMON_TOKEN" >&2; exit 1; }

echo "=== POST /_api/projects/$NAME/redeploy ==="
curl -s -m 120 -o /tmp/pod-redeploy.json -w 'HTTP %{http_code}\n' \
  -X POST "$CVM/_api/projects/$NAME/redeploy" -H "Authorization: Bearer $TOKEN"
# Response echoes the manifest, which contains deploy-time secrets in plaintext.
python3 - /tmp/pod-redeploy.json <<'PY' 2>/dev/null || head -c 200 /tmp/pod-redeploy.json
import json, sys
d = json.load(open(sys.argv[1]))
if isinstance(d, dict):
    for k in ("name", "oci_runtime", "container_id", "image_digest", "error", "detail"):
        if k in d:
            print("  %-14s %s" % (k, str(d[k])[:90]))
    if d.get("env"):
        print("  %-14s <%d vars, redacted>" % ("env", len(d["env"])))
PY

echo
echo "=== polling health ==="
for i in 1 2 3 4 5 6; do
  code="$(curl -s -m 15 -o /tmp/pod-health.json -w '%{http_code}' "$CVM/$NAME/health" || true)"
  echo "  t+$((i*10))s: HTTP $code $(head -c 150 /tmp/pod-health.json 2>/dev/null | tr -d '\n')"
  [ "$code" = "200" ] && break
  sleep 10
done
