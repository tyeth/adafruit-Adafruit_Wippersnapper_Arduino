"""
HIL tests for Adafruit Wippersnapper firmware's light sleep mode.
"""

import re

import pytest

import sleep_helpers as sh
from sleep_helpers import (CHECKIN_TIMEOUT, DISCONNECT_TIMEOUT, TIMER_S,
                           WAKE_TIMEOUT, log)

# The runtime SleepConfig the broker sends, and every wake checkin repeats
SLEEP_CONFIG = {"mode": "S_LIGHT", "timer": {"duration": TIMER_S}}
# WakeFromLightSleep()'s first line, once it has restarted Serial. The
# line before it, "*** WOKE FROM LIGHT SLEEP ***", prints while Serial is
# still ended, so it doesn't reach the host. StartSleep()'s Serial.end()
# puts a NUL on the line as the DUT sleeps, which starts this line on the
# host: it reads "\0[sleep] Woke up...", stamped with the NUL's arrival,
# when the DUT went to sleep, not when it woke.
WAKE_RE = re.compile(r"^\x00?\[sleep\] Woke up from light sleep!$")
# The first line after the wake line, whose first byte is sent after the
# wake: its arrival times the wake (Serial.begin() and a few ms late)
WOKE_TIME_RE = r"^Slept for (\d+) seconds$"
# A reset while asleep: the boot ROM's reset line, or the firmware's boot
# info. A light sleep wake resumes where it slept, so it prints neither.
RESET_RE = re.compile(r"^rst:0x|^ESP Reset Reason: ")
# The light sleep cycle count the first wake logs: the boot counts as
# cycle 1 (SleepHardware's constructor), so the first wake is cycle 2
FIRST_WAKE_SLEEP_CYCLE = 2
# The wake's checkin: WiFi and the broker again, no boot
WAKE_CHECKIN_TIMEOUT = 120
# How many times the timer wakes the DUT and it goes back to sleep: 3 in
# the basic test (one applied wake checkin, two skipped), 20 (21 sleeps)
# in the slow soak test
BASIC_WAKE_CYCLES = 3
SOAK_WAKE_CYCLES = 20


def sleep_wake_cycle(board, serial, protomq, uid, board_definition,
                     wake_response, cycle, slept_at, rows, misses):
    """One light sleep, from slept_at (its "Starting light sleep NOW..."
    line's time): the timer wakes the DUT where it slept, it reconnects,
    checks in, and sleeps again. The first wake's checkin response (now
    with sleepEnabled) differs from the boot's, so the DUT applies it; it
    skips every later one, unchanged. Appends the cycle's row to rows and
    any miss to misses. Returns the next sleep's slept_at."""
    asleep_from = serial.mark()
    # Through the whole sleep, to log the USB/serial/broker timeline
    sh.assert_dropped(uid, sh.watch_sleep(board, serial, protomq, uid,
                                          WAKE_TIMEOUT, wake_re=WAKE_RE))

    # The timer wakes it when it was due: from its last line before
    # sleeping to its first line sent wholly after (see WOKE_TIME_RE)
    serial.expect(WAKE_RE.pattern, timeout=WAKE_TIMEOUT)
    slept_for = serial.expect(WOKE_TIME_RE, timeout=5)
    woke_at = sh.line_time(serial, slept_for)
    asleep = woke_at - slept_at
    reported = int(slept_for.group(1))
    # ...where it slept, not through a reset
    resets = [l.text for l in serial.lines(asleep_from)
              if l.time <= woke_at and RESET_RE.search(l.text)]
    assert not resets, (
        f"cycle {cycle}: DUT reset during light sleep: {resets}")
    sleep_cycle = int(serial.expect(r"^Total light sleep cycles: (\d+)$",
                                    timeout=5).group(1))
    log.info("cycle %d: DUT woke %.3f s after entering light sleep, expected "
             "%d s (%+.3f s); firmware reports %d s, light sleep cycle %d",
             cycle, asleep, TIMER_S, asleep - TIMER_S, reported, sleep_cycle)
    sh.check_wake_timing(cycle, asleep, reported, misses)
    expected_cycle = FIRST_WAKE_SLEEP_CYCLE + cycle - 1
    if sleep_cycle != expected_cycle:
        misses.append(f"cycle {cycle}: firmware logs light sleep cycle "
                      f"{sleep_cycle}, expected {expected_cycle}")

    # It reconnects and checks in
    serial.expect(r"^\[app\] Reconnecting network after light sleep "
                  r"wake\.\.\.$", timeout=10)
    serial.expect(r"^Connected to WiFi!$", timeout=WAKE_CHECKIN_TIMEOUT)
    serial.expect(r"^\[app\] Checking in after light sleep wake\.\.\.$",
                  timeout=WAKE_CHECKIN_TIMEOUT)
    sh.expect_wake_checkin(protomq, uid, board_definition, wake_response,
                           WAKE_CHECKIN_TIMEOUT)

    # The first wake applies the response, with sleepEnabled and the
    # SleepConfig now; every later one is unchanged, so skipped
    changed = cycle == 1
    if changed:
        serial.expect(r"^\[checkin\] Response is new or changed$",
                      timeout=CHECKIN_TIMEOUT)
        serial.expect(r"^\[app\] Processing sleep configuration from "
                      r"checkin$", timeout=CHECKIN_TIMEOUT)
        # Applying it re-arms the timer, within the checkin
        serial.expect(r"^\[sleep\] Timer wakeup set to %d seconds$" % TIMER_S,
                      timeout=5)
        serial.expect(r"^Sleep Enabled: YES$", timeout=5)
    else:
        serial.expect(r"^\[checkin\] Response unchanged since last checkin$",
                      timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^Completed checkin process!$", timeout=CHECKIN_TIMEOUT)
    if not changed:
        # Since this sleep began: the runtime config's is before it
        applied = [l.text for l in serial.lines(asleep_from)
                   if "Processing sleep configuration" in l.text]
        assert not applied, (
            f"cycle {cycle}: DUT applied an unchanged wake checkin: {applied}")
    rows.append((cycle, asleep, reported, sleep_cycle,
                 "changed" if changed else "unchanged"))

    # ...and sleeps again: run() stays in loopSleep(), and the timer was
    # re-armed, if at all, within the checkin above
    return sh.expect_goodnight_and_sleep(serial, protomq, uid, "light",
                                         configured=False,
                                         entering_loop=False)


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
    slept_at = sh.expect_goodnight_and_sleep(serial, protomq, uid, "light")

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
        sh.log_wake_table("light sleep wakes", rows,
                          extra_headers=("sleep cycle #",
                                         "checkin response"))
    assert not misses, "\n".join(misses)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_light_timer_runtime(board, secrets_name, flashed_board, serial,
                                  protomq, checkin, board_definition):
    """Three light sleep/wake cycles: the runtime SleepConfig puts the DUT
    to sleep, the timer wakes it where it slept, it checks in (the first
    response applied, the next two skipped, unchanged), and sleeps again."""
    run_sleep_wake_cycles(board, serial, protomq, checkin, board_definition,
                          BASIC_WAKE_CYCLES)


# SOAK_WAKE_CYCLES of ~40 s each, past the suite's --timeout=600
@pytest.mark.slow
@pytest.mark.timeout(SOAK_WAKE_CYCLES * (WAKE_CHECKIN_TIMEOUT + 60) + 300)
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_light_timer_soak(board, secrets_name, flashed_board, serial,
                               protomq, checkin, board_definition):
    """20 (SOAK_WAKE_CYCLES) sleep/wake cycles in a row, for the wake timing's
    spread and the cycle's reliability."""
    run_sleep_wake_cycles(board, serial, protomq, checkin, board_definition,
                          SOAK_WAKE_CYCLES)
