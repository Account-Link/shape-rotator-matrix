#!/usr/bin/env bash
# bridge/pod-probe.sh — what a tee-daemon pod can actually run.
#
#   bash bridge/pod-probe.sh [CVM_URL]
#
# Reports the daemon's configured OCI runtime and its existing projects, so an
# `image` runtime deploy can be aimed at a pod that supports it. Reads the
# token the same way deploy-pod.sh does; prints no secrets.
set -euo pipefail

CVM="${1:-${CVM:-https://pod.dstack.soc1024.com}}"
SECRETS_ENV="$HOME/.oauth3-prod-secrets.env"
# TOKEN_FILE lets a caller aim a specific stored token at a specific pod --
# the tokens are per-instance and the wrong one gives 403 on write while still
# passing read-only GETs, which is confusing to diagnose by hand.
TOKEN="${TEE_DAEMON_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -n "${TOKEN_FILE:-}" ] && [ -f "$TOKEN_FILE" ]; then
  case "$TOKEN_FILE" in
    *.env) TOKEN="$(grep -m1 '^TEE_DAEMON_TOKEN=' "$TOKEN_FILE" | cut -d= -f2-)" ;;
    *)     TOKEN="$(tr -d '[:space:]' <"$TOKEN_FILE")" ;;
  esac
fi
if [ -z "$TOKEN" ] && [ -f "$SECRETS_ENV" ]; then
  TOKEN="$(grep -m1 '^TEE_DAEMON_TOKEN=' "$SECRETS_ENV" | cut -d= -f2-)"
fi
[ -n "$TOKEN" ] || { echo "pod-probe: no TEE_DAEMON_TOKEN" >&2; exit 1; }

echo "=== $CVM /_api/substrate ==="
curl -s -m 20 "$CVM/_api/substrate" -H "Authorization: Bearer $TOKEN" | head -c 800; echo

echo
echo "=== projects (name / runtime / mode) ==="
curl -s -m 20 "$CVM/_api/projects" -H "Authorization: Bearer $TOKEN" > /tmp/pod-projects.json
python3 - /tmp/pod-projects.json <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
rows = d if isinstance(d, list) else d.get("projects", d)
if isinstance(rows, dict):
    rows = [dict(v, name=k) for k, v in rows.items()]
for p in rows:
    print("  %-24s %-10s %s" % (p.get("name"), p.get("runtime"), p.get("mode", "")))
img = [p for p in rows if p.get("runtime") == "image"]
print("\n  image-runtime tenants here: %d" % len(img))
for p in img:
    print("    %s: %s" % (p.get("name"), str(p.get("image", ""))[:70]))
PY2
