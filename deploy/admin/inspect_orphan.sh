#!/usr/bin/env bash
# Read-only diagnostic: SSH into the dstack-matrix CVM and dump enough
# state to make the cleanup decision for tapp-continuwuity-1.
# Doesn't touch any container. Doesn't read credentials. Pure inspect.
set -uo pipefail

KEY=/home/amiller/projects/dstack/shape-rotator-matrix/deploy/deploy_key
CVM=dstack-matrix

phala ssh "$CVM" -- -i "$KEY" bash -c '
  set +e
  echo "== ps =="
  docker ps -a --format "{{.Names}}\t{{.Status}}\t{{.Image}}" | grep continuwuity
  echo
  echo "== tapp-continuwuity-1 mounts =="
  docker inspect tapp-continuwuity-1 --format "{{range .Mounts}}{{.Type}}: {{.Source}} -> {{.Destination}} (rw={{.RW}}){{println}}{{end}}"
  echo "== dstack-continuwuity-1 mounts =="
  docker inspect dstack-continuwuity-1 --format "{{range .Mounts}}{{.Type}}: {{.Source}} -> {{.Destination}} (rw={{.RW}}){{println}}{{end}}"
  echo
  echo "== last 20 lines from tapp-continuwuity-1 =="
  docker logs --tail=20 tapp-continuwuity-1 2>&1
  echo
  echo "== docker volume ls (continuwuity-related only) =="
  docker volume ls | grep -i continuwuity
'
