#!/usr/bin/env bash
# Rebuilds .env with the current base64-encoded landing pages + approver script.
# Preserves secret env vars already present in .env; just refreshes the *_B64 entries.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "Created .env from .env.example — fill in the secrets, then re-run."
    exit 0
  else
    echo "No .env or .env.example found" >&2
    exit 1
  fi
fi

INDEX_B64=$(base64 -w0 landing/index.html)
JOIN_B64=$(base64 -w0 landing/join.html)
NGINX_CONF_B64=$(base64 -w0 landing/nginx.conf)
APPROVER_B64=$(base64 -w0 knock-approver/approver.py)

python3 - <<PYEOF
from pathlib import Path
p = Path(".env")
updates = {
    "INDEX_B64":      """$INDEX_B64""",
    "JOIN_B64":       """$JOIN_B64""",
    "NGINX_CONF_B64": """$NGINX_CONF_B64""",
    "APPROVER_B64":   """$APPROVER_B64""",
}
lines, seen = [], set()
for l in p.read_text().splitlines():
    k = l.split("=", 1)[0] if "=" in l else None
    if k in updates:
        lines.append(f"{k}={updates[k]}")
        seen.add(k)
    else:
        lines.append(l)
for k, v in updates.items():
    if k not in seen:
        lines.append(f"{k}={v}")
p.write_text("\n".join(lines) + "\n")
print("Updated .env with fresh *_B64 values.")
PYEOF
