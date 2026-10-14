#!/usr/bin/env bash
# BUI-1267: the every-3-days job (com.comics.ebay-shipped-submit) runs both
# unattended halves of the post-win workflow:
#
#   1. ebay-shipped --auto-submit   shipped orders -> EZShip (BUI-1238)
#   2. gixen record-wins            new Gixen wins -> the collection
#
# Each step runs whatever the other did, and each pings its own heartbeat
# (ebay-shipped-submit, record-wins), so one failing never hides the other.
# The script exits non-zero if either step failed.
set -u

status=0

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) ebay-shipped --auto-submit"
ebay-shipped --auto-submit || { rc=$?; echo "ebay-shipped exited $rc" >&2; status=1; }

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) gixen record-wins"
gixen record-wins || { rc=$?; echo "gixen record-wins exited $rc" >&2; status=1; }

exit "$status"
