"""
HIL tests for Adafruit Wippersnapper firmware's light sleep mode: a timer
wake (basic and slow soak), ext0 (RTC GPIO level) wakes on a pin pulled
down (wakes HIGH) or up (wakes LOW), and two I2C sensors read each wake,
woken by the timer or ext0.

A light sleep wake resumes where the DUT slept, with no reset: it
reconnects, checks in (a response unchanged since the last one it
applied is skipped), and sleeps again.
"""

import re

import pytest

import sleep_helpers as sh
from sleep_helpers import (CHECKIN_TIMEOUT, DISCONNECT_TIMEOUT, HOLD_S,
                           TIMER_S, WAKE_TIMEOUT, log)

# WakeFromLightSleep()'s first line, once it has restarted Serial. The
# line before it, "*** WOKE FROM LIGHT SLEEP ***", prints while Serial is
# still ended, so it doesn't reach the host. StartSleep()'s Serial.end()
# puts a NUL on the line as the DUT sleeps, which starts this line on the
# host: it reads "\0[sleep] Woke up...", stamped with the NUL's arrival,
# when the DUT went to sleep, not when it woke.
LIGHT_WAKE_RE = re.compile(r"^\x00?\[sleep\] Woke up from light sleep!$")
# The first line after the wake line, whose first byte is sent after the
# wake: its arrival times the wake (Serial.begin() and a few ms late)
WOKE_TIME_RE = r"^Slept for (\d+) seconds$"
# A reset while asleep: the boot ROM's reset line, or the firmware's boot
# info. A light sleep wake resumes where it slept, so it prints neither.
RESET_RE = re.compile(r"^rst:0x|^ESP Reset Reason: ")
# Any wake or reset, ending a sleep watch
WAKE_OR_RESET_RE = re.compile(LIGHT_WAKE_RE.pattern + "|" + RESET_RE.pattern)
# The light sleep cycle count the first wake logs: the boot counts as
# cycle 1 (SleepHardware's constructor), so the first wake is cycle 2
FIRST_WAKE_SLEEP_CYCLE = 2
# The wake's checkin: WiFi and the broker again, no boot
LIGHT_WAKE_CHECKIN_TIMEOUT = 120

# --- Timer --------------------------------------------------------------

# The runtime SleepConfig the broker sends, and every wake checkin repeats
TIMER_SLEEP_CONFIG = {"mode": "S_LIGHT", "timer": {"duration": TIMER_S}}
# How many times the timer wakes the DUT and it goes back to sleep: 3 in
# the basic test (one applied wake checkin, two skipped), 20 (21 sleeps)
# in the slow soak test
BASIC_WAKE_CYCLES = 3
SOAK_WAKE_CYCLES = 20


def timer_sleep_wake_cycle(board, serial, protomq, uid, board_definition,
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
                                          WAKE_TIMEOUT, wake_re=LIGHT_WAKE_RE))

    # The timer wakes it when it was due: from its last line before
    # sleeping to its first line sent wholly after (see WOKE_TIME_RE)
    serial.expect(LIGHT_WAKE_RE.pattern, timeout=WAKE_TIMEOUT)
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
    serial.expect(r"^Connected to WiFi!$", timeout=LIGHT_WAKE_CHECKIN_TIMEOUT)
    serial.expect(r"^\[app\] Checking in after light sleep wake\.\.\.$",
                  timeout=LIGHT_WAKE_CHECKIN_TIMEOUT)
    sh.expect_wake_checkin(protomq, uid, board_definition, wake_response,
                           LIGHT_WAKE_CHECKIN_TIMEOUT)

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


def run_timer_cycles(board, serial, protomq, checkin, board_definition,
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
                                               TIMER_SLEEP_CONFIG)

    # --- Act ----------------------------------------------------------
    protomq.publish(uid, {"sleep": {"sleepConfig": TIMER_SLEEP_CONFIG}})

    # --- Assert -------------------------------------------------------
    protomq.expect_b2d_message(uid, "sleep.sleepConfig",
                               match=TIMER_SLEEP_CONFIG, timeout=5)
    # 1. The runtime SleepConfig puts the DUT to sleep
    slept_at = sh.expect_goodnight_and_sleep(serial, protomq, uid, "light")

    # 2. It then cycles: wake, checkin, sleep again
    rows = []
    misses = []
    try:
        for cycle in range(1, cycles + 1):
            slept_at = timer_sleep_wake_cycle(board, serial, protomq, uid,
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
    run_timer_cycles(board, serial, protomq, checkin, board_definition,
                          BASIC_WAKE_CYCLES)


# SOAK_WAKE_CYCLES of ~40 s each, past the suite's --timeout=600
@pytest.mark.slow
@pytest.mark.timeout(
    SOAK_WAKE_CYCLES * (LIGHT_WAKE_CHECKIN_TIMEOUT + 60) + 300)
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_light_timer_soak(board, secrets_name, flashed_board, serial,
                               protomq, checkin, board_definition):
    """20 (SOAK_WAKE_CYCLES) sleep/wake cycles in a row, for the wake timing's
    spread and the cycle's reliability."""
    run_timer_cycles(board, serial, protomq, checkin, board_definition,
                          SOAK_WAKE_CYCLES)


# --- ext0 ---------------------------------------------------------------


def expect_light_reconnect_checkin(serial, protomq, uid, board_definition,
                                   wake_response):
    """After a light sleep wake: the DUT reconnects and checks in, the
    broker answering wake_response."""
    serial.expect(r"^\[app\] Reconnecting network after light sleep "
                  r"wake\.\.\.$", timeout=10)
    serial.expect(r"^Connected to WiFi!$", timeout=LIGHT_WAKE_CHECKIN_TIMEOUT)
    serial.expect(r"^\[app\] Checking in after light sleep wake\.\.\.$",
                  timeout=LIGHT_WAKE_CHECKIN_TIMEOUT)
    sh.expect_wake_checkin(protomq, uid, board_definition, wake_response,
                           LIGHT_WAKE_CHECKIN_TIMEOUT)


@pytest.mark.parametrize("variant", list(sh.EXT0_VARIANTS))
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_light_ext0(board, secrets_name, flashed_board, serial,
                          protomq, checkin, board_definition, probe, variant):
    """The wake pin, added with a pull holding it idle, arms an ext0 light
    sleep: the DUT stays asleep while the pin idles, wakes where it slept
    as soon as the probe drives the wake level, hands the pin back to its
    digital component, reconnects, checks in (applying the response, now
    with sleepEnabled, which re-arms ext0), and sleeps again."""
    v = sh.EXT0_VARIANTS[variant]
    # --- Arrange ------------------------------------------------------
    uid, wake_response, config = sh.ext0_arrange(
        probe, serial, protomq, checkin, board_definition, "S_LIGHT", v)

    # --- Act / Assert -------------------------------------------------
    # 1. The runtime SleepConfig arms ext0, and the DUT sleeps
    slept_at = sh.ext0_send_and_sleep(serial, protomq, uid, "light", v,
                                      config)
    asleep_from = serial.mark()

    # 2. It stays asleep while the pin idles
    sh.assert_stays_asleep(board, serial, protomq, uid, HOLD_S,
                           WAKE_OR_RESET_RE)

    # 3. The wake level wakes it at once, where it slept
    driven_at, _ = sh.drive_wake(probe, serial, v, LIGHT_WAKE_RE.pattern)
    # ext0 left the pin routed to RTC IO: the DUT hands it back
    serial.expect(r"^\[sleep\] Released ext0 wake pin back to digital "
                  r"GPIO$", timeout=5)
    slept_for = serial.expect(WOKE_TIME_RE, timeout=5)
    woke_at = sh.line_time(serial, slept_for)
    resets = [l.text for l in serial.lines(asleep_from)
              if l.time <= woke_at and RESET_RE.search(l.text)]
    assert not resets, f"DUT reset during light sleep: {resets}"
    sleep_cycle = int(serial.expect(r"^Total light sleep cycles: (\d+)$",
                                    timeout=5).group(1))
    misses = []
    sh.ext0_result("light", v, woke_at - driven_at, woke_at - slept_at,
                   int(slept_for.group(1)), misses)
    if sleep_cycle != FIRST_WAKE_SLEEP_CYCLE:
        misses.append(f"firmware logs light sleep cycle {sleep_cycle}, "
                      f"expected {FIRST_WAKE_SLEEP_CYCLE}")

    # 4. It reconnects and checks in: the response, now with sleepEnabled,
    # differs from the boot's, so it's applied, re-arming ext0
    expect_light_reconnect_checkin(serial, protomq, uid, board_definition,
                                   wake_response)
    serial.expect(r"^\[checkin\] Response is new or changed$",
                  timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^\[app\] Processing sleep configuration from checkin$",
                  timeout=CHECKIN_TIMEOUT)
    serial.expect(sh.ext0_armed_re(v), timeout=5)
    serial.expect(r"^Sleep Enabled: YES$", timeout=5)
    serial.expect(r"^Completed checkin process!$", timeout=CHECKIN_TIMEOUT)

    # 5. ...publishes the pin, idle again, and sleeps again, staying asleep:
    # run() stays in loopSleep()
    sh.expect_ext0_pin_published(serial, protomq, uid, v.idle,
                                 entering_loop=False)
    sh.expect_goodnight_and_sleep(serial, protomq, uid, "light",
                                  configured=False, entering_loop=False)
    sh.assert_stays_asleep(board, serial, protomq, uid, HOLD_S,
                           WAKE_OR_RESET_RE)
    assert not misses, "\n".join(misses)


# --- Two I2C sensors (timer and ext0 wakes) ----------------------------

# How many times the DUT wakes after the boot: two, so the second wake
# also runs on what the boot set up, with no checkin applied since
I2C_WAKE_CYCLES = 2
# The ext0 wake for the I2C ext0 test
I2C_EXT0_VARIANT = "high"


def run_i2c_sleep(board, serial, protomq, checkin, board_definition, probe,
                  sensors, variant):
    """The boot checkin adds sensors (and, if variant, the ext0 pin) and
    enables light sleep, waking on the timer (variant None) or ext0. The
    boot publishes one reading per sensor, then sleeps. Each wake resumes
    where it slept: its checkin's response is the boot's, unchanged, so
    skipped: nothing re-added, ext0 not re-armed. The sensors, powered
    off over the sleep with the board's other external components, still
    publish one reading each, within I2C_PERIOD of the checkin and before
    the goodnight, and the DUT sleeps again."""
    # --- Arrange ------------------------------------------------------
    adds = sh.i2c_component_adds(sensors)
    if variant is None:
        config = TIMER_SLEEP_CONFIG
    else:
        probe.release(sh.EXT0_PIN)
        adds = {**adds, **sh.ext0_component_adds(variant)}
        config = sh.ext0_sleep_config("S_LIGHT", variant)

    # --- Act ----------------------------------------------------------
    # Boot: the checkin adds everything and enables sleep; the broker
    # answers every wake's checkin the same
    boot_from = serial.mark()
    uid = checkin(adds, sleepEnabled=True, sleepConfig=config)
    wake_response = sh.checkin_response(board_definition, componentAdds=adds,
                                        sleepEnabled=True, sleepConfig=config)

    # --- Assert -------------------------------------------------------
    names = [dev.name for dev, *_ in sensors]
    rows = []      # (cycle, asleep, reported, *reading latencies)
    misses = []
    try:
        # 1. Boot: all added, a reading each, then sleep
        sh.assert_added_since(serial, boot_from, sensors, variant, "boot")
        slept_at, _ = sh.i2c_readings_then_sleep(
            serial, protomq, uid, "light", sensors, variant, "boot", misses,
            entering_loop=True)
        for cycle in range(1, I2C_WAKE_CYCLES + 1):
            label = f"wake {cycle}"
            asleep_from = serial.mark()
            # 2. The wake, where it slept
            if variant is None:
                sh.assert_dropped(uid, sh.watch_sleep(
                    board, serial, protomq, uid, WAKE_TIMEOUT,
                    wake_re=LIGHT_WAKE_RE))
                serial.expect(LIGHT_WAKE_RE.pattern, timeout=WAKE_TIMEOUT)
            else:
                sh.assert_stays_asleep(board, serial, protomq, uid, HOLD_S,
                                       WAKE_OR_RESET_RE)
                driven_at, _ = sh.drive_wake(probe, serial, variant,
                                             LIGHT_WAKE_RE.pattern)
                serial.expect(r"^\[sleep\] Released ext0 wake pin back to "
                              r"digital GPIO$", timeout=5)
            slept_for = serial.expect(WOKE_TIME_RE, timeout=5)
            woke_at = sh.line_time(serial, slept_for)
            asleep, reported = woke_at - slept_at, int(slept_for.group(1))
            resets = [l.text for l in serial.lines(asleep_from)
                      if l.time <= woke_at and RESET_RE.search(l.text)]
            assert not resets, (
                f"{label}: DUT reset during light sleep: {resets}")
            sleep_cycle = int(serial.expect(
                r"^Total light sleep cycles: (\d+)$", timeout=5).group(1))
            if sleep_cycle != FIRST_WAKE_SLEEP_CYCLE + cycle - 1:
                misses.append(f"{label}: firmware logs light sleep cycle "
                              f"{sleep_cycle}, expected "
                              f"{FIRST_WAKE_SLEEP_CYCLE + cycle - 1}")
            if variant is None:
                sh.check_wake_timing(cycle, asleep, reported, misses)
            else:
                sh.ext0_result("light", variant, woke_at - driven_at, asleep,
                               reported, misses)
            # 3. It reconnects and checks in: the boot's response, skipped
            expect_light_reconnect_checkin(serial, protomq, uid,
                                           board_definition, wake_response)
            serial.expect(r"^\[checkin\] Response unchanged since last "
                          r"checkin$", timeout=CHECKIN_TIMEOUT)
            serial.expect(r"^Completed checkin process!$",
                          timeout=CHECKIN_TIMEOUT)
            sh.assert_none_added_since(serial, asleep_from, label)
            # 4. A reading each, then sleep again
            slept_at, latencies = sh.i2c_readings_then_sleep(
                serial, protomq, uid, "light", sensors, variant, label,
                misses, entering_loop=False)
            rows.append((cycle, asleep, reported,
                         *(f"{latencies.get(n, float('nan')):.2f}"
                           for n in names)))
        # The last sleep: the broker sees it go (and, ext0, it stays asleep)
        if variant is None:
            sh.assert_dropped(uid, sh.watch_sleep(board, serial, protomq,
                                                  uid, DISCONNECT_TIMEOUT))
        else:
            sh.assert_stays_asleep(board, serial, protomq, uid, HOLD_S,
                                   WAKE_OR_RESET_RE)
    finally:
        sh.log_wake_table(
            f"light sleep wakes ({'timer' if variant is None else 'ext0'}), "
            f"{' + '.join(names)} every {sh.I2C_PERIOD} s", rows,
            extra_headers=tuple(f"{n} reading (s)" for n in names))
    assert not misses, "\n".join(misses)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_light_timer_i2c(board, secrets_name, flashed_board, serial,
                               protomq, checkin, board_definition,
                               i2c_devices, i2c_address_space,
                               component_definition):
    """Two I2C sensors, light sleep on the timer: the boot adds both, and
    every wake publishes a reading from each before the goodnight, without
    re-adding them."""
    sensors = sh.i2c_sensors(i2c_devices, i2c_address_space,
                             component_definition)
    run_i2c_sleep(board, serial, protomq, checkin, board_definition, None,
                  sensors, None)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_light_ext0_i2c(board, secrets_name, flashed_board, serial,
                              protomq, checkin, board_definition, probe,
                              i2c_devices, i2c_address_space,
                              component_definition):
    """Two I2C sensors, light sleep woken by ext0 (the wake pin pulled down,
    driven HIGH): every wake publishes a reading from each sensor and the
    pin's level before the goodnight, and ext0, armed once at boot, keeps
    waking it."""
    sensors = sh.i2c_sensors(i2c_devices, i2c_address_space,
                             component_definition)
    run_i2c_sleep(board, serial, protomq, checkin, board_definition, probe,
                  sensors, sh.EXT0_VARIANTS[I2C_EXT0_VARIANT])


# --- "Stop sleeping" ----------------------------------------------------


def run_stop_sleeping(board, serial, protomq, checkin, board_definition,
                      wakes_before_stop):
    """Check in, send the runtime light sleep SleepConfig, and let the
    timer wake the DUT wakes_before_stop times (each wake checkin
    answered with sleep enabled). Then IO's "stop sleeping" while it
    sleeps: no message, only the next checkin's response disables sleep.
    The timer wakes the DUT where it slept; its wake checkin's response
    must differ from the last one it applied (fail its hash check), so
    it's applied: sleep disabled, the DUT leaves loopSleep() for its
    regular loop and stays awake."""
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    sleep_response = sh.enable_sleep_on_checkin(protomq, board_definition,
                                                TIMER_SLEEP_CONFIG)
    protomq.publish(uid, {"sleep": {"sleepConfig": TIMER_SLEEP_CONFIG}})
    protomq.expect_b2d_message(uid, "sleep.sleepConfig",
                               match=TIMER_SLEEP_CONFIG, timeout=5)
    slept_at = sh.expect_goodnight_and_sleep(serial, protomq, uid, "light")
    rows, misses = [], []
    for cycle in range(1, wakes_before_stop + 1):
        slept_at = timer_sleep_wake_cycle(board, serial, protomq, uid,
                                          board_definition, sleep_response,
                                          cycle, slept_at, rows, misses)
    asleep_from = serial.mark()

    # --- Act ----------------------------------------------------------
    # "Stop sleeping" while it sleeps
    awake_response = sh.stop_sleeping(protomq, board_definition)

    # --- Assert -------------------------------------------------------
    # 1. The timer still wakes it, where it slept
    sh.assert_dropped(uid, sh.watch_sleep(board, serial, protomq, uid,
                                          WAKE_TIMEOUT,
                                          wake_re=LIGHT_WAKE_RE))
    serial.expect(LIGHT_WAKE_RE.pattern, timeout=WAKE_TIMEOUT)
    slept_for = serial.expect(WOKE_TIME_RE, timeout=5)
    woke_at = sh.line_time(serial, slept_for)
    resets = [l.text for l in serial.lines(asleep_from)
              if l.time <= woke_at and RESET_RE.search(l.text)]
    assert not resets, f"DUT reset during light sleep: {resets}"
    log.info("DUT woke %.3f s after entering light sleep; firmware reports "
             "%s s", woke_at - slept_at, slept_for.group(1))

    # 2. It reconnects and checks in: the response, without sleep, fails
    # the hash check, so it's applied and disables sleep
    serial.expect(r"^\[app\] Reconnecting network after light sleep "
                  r"wake\.\.\.$", timeout=10)
    serial.expect(r"^\[app\] Checking in after light sleep wake\.\.\.$",
                  timeout=LIGHT_WAKE_CHECKIN_TIMEOUT)
    sh.expect_sleep_stopped_checkin(protomq, uid, board_definition,
                                    awake_response,
                                    LIGHT_WAKE_CHECKIN_TIMEOUT)
    serial.expect(r"^\[checkin\] Response is new or changed$",
                  timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^\[app\] Sleep disabled by checkin$",
                  timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^\[sleep\] Sleep disabled, resuming regular "
                  r"operation\.$", timeout=5)
    serial.expect(r"^Completed checkin process!$", timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^\[app\] Sleep disabled, entering loop\(\)$",
                  timeout=CHECKIN_TIMEOUT)

    # 3. ...and stays awake, in its regular loop
    sh.assert_stays_awake(serial, protomq, uid)
    assert not misses, "\n".join(misses)


# Known firmware bug (#1123 / #1060): a runtime sleep.B2D SleepConfig
# doesn't invalidate the stored checkin response hash. Stopped before any
# wake checkin, the "stop sleeping" response (plain R_OK, sleep off) is
# byte-identical to the boot's, last applied: the wake checkin skips it as
# unchanged, and the DUT keeps sleeping. strict: an XPASS fails the run,
# flagging the fix so the mark can come off.
@pytest.mark.xfail(strict=True, reason=(
    "firmware: a runtime SleepConfig doesn't invalidate the checkin hash, "
    "so 'stop sleeping' before the first wake checkin matches the boot "
    "response and is skipped; the DUT keeps sleeping"))
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_light_stop_sleeping(board, secrets_name, flashed_board, serial,
                                   protomq, checkin, board_definition):
    """"Stop sleeping" during the first light sleep after a runtime
    SleepConfig, before any wake checkin."""
    run_stop_sleeping(board, serial, protomq, checkin, board_definition, 0)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_light_stop_sleeping_after_wake(board, secrets_name,
                                              flashed_board, serial, protomq,
                                              checkin, board_definition):
    """"Stop sleeping" after one light sleep/wake cycle, whose wake checkin
    applied a response with sleep enabled: the stopped response differs
    from it."""
    run_stop_sleeping(board, serial, protomq, checkin, board_definition, 1)
