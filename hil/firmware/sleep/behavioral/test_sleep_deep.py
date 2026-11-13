"""
HIL tests for Adafruit Wippersnapper firmware's deep sleep mode: a timer
wake (basic and slow soak), ext0 (RTC GPIO level) wakes on a pin pulled
down (wakes HIGH) or up (wakes LOW), and two I2C sensors re-registered and
read each boot, woken by the timer or ext0.

A deep sleep wake is a reset into a fresh boot: the DUT checks in again,
and the broker's response re-adds its components and re-enables sleep.
"""

import re

import pytest

import sleep_helpers as sh
from sleep_helpers import (CHECKIN_TIMEOUT, DISCONNECT_TIMEOUT, HOLD_S,
                           SLEEP_TOLERANCE_S, TIMER_S, WAKE_TIMEOUT, log)

# ws_sleep_SleepMode_S_DEEP, as the wake's "Prv. Sleep Mode" line prints it
S_DEEP = 2
# The boot ROM's reset line on a deep sleep wake, the first line the DUT
# prints when it wakes: "rst:0x5 (DEEPSLEEP_RESET),boot:..." on an ESP32,
# "rst:0x5 (DSLEEP),boot:..." on the S2/S3/C3
DEEP_SLEEP_RESET_RE = r"^rst:0x5 \((DEEPSLEEP_RESET|DSLEEP)\)"
# The wake's first boot lines, ending a sleep watch
WAKE_RE = re.compile(DEEP_SLEEP_RESET_RE + r"|^ESP Reset Reason: ")
# The wake's checkin: the timer, then a boot, WiFi and the broker
WAKE_CHECKIN_TIMEOUT = TIMER_S + SLEEP_TOLERANCE_S + 150

# --- Timer --------------------------------------------------------------

# The runtime SleepConfig the broker sends, and every wake checkin repeats
TIMER_SLEEP_CONFIG = {"mode": "S_DEEP", "timer": {"duration": TIMER_S}}
# How many times the timer wakes the DUT and it goes back to sleep: once
# in the basic test, 20 times (21 sleeps) in the slow soak test
BASIC_WAKE_CYCLES = 1
SOAK_WAKE_CYCLES = 20


def expect_timer_wake(serial, slept_at):
    """The timer wakes the DUT, a reset into a fresh boot. Returns (when
    the boot ROM's reset line arrived, seconds asleep measured from
    slept_at, the firmware's Total Sleep Duration)."""
    woke_at = sh.line_time(serial, serial.expect(DEEP_SLEEP_RESET_RE,
                                                 timeout=WAKE_TIMEOUT))
    serial.expect(r"^Sleep Wakeup Reason: Timer$", timeout=10)
    serial.expect(r"^Prv\. Sleep Mode: %d$" % S_DEEP, timeout=5)
    reported = int(serial.expect(r"^Total Sleep Duration \(sec\): (\d+)$",
                                 timeout=5).group(1))
    return woke_at, woke_at - slept_at, reported


def timer_sleep_wake_cycle(board, serial, protomq, uid, board_definition,
                           wake_response, cycle, slept_at, rows, misses):
    """One deep sleep, from slept_at (its "Entering deep sleep" line's
    time): the timer wakes the DUT, it checks in, and the broker's
    sleepEnabled puts it back to sleep. Appends the cycle's row to rows
    and any timing miss to misses. Returns the next sleep's slept_at."""
    goodnight_at = protomq.last_d2b_time(uid)
    # Through the whole sleep, to log the USB/serial/broker timeline
    sh.assert_dropped(uid, sh.watch_sleep(board, serial, protomq, uid,
                                          WAKE_TIMEOUT, wake_re=WAKE_RE))
    woke_at, asleep, reported = expect_timer_wake(serial, slept_at)
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


def run_timer_cycles(board, serial, protomq, checkin, board_definition,
                     cycles):
    """Check in with no sleep fields, then send the runtime SleepConfig:
    the DUT sleeps, and the timer wakes it cycles times, each wake checking
    in, the broker re-enabling sleep, and the DUT sleeping again. Timing
    misses are collected so every cycle is measured, and logged with the
    wake table; a missing step fails at once."""
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    wake_response = sh.enable_sleep_on_checkin(protomq, board_definition,
                                               TIMER_SLEEP_CONFIG)

    # --- Act ----------------------------------------------------------
    protomq.publish(uid, {"sleep": {"sleepConfig": TIMER_SLEEP_CONFIG}})

    # --- Assert -------------------------------------------------------
    protomq.expect_b2d_message(uid, "sleep.sleepConfig",
                               match=TIMER_SLEEP_CONFIG, timeout=5)
    # 1. The runtime SleepConfig puts the DUT to sleep
    slept_at = sh.expect_goodnight_and_sleep(serial, protomq, uid, "deep")

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
        sh.log_wake_table("deep sleep wakes", rows)
    assert not misses, "\n".join(misses)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_deep_timer_runtime(board, secrets_name, flashed_board, serial,
                                  protomq, checkin, board_definition):
    """One deep sleep/wake cycle: the runtime SleepConfig puts the DUT to
    sleep, the timer wakes it, it checks in, and sleeps again."""
    run_timer_cycles(board, serial, protomq, checkin, board_definition,
                     BASIC_WAKE_CYCLES)


# SOAK_WAKE_CYCLES of ~50 s each, past the suite's --timeout=600
@pytest.mark.slow
@pytest.mark.timeout(SOAK_WAKE_CYCLES * (WAKE_CHECKIN_TIMEOUT + 60) + 300)
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_deep_timer_soak(board, secrets_name, flashed_board, serial,
                               protomq, checkin, board_definition):
    """20 (SOAK_WAKE_CYCLES) sleep/wake cycles in a row, for the wake
    timing's spread and the cycle's reliability."""
    run_timer_cycles(board, serial, protomq, checkin, board_definition,
                     SOAK_WAKE_CYCLES)


# --- Two I2C sensors (timer and ext0 wakes) ----------------------------

# How many times the DUT wakes after the boot
I2C_WAKE_CYCLES = 1
# The ext0 wake for the I2C ext0 test
I2C_EXT0_VARIANT = "high"


def run_i2c_sleep(board, serial, protomq, checkin, board_definition, probe,
                  sensors, variant):
    """The boot checkin adds sensors (and, if variant, the ext0 pin) and
    enables deep sleep, waking on the timer (variant None) or ext0: each
    boot, the boot's and every wake's, adds them all and publishes one
    reading per sensor, within I2C_PERIOD of its checkin and all before
    its goodnight, then sleeps."""
    # --- Arrange ------------------------------------------------------
    adds = sh.i2c_component_adds(sensors)
    if variant is None:
        config, armed_re = TIMER_SLEEP_CONFIG, sh.TIMER_ARMED_RE
    else:
        probe.release(sh.EXT0_PIN)
        adds = {**adds, **sh.ext0_component_adds(variant)}
        config = sh.ext0_sleep_config("S_DEEP", variant)
        armed_re = sh.ext0_armed_re(variant)

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
            serial, protomq, uid, "deep", sensors, variant, "boot", misses,
            entering_loop=True)
        for cycle in range(1, I2C_WAKE_CYCLES + 1):
            label = f"wake {cycle}"
            # 2. The wake, a reset into a fresh boot
            if variant is None:
                sh.assert_dropped(uid, sh.watch_sleep(
                    board, serial, protomq, uid, WAKE_TIMEOUT,
                    wake_re=WAKE_RE))
                wake_from = serial.mark()
                woke_at, asleep, reported = expect_timer_wake(serial,
                                                              slept_at)
                sh.check_wake_timing(cycle, asleep, reported, misses)
            else:
                sh.assert_stays_asleep(board, serial, protomq, uid, HOLD_S,
                                       WAKE_RE)
                wake_from = serial.mark()
                driven_at, reset = sh.drive_wake(probe, serial, variant,
                                                 DEEP_SLEEP_RESET_RE)
                woke_at = sh.line_time(serial, reset)
                serial.expect(r"^Sleep Wakeup Reason: EXT0$", timeout=10)
                reported = int(serial.expect(
                    r"^Total Sleep Duration \(sec\): (\d+)$",
                    timeout=10).group(1))
                asleep = woke_at - slept_at
                sh.ext0_result("deep", variant, woke_at - driven_at, asleep,
                               reported, misses)
            # 3. Its checkin re-adds everything and re-arms the wake
            sh.expect_wake_checkin(protomq, uid, board_definition,
                                   wake_response, WAKE_CHECKIN_TIMEOUT)
            serial.expect(r"^\[app\] Processing sleep configuration from "
                          r"checkin$", timeout=CHECKIN_TIMEOUT)
            sh.assert_added_since(serial, wake_from, sensors, variant, label)
            serial.expect(armed_re, timeout=5)
            serial.expect(r"^Sleep Enabled: YES$", timeout=5)
            # 4. A reading each, then sleep again
            slept_at, latencies = sh.i2c_readings_then_sleep(
                serial, protomq, uid, "deep", sensors, variant, label,
                misses, entering_loop=True)
            rows.append((cycle, asleep, reported,
                         *(f"{latencies.get(n, float('nan')):.2f}"
                           for n in names)))
        # The last sleep: the broker sees it go (and, ext0, it stays asleep)
        if variant is None:
            sh.assert_dropped(uid, sh.watch_sleep(board, serial, protomq,
                                                  uid, DISCONNECT_TIMEOUT))
        else:
            sh.assert_stays_asleep(board, serial, protomq, uid, HOLD_S,
                                   WAKE_RE)
    finally:
        sh.log_wake_table(
            f"deep sleep wakes ({'timer' if variant is None else 'ext0'}), "
            f"{' + '.join(names)} every {sh.I2C_PERIOD} s", rows,
            extra_headers=tuple(f"{n} reading (s)" for n in names))
    assert not misses, "\n".join(misses)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_deep_timer_i2c(board, secrets_name, flashed_board, serial,
                              protomq, checkin, board_definition,
                              i2c_devices, i2c_address_space,
                              component_definition):
    """Two I2C sensors, deep sleep on the timer: each boot (the first, and
    the wake's) adds both, publishes a reading from each before the
    goodnight, and sleeps."""
    sensors = sh.i2c_sensors(i2c_devices, i2c_address_space,
                             component_definition)
    run_i2c_sleep(board, serial, protomq, checkin, board_definition, None,
                  sensors, None)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_deep_ext0_i2c(board, secrets_name, flashed_board, serial,
                             protomq, checkin, board_definition, probe,
                             i2c_devices, i2c_address_space,
                             component_definition):
    """Two I2C sensors, deep sleep woken by ext0 (the wake pin pulled down,
    driven HIGH): each boot adds the sensors and the pin, publishes a
    reading from each sensor and the pin's level before the goodnight,
    and sleeps."""
    sensors = sh.i2c_sensors(i2c_devices, i2c_address_space,
                             component_definition)
    run_i2c_sleep(board, serial, protomq, checkin, board_definition, probe,
                  sensors, sh.EXT0_VARIANTS[I2C_EXT0_VARIANT])


# --- ext0 ---------------------------------------------------------------

# The ext0 wake's checkin: a boot, WiFi and the broker
EXT0_WAKE_CHECKIN_TIMEOUT = 150


@pytest.mark.parametrize("variant", list(sh.EXT0_VARIANTS))
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_deep_ext0(board, secrets_name, flashed_board, serial, protomq,
                         checkin, board_definition, probe, variant):
    """The wake pin, added with a pull holding it idle, arms an ext0 deep
    sleep: the DUT stays asleep while the pin idles, wakes as soon as the
    probe drives the wake level (a reset into a fresh boot), checks in
    (re-adding the pin and re-arming ext0), and sleeps again."""
    v = sh.EXT0_VARIANTS[variant]
    # --- Arrange ------------------------------------------------------
    uid, wake_response, config = sh.ext0_arrange(
        probe, serial, protomq, checkin, board_definition, "S_DEEP", v)

    # --- Act / Assert -------------------------------------------------
    # 1. The runtime SleepConfig arms ext0, and the DUT sleeps
    slept_at = sh.ext0_send_and_sleep(serial, protomq, uid, "deep", v, config)

    # 2. It stays asleep while the pin idles
    sh.assert_stays_asleep(board, serial, protomq, uid, HOLD_S, WAKE_RE)

    # 3. The wake level wakes it at once: a reset into a fresh boot
    driven_at, reset = sh.drive_wake(probe, serial, v, DEEP_SLEEP_RESET_RE)
    woke_at = sh.line_time(serial, reset)
    serial.expect(r"^Sleep Wakeup Reason: EXT0$", timeout=10)
    serial.expect(r"^Prv\. Sleep Mode: %d$" % S_DEEP, timeout=5)
    reported = int(serial.expect(r"^Total Sleep Duration \(sec\): (\d+)$",
                                 timeout=5).group(1))
    misses = []
    sh.ext0_result("deep", v, woke_at - driven_at, woke_at - slept_at,
                   reported, misses)

    # 4. It checks in: the response re-adds the pin and re-arms ext0
    sh.expect_wake_checkin(protomq, uid, board_definition, wake_response,
                           EXT0_WAKE_CHECKIN_TIMEOUT)
    sh.expect_ext0_pin_added(serial, v)
    serial.expect(r"^\[app\] Processing sleep configuration from checkin$",
                  timeout=CHECKIN_TIMEOUT)
    serial.expect(sh.ext0_armed_re(v), timeout=5)
    serial.expect(r"^Sleep Enabled: YES$", timeout=5)

    # 5. ...publishes the pin, idle again, and sleeps again, staying asleep
    sh.expect_ext0_pin_published(serial, protomq, uid, v.idle,
                                 entering_loop=True)
    sh.expect_goodnight_and_sleep(serial, protomq, uid, "deep",
                                  configured=False, entering_loop=False)
    sh.assert_stays_asleep(board, serial, protomq, uid, HOLD_S, WAKE_RE)
    assert not misses, "\n".join(misses)


# --- "Stop sleeping" ----------------------------------------------------


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_deep_stop_sleeping(board, secrets_name, flashed_board, serial,
                                  protomq, checkin, board_definition):
    """IO's "stop sleeping" while the DUT deep sleeps: no message, only the
    next checkin's response disables sleep. The timer wakes the DUT, a
    fresh boot; it checks in afresh (no response hash survives a reset),
    the response has no sleep, and it stays awake in its regular loop."""
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    sh.enable_sleep_on_checkin(protomq, board_definition, TIMER_SLEEP_CONFIG)
    protomq.publish(uid, {"sleep": {"sleepConfig": TIMER_SLEEP_CONFIG}})
    protomq.expect_b2d_message(uid, "sleep.sleepConfig",
                               match=TIMER_SLEEP_CONFIG, timeout=5)
    slept_at = sh.expect_goodnight_and_sleep(serial, protomq, uid, "deep")

    # --- Act ----------------------------------------------------------
    # "Stop sleeping" while it sleeps
    awake_response = sh.stop_sleeping(protomq, board_definition)

    # --- Assert -------------------------------------------------------
    # 1. The timer still wakes it, a reset into a fresh boot
    sh.assert_dropped(uid, sh.watch_sleep(board, serial, protomq, uid,
                                          WAKE_TIMEOUT, wake_re=WAKE_RE))
    _, asleep, reported = expect_timer_wake(serial, slept_at)
    log.info("DUT woke %.3f s after entering deep sleep; firmware reports "
             "%d s", asleep, reported)

    # 2. It checks in afresh: the response, without sleep, is applied
    sh.expect_sleep_stopped_checkin(protomq, uid, board_definition,
                                    awake_response, WAKE_CHECKIN_TIMEOUT)
    serial.expect(r"^\[checkin\] Response is new or changed$",
                  timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^Completed checkin process!$", timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^Running app loop\.\.\.$", timeout=CHECKIN_TIMEOUT)

    # 3. ...and stays awake, in its regular loop
    sh.assert_stays_awake(serial, protomq, uid)
