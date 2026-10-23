#!/usr/bin/env bash
# SPDX-FileCopyrightText: Brent Rubell for Adafruit Industries, 2026
#
# SPDX-License-Identifier: MIT
#
# Runs one test folder under hil/firmware (e.g. "network") against BOARD,
# for build-hil.yml, which calls this once per folder so each gets its own
# step. Expects BOARD, FIRMWARE_DIR and LOG_DIR in the environment, and runs
# from hil/. A folder the board's run_tests leaves out (pytest exit code 5,
# no tests collected) passes.
set -uo pipefail

suite="$1"
creds=~/.config/hil/creds.env
if [ -f "$creds" ]; then
  set -a; . "$creds"; set +a
else
  echo "::warning::$creds not found; tests that need credentials will fail"
fi

venv/bin/pytest "firmware/$suite" \
  --board "$BOARD" \
  --firmware-dir "$FIRMWARE_DIR" \
  --serial-log-dir "$LOG_DIR" \
  --junitxml "$LOG_DIR/junit-$suite.xml" \
  --log-cli-level=INFO
rc=$?
if [ "$rc" -eq 5 ]; then
  echo "No tests selected in firmware/$suite for $BOARD"
  exit 0
fi
exit "$rc"
