#!/usr/bin/env bash
# Run the test suite at several pinned clock starts, so no test can depend on when CI happens to run.
#
# Every test runs on the project clock (tests/conftest.py, _pinned_clock), which starts at
# MAVIS_TEST_NOW and ticks with real elapsed time. This script runs the full suite once per start below:
# quiet hours, just past midnight (the ambiguous "tomorrow" window), a weekend, both DST changes and a year
# end in a far zone. Runs are sequential by default; set PARALLEL=1 to run them at once (faster, but the
# machine load can upset timing-sensitive tests).
#
#   scripts/test_clock_matrix.sh                 # all starts
#   scripts/test_clock_matrix.sh 2026-10-06T23:30:00+05:30 -- -x tests/agents   # one start, extra args
set -u
cd "$(dirname "$0")/.."

DEFAULT_STARTS=(
  "2026-10-06T23:30:00+05:30"   # quiet hours, Asia/Kolkata
  "2026-10-07T00:30:00+05:30"   # just past midnight
  "2026-10-10T10:00:00+05:30"   # Saturday
  "2026-11-01T01:30:00-04:00"   # New York falls back
  "2027-03-28T00:30:00+00:00"   # London springs forward
  "2026-12-31T23:59:00+13:00"   # year end, Pacific/Auckland
)

starts=()
while [ $# -gt 0 ] && [ "$1" != "--" ]; do starts+=("$1"); shift; done
[ "${1:-}" = "--" ] && shift
[ ${#starts[@]} -eq 0 ] && starts=("${DEFAULT_STARTS[@]}")
extra=("$@")

run() {
  echo "== MAVIS_TEST_NOW=$1"
  MAVIS_TEST_NOW="$1" uv run pytest -q -p no:cacheprovider "${extra[@]}" 2>&1 | tail -n 15
  return "${PIPESTATUS[0]}"
}

status=0
if [ "${PARALLEL:-0}" = "1" ]; then
  pids=()
  for t in "${starts[@]}"; do run "$t" > "/tmp/clock-matrix-${t//[:+]/_}.log" 2>&1 & pids+=($!); done
  for i in "${!pids[@]}"; do
    wait "${pids[$i]}" || status=1
    cat "/tmp/clock-matrix-${starts[$i]//[:+]/_}.log"
  done
else
  for t in "${starts[@]}"; do run "$t" || status=1; done
fi
[ $status -eq 0 ] && echo "clock matrix: all starts passed" || echo "clock matrix: FAILED at one or more starts"
exit $status
