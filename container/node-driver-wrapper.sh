#!/bin/bash
# Playwright node driver wrapper (installed over playwright/driver/node
# by container/browser.dockerfile; the real binary sits beside it as
# node.real).
#
# The driver exits silently on some failures: it prints nothing to
# stderr (which playwright-python inherits into the worker log), leaves
# no kernel trace (no OOM event, no segfault line) and playwright drops
# the process handle. This wrapper tees the driver's stderr into a log
# file AND records every exit with its code, so the reason survives:
#   rc=0   clean exit (playwright asked it to stop)
#   rc=1   error -- stack trace lands in the log
#   rc=137 SIGKILL (find the killer)
#   rc=143 SIGTERM (who sent it)
# Node diagnostic reports (--report-on-fatalerror /
# --report-uncaught-exception via NODE_OPTIONS) land in
# $NODE_REPORT_DIR next to it.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG=/tmp/node-driver.log
mkdir -p "${NODE_REPORT_DIR:-/tmp/node-reports}"
echo "$(date -u '+%FT%TZ') driver start: $*" >> "$LOG"
"$DIR/node.real" "$@" 2> >(exec tee -a "$LOG" >&2)
rc=$?
echo "$(date -u '+%FT%TZ') driver exit rc=$rc" >> "$LOG"
exit $rc
