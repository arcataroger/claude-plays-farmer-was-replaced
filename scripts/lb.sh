#!/usr/bin/env bash
# Runs one leaderboard: lb.sh BOARD PROGRAM_FILE [SPEEDUP] [MAX_SECONDS]
# A wrapper around lb.py beside it, which does the work and also runs on Windows.
here="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$here/lb.py" "$@"
