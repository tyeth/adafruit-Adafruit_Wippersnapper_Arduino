"""
HIL tests for Adafruit Wippersnapper firmware's deep sleep mode.
"""

import logging
import os
import re
import time
from pathlib import Path

import pytest

log = logging.getLogger("hil.firmware.sleep")

# ws_sleep_SleepMode_S_DEEP, as the wake's "Prv. Sleep Mode" line prints it
S_DEEP = 2
# How long the DUT sleeps, in seconds
TIMER_S = 30
# The measured sleep, from the wake's "Total Sleep Duration (sec)" line, may
# be off from TIMER_S by this much, in seconds
SLEEP_TOLERANCE_S = 3
# The runtime SleepConfig the broker sends, and the wake checkin repeats
SLEEP_CONFIG = {"mode": "S_DEEP", "timer": {"duration": TIMER_S}}
# How long the DUT may take to apply the SleepConfig and say goodnight with
# no components to update first, in seconds
GOODNIGHT_TIMEOUT = 15
# How long after the goodnight the broker may take to drop the DUT: an MQTT
# DISCONNECT drops it at once, else 1.5x the firmware's 11 s keepalive
# (WS_BROKER_KEEPALIVE_MS) expires the session, plus margin
DISCONNECT_TIMEOUT = 25
# The boot ROM's reset line on a deep sleep wake, the first line the DUT
# prints when the timer fires: "rst:0x5 (DEEPSLEEP_RESET),boot:..." on an
# ESP32, "rst:0x5 (DSLEEP),boot:..." on the S2/S3/C3
DEEP_SLEEP_RESET_RE = r"^rst:0x5 \((DEEPSLEEP_RESET|DSLEEP)\)"
# The wake's first boot lines, ending a sleep watch
WAKE_RE = re.compile(DEEP_SLEEP_RESET_RE + r"|^ESP Reset Reason: ")
# The wake's checkin: the timer, then a boot, WiFi and the broker
WAKE_CHECKIN_TIMEOUT = TIMER_S + SLEEP_TOLERANCE_S + 150
CHECKIN_TIMEOUT = 30


def checkin_response(board_definition, **extra):
    """The R_OK checkin.Response the checkin fixture sends for
    board_definition, with extra fields (ProtoMQ JSON names)."""
    definition = board_definition.definition
    return {
        "response": "R_OK",
        "totalGpioPins": len(definition["components"]["digitalPins"]),
        "totalAnalogPins": len(definition["components"]["analogPins"]),
        "referenceVoltage": definition["mcuRefVoltage"],
        **extra,
    }


def usb_sysfs_path(board):
    """The DUT's USB device in sysfs, from the board entry's hub location,
    e.g. /sys/bus/usb/devices/3-1.3.1."""
    loc = board.entry["location"]
    return Path("/sys/bus/usb/devices") / f"{loc['hub']}.{loc['port']}"


def watch_sleep(board, serial, protomq, uid, timeout, until_wake):
    """While the DUT sleeps, log once a second whether its USB device and
    serial port are present, and whether the broker still lists it. Stops
    at the wake's first boot line if until_wake, else once the broker
    drops it, or after timeout seconds. Returns the seconds until the
    broker dropped it, or None if it never did."""
    usb = usb_sysfs_path(board)
    start = serial.mark()
    t0 = time.monotonic()
    dropped_at = None
    last = None
    while (elapsed := time.monotonic() - t0) < timeout:
        state = (usb.exists(), os.path.exists(serial.port),
                 uid in protomq.clients())
        if state != last:
            log.info("t+%5.1fs during sleep: usb %s %s, %s %s, broker %s",
                     elapsed, usb.name,
                     "present" if state[0] else "GONE", serial.port,
                     "present" if state[1] else "GONE",
                     "connected" if state[2] else "DROPPED")
            last = state
        if not state[2] and dropped_at is None:
            dropped_at = elapsed
            if not until_wake:
                break
        if until_wake and any(WAKE_RE.search(l.text)
                              for l in serial.lines(start)):
            log.info("t+%5.1fs: wake boot seen on serial", elapsed)
            break
        time.sleep(1)
    if dropped_at is None:
        log.info("broker still listed %s after %.1fs of sleep", uid, elapsed)
    return dropped_at


def line_time(serial, match):
    """When the serial line a serial.expect() just matched arrived, as
    time.time(): the latest line with that text."""
    return next(l.time for l in reversed(serial.lines())
                if l.text == match.string)


def expect_goodnight_and_sleep(serial, protomq, uid):
    """The DUT enables sleep, says goodnight, disconnects from IO and WiFi,
    and deep sleeps. Returns when its "Entering deep sleep" line arrived,
    as time.time()."""
    serial.expect(r"^\[sleep\] Timer wakeup set to %d seconds$" % TIMER_S,
                  timeout=GOODNIGHT_TIMEOUT)
    serial.expect(r"^Sleep Enabled: YES$", timeout=5)
    serial.expect(r"^\[app\] Sleep enabled, entering loopSleep\(\)$",
                  timeout=GOODNIGHT_TIMEOUT)
    protomq.expect_d2b_message(uid, "sleep.goodnight",
                               timeout=GOODNIGHT_TIMEOUT)
    # MQTT DISCONNECT, then WiFi off (waits up to 500 ms for the link).
    # Match the failure line too, so a failed disconnect fails here by name
    # rather than as a timeout.
    for what in ("IO", "WiFi"):
        line = serial.expect(
            r"^\[app\] (Disconnected from|Failed to disconnect from) "
            r"%s\.$" % what, timeout=10).string
        assert line.startswith("[app] Disconnected from"), (
            f"DUT failed to disconnect from {what} before sleep: {line!r}")
    serial.expect(r"^\[app\] All components updated, entering sleep\.\.\.$",
                  timeout=5)
    return line_time(serial, serial.expect(r"^\[sleep\] Entering deep sleep$",
                                           timeout=5))


def assert_dropped(uid, dropped_at):
    assert dropped_at is not None, (
        f"broker still listed {uid} through its deep sleep: no MQTT "
        "DISCONNECT, and no keepalive expiry")


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_deep_timer_runtime(board, secrets_name, flashed_board, serial,
                                  protomq, checkin, board_definition):
    # --- Arrange ------------------------------------------------------
    # A regular checkin, no sleep fields
    uid = checkin()
    # Every later checkin (one per wake) gets sleep re-enabled
    wake_response = checkin_response(board_definition, sleepEnabled=True,
                                     sleepConfig=SLEEP_CONFIG)
    protomq.clear_autorespond()
    protomq.set_autorespond(
        "checkin.request", {"checkin": {"response": wake_response}},
        match={"hardwareUid": board_definition.board_id})

    # --- Act ----------------------------------------------------------
    protomq.publish(uid, {"sleep": {"sleepConfig": SLEEP_CONFIG}})

    # --- Assert -------------------------------------------------------
    protomq.expect_b2d_message(uid, "sleep.sleepConfig", match=SLEEP_CONFIG,
                               timeout=5)
    # 1. The runtime SleepConfig puts the DUT to sleep
    slept_at = expect_goodnight_and_sleep(serial, protomq, uid)
    goodnight_at = protomq.last_d2b_time(uid)
    # Through the whole sleep, to log the USB/serial/broker timeline
    dropped_at = watch_sleep(board, serial, protomq, uid,
                             TIMER_S + SLEEP_TOLERANCE_S + 15, until_wake=True)
    assert_dropped(uid, dropped_at)

    # 2. The timer wakes it, a reset into a fresh boot, when it was due:
    # from "Entering deep sleep" to the boot ROM's deep sleep reset line
    woke_at = line_time(serial, serial.expect(
        DEEP_SLEEP_RESET_RE, timeout=TIMER_S + SLEEP_TOLERANCE_S + 15))
    asleep = woke_at - slept_at
    log.info("DUT woke %.1f s after entering deep sleep, expected %d s "
             "(%+.1f s)", asleep, TIMER_S, asleep - TIMER_S)
    assert abs(asleep - TIMER_S) <= SLEEP_TOLERANCE_S, (
        f"DUT woke {asleep:.1f} s after entering deep sleep, expected "
        f"{TIMER_S} +/- {SLEEP_TOLERANCE_S} s")
    serial.expect(r"^Sleep Wakeup Reason: Timer$", timeout=10)
    serial.expect(r"^Prv\. Sleep Mode: %d$" % S_DEEP, timeout=5)
    slept = int(serial.expect(r"^Total Sleep Duration \(sec\): (\d+)$",
                              timeout=5).group(1))
    log.info("DUT reports %d s asleep (timer %d s)", slept, TIMER_S)
    assert abs(slept - TIMER_S) <= SLEEP_TOLERANCE_S, (
        f"DUT slept {slept} s, expected {TIMER_S} +/- {SLEEP_TOLERANCE_S} s")

    # 3. It checks in again, and the broker re-enables sleep
    protomq.expect_d2b_message(
        uid, "checkin.request",
        match={"hardwareUid": board_definition.board_id},
        timeout=WAKE_CHECKIN_TIMEOUT)
    checkin_at = protomq.last_d2b_time(uid)
    protomq.expect_b2d_message(uid, "checkin.response",
                               match=wake_response, timeout=5)
    protomq.expect_d2b_message(uid, "checkin.complete",
                               timeout=CHECKIN_TIMEOUT)
    # For reference: the wake's boot, WiFi and broker connect on top. The
    # broker and the serial monitor both run on this host, so one clock.
    if goodnight_at is not None and checkin_at is not None:
        log.info("goodnight to wake checkin: %.1f s (%.1f s after the wake)",
                 checkin_at - goodnight_at, checkin_at - woke_at)

    # 4. ...and goes back to sleep
    serial.expect(r"^\[app\] Processing sleep configuration from checkin$",
                  timeout=CHECKIN_TIMEOUT)
    expect_goodnight_and_sleep(serial, protomq, uid)
    assert_dropped(uid, watch_sleep(board, serial, protomq, uid,
                                    DISCONNECT_TIMEOUT, until_wake=False))
