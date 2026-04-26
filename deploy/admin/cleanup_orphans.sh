#!/usr/bin/env bash
# Remove the tapp-continuwuity-1 orphan from the dstack-matrix CVM.
# Container only — leaves tapp_continuwuity-data volume in place so this
# is reversible. Run a separate `docker volume rm tapp_continuwuity-data`
# once you're sure the volume isn't needed.
set -euo pipefail

KEY=/home/amiller/projects/dstack/shape-rotator-matrix/deploy/deploy_key
CVM=dstack-matrix
TARGET=tapp-continuwuity-1

phala ssh "$CVM" -- -i "$KEY" bash -c "
  set -e
  if ! docker inspect $TARGET >/dev/null 2>&1; then
    echo 'no container named $TARGET — nothing to clean up'
    exit 0
  fi
  echo 'before:'
  docker ps -a --format '  {{.Names}}\t{{.Status}}' | grep continuwuity || true
  echo 'removing $TARGET...'
  docker rm -f $TARGET
  echo 'after:'
  docker ps -a --format '  {{.Names}}\t{{.Status}}' | grep continuwuity || true
  echo 'volumes still present (intentionally preserved):'
  docker volume ls | grep continuwuity || true
"
