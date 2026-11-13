"""
HIL tests for Adafruit Wippersnapper firmware's deep sleep mode.
"""

import re

import pytest

import sleep_helpers as sh
from sleep_helpers import (CHECKIN_TIMEOUT, DISCONNECT_TIMEOUT,
                           SLEEP_TOLERANCE_S, TIMER_S, WAKE_TIMEOUT, log)

# ws_sleep_SleepMode_S_DEEP, as the wake's "Prv. Sleep Mode" line prints it
S_DEEP = 2
# The runtime SleepConfig the broker sends, and the wake checkin repeats
SLEEP_CONFIG = {"mode": "S_DEEP", "timer": {"duration": TIMER_S}}
# The boot ROM's reset line on a deep sleep wake, the first line the DUT
# prints when the timer fires: "rst:0x5 (DEEPSLEEP_RESET),boot:..." on an
# ESP32, "rst:0x5 (DSLEEP),boot:..." on the S2/S3/C3
DEEP_SLEEP_RESET_RE = r"^rst:0x5 \((DEEPSLEEP_RESET|DSLEEP)\)"
# The wake's first boot lines, ending a sleep watch
WAKE_RE = re.compile(DEEP_SLEEP_RESET_RE + r"|^ESP Reset Reason: ")
# The wake's checkin: the timer, then a boot, WiFi and the broker
WAKE_CHECKIN_TIMEOUT = TIMER_S + SLEEP_TOLERANCE_S + 150
# How many times the timer wakes the DUT and it goes back to sleep: once
# in the basic test, 20 times (21 sleeps) in the slow soak test
BASIC_WAKE_CYCLES = 1
SOAK_WAKE_CYCLES = 20


def sleep_wake_cycle(board, serial, protomq, uid, board_definition,
                     wake_response, cycle, slept_at, rows, misses):
    """One deep sleep, from slept_at (its "Entering deep sleep" line's
    time): the timer wakes the DUT, it checks in, and the broker's
    sleepEnabled puts it back to sleep. Appends the cycle's row to rows
    and any timing miss to misses. Returns the next sleep's slept_at."""
    goodnight_at = protomq.last_d2b_time(uid)
    # Through the whole sleep, to log the USB/serial/broker timeline
    sh.assert_dropped(uid, sh.watch_sleep(board, serial, protomq, uid,
                                          WAKE_TIMEOUT, wake_re=WAKE_RE))

    # The timer wakes it, a reset into a fresh boot, when it was due: from
    # "Entering deep sleep" to the boot ROM's deep sleep reset line
    woke_at = sh.line_time(serial, serial.expect(DEEP_SLEEP_RESET_RE,
                                                 timeout=WAKE_TIMEOUT))
    asleep = woke_at - slept_at
    serial.expect(r"^Sleep Wakeup Reason: Timer$", timeout=10)
    serial.expect(r"^Prv\. Sleep Mode: %d$" % S_DEEP, timeout=5)
    reported = int(serial.expect(r"^Total Sleep Duration \(sec\): (\d+)$",
                                 timeout=5).group(1))
    rows.append((cycle, asleep, reported))
    log.info("cycle %d: DUT woke %.3f s after entering deep sleep, expected "
             "%d s (%+.3f s); firmware reports %d s", cycle, asleep, TIMER_S,
             asleep - TIMER_S, reported)
    sh.check_wake_timing(cycle, asleep, reported, misses)

    # It checks in again, and the broker re-enables sleep
    checkin_at = sh.expect_wake_checkin(protomq, uid, board_definition,
                                        wake_response, WAKE_CHECKIN_TIMEOUT)
    # For reference: the wake's boot, WiFi and broker connect on top
    if goodnight_at is not None and checkin_at is not None:
        log.info("cycle %d: goodnight to wake checkin: %.1f s (%.1f s after "
                 "the wake)", cycle, checkin_at - goodnight_at,
                 checkin_at - woke_at)

    # ...and goes back to sleep
    serial.expect(r"^\[app\] Processing sleep configuration from checkin$",
                  timeout=CHECKIN_TIMEOUT)
    return sh.expect_goodnight_and_sleep(serial, protomq, uid, "deep")


def run_sleep_wake_cycles(board, serial, protomq, checkin, board_definition,
                          cycles):
    """Check in with no sleep fields, then send the runtime SleepConfig:
    the DUT sleeps, and the timer wakes it cycles times, each wake checking
    in, the broker re-enabling sleep, and the DUT sleeping again. Timing
    misses are collected so every cycle is measured, and logged with the
    wake table; a missing step fails at once."""
    # --- Arrange ------------------------------------------------------
    # A regular checkin, no sleep fields
    uid = checkin()
    wake_response = sh.enable_sleep_on_checkin(protomq, board_definition,
                                               SLEEP_CONFIG)

    # --- Act ----------------------------------------------------------
    protomq.publish(uid, {"sleep": {"sleepConfig": SLEEP_CONFIG}})

    # --- Assert -------------------------------------------------------
    protomq.expect_b2d_message(uid, "sleep.sleepConfig", match=SLEEP_CONFIG,
                               timeout=5)
    # 1. The runtime SleepConfig puts the DUT to sleep
    slept_at = sh.expect_goodnight_and_sleep(serial, protomq, uid, "deep")

    # 2. It then cycles: wake, checkin, sleep again
    rows = []
    misses = []
    try:
        for cycle in range(1, cycles + 1):
            slept_at = sleep_wake_cycle(board, serial, protomq, uid,
                                        board_definition, wake_response,
                                        cycle, slept_at, rows, misses)
        # The last sleep: the broker still sees it go
        sh.assert_dropped(uid, sh.watch_sleep(board, serial, protomq, uid,
                                              DISCONNECT_TIMEOUT))
    finally:
        sh.log_wake_table("deep sleep wakes", rows)
    assert not misses, "\n".join(misses)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_deep_timer_runtime(board, secrets_name, flashed_board, serial,
                                  protomq, checkin, board_definition):
    """One deep sleep/wake cycle: the runtime SleepConfig puts the DUT to
    sleep, the timer wakes it, it checks in, and sleeps again."""
    run_sleep_wake_cycles(board, serial, protomq, checkin, board_definition,
                          BASIC_WAKE_CYCLES)


# SOAK_WAKE_CYCLES of ~50 s each, past the suite's --timeout=600
@pytest.mark.slow
@pytest.mark.timeout(SOAK_WAKE_CYCLES * (WAKE_CHECKIN_TIMEOUT + 60) + 300)
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_deep_timer_soak(board, secrets_name, flashed_board, serial,
                               protomq, checkin, board_definition):
    """20 (SOAK_WAKE_CYCLES) sleep/wake cycles in a row, for the wake timing's
    spread and the cycle's reliability."""
    run_sleep_wake_cycles(board, serial, protomq, checkin, board_definition,
                          SOAK_WAKE_CYCLES)
