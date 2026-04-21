#!/usr/bin/env bash
# Hot-patch landing files + approver.py into the running CVM without a
# compose redeploy. Zero Matrix downtime; survives until the next
# `phala deploy` re-baselines from .env.
#
# Usage:
#   deploy/hot_patch.sh                      # syncs all landing + approver files
#   deploy/hot_patch.sh signup.html          # just one file (faster)
#   deploy/hot_patch.sh responder.py
#
# What it does:
#   - landing/*.html, landing/nginx.conf  → docker cp into dstack-landing-1 +
#                                           nginx -s reload
#   - landing/responder.py, landing/sas_verification.py → docker cp into
#                                           landing container (static fetches)
#   - knock-approver/approver.py          → docker cp into dstack-knock-approver-1
#                                           + container restart
#
# After hot-patching, remember to run deploy/encode_env.sh before the next
# phala deploy so .env reflects what's actually live.

set -euo pipefail
cd "$(dirname "$0")/.."

CVM_ID=${CVM_ID:-12b577ffb80492b08db079a9d0b07391fcc20529}
KEY=${KEY:-deploy/deploy_key}
SSH="phala ssh $CVM_ID -- -i $KEY -o BatchMode=yes -o LogLevel=ERROR"

# Map local path → (container, target path, post-action)
declare -A TARGETS=(
  [landing/index.html]="dstack-landing-1:/usr/share/nginx/html/index.html:reload_nginx"
  [landing/join.html]="dstack-landing-1:/usr/share/nginx/html/join.html:reload_nginx"
  [landing/signup.html]="dstack-landing-1:/usr/share/nginx/html/signup.html:reload_nginx"
  [landing/nginx.conf]="dstack-landing-1:/etc/nginx/conf.d/default.conf:reload_nginx"
  [landing/responder.py]="dstack-landing-1:/usr/share/nginx/html/responder.py:none"
  [landing/sas_verification.py]="dstack-landing-1:/usr/share/nginx/html/sas_verification.py:none"
  [knock-approver/approver.py]="dstack-knock-approver-1:/app.py:restart_approver"
)

reload_nginx() { $SSH 'docker exec dstack-landing-1 nginx -s reload' ; }
restart_approver() { $SSH 'docker restart dstack-knock-approver-1' >/dev/null ; }

patch_file() {
  local src="$1" spec="${TARGETS[$1]}"
  local container="${spec%%:*}"; local rest="${spec#*:}"
  local target="${rest%:*}";     local action="${rest##*:}"
  [ -f "$src" ] || { echo "missing: $src" >&2; return 1; }

  # Stream the local file through ssh into `docker exec ... tee`. No
  # intermediate tmp file on the CVM, no docker cp (which would need the file
  # on the CVM first).
  $SSH "docker exec -i $container tee '$target' >/dev/null" < "$src"
  echo "  patched: $src → $container:$target"
  case "$action" in
    reload_nginx)    reload_nginx      ; echo "  nginx reloaded" ;;
    restart_approver) restart_approver ; echo "  approver restarted" ;;
    none) ;;
  esac
}

post_reload_batched=0
if [ $# -eq 0 ]; then
  FILES=("${!TARGETS[@]}")
else
  FILES=()
  for arg in "$@"; do
    hit=""
    for k in "${!TARGETS[@]}"; do
      case "$k" in *"$arg") hit="$k"; break ;; esac
    done
    [ -n "$hit" ] || { echo "no target for $arg" >&2; exit 1; }
    FILES+=("$hit")
  done
fi

# Patch everything; dedupe the post-action calls so we reload nginx once even
# for a batch of 5 html files.
declare -A SEEN_ACTIONS=()
for f in "${FILES[@]}"; do
  spec="${TARGETS[$f]}"
  action="${spec##*:}"
  SEEN_ACTIONS[$action]=1
  # Push file without running post-action; we'll batch those at the end.
  local_action_skip=1
  container="${spec%%:*}"; rest="${spec#*:}"; target="${rest%:*}"
  [ -f "$f" ] || { echo "missing: $f" >&2; exit 1; }
  $SSH "docker exec -i $container tee '$target' >/dev/null" < "$f"
  echo "  patched: $f → $container:$target"
done

for a in "${!SEEN_ACTIONS[@]}"; do
  case "$a" in
    reload_nginx)    reload_nginx      ; echo "  nginx reloaded" ;;
    restart_approver) restart_approver ; echo "  approver restarted" ;;
    none) ;;
  esac
done
echo "done."
