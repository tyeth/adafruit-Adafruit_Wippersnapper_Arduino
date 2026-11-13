"""
Helpers shared by the sleep behavioral tests (test_sleep_deep_timer.py,
test_sleep_light_timer.py): the checkin response that re-enables sleep,
the goodnight and pre-sleep disconnects, watching the DUT while it
sleeps, timing serial lines, and the per-cycle wake table.

Times are time.time() on this host: the serial monitor stamps each line
when its first byte arrives, and ProtoMQ, also on this host, stamps each
delivery, so the two compare directly.
"""

import logging
import os
import time
from pathlib import Path

log = logging.getLogger("hil.firmware.sleep")

# How long the DUT sleeps, in seconds
TIMER_S = 30
# A wake, measured on this host or reported by the firmware, may be off
# from TIMER_S by this much, in seconds
SLEEP_TOLERANCE_S = 3
# How long the DUT may take to apply a SleepConfig and say goodnight with
# no components to update first, in seconds
GOODNIGHT_TIMEOUT = 15
# How long after the goodnight the broker may take to drop the DUT: an MQTT
# DISCONNECT drops it at once, else 1.5x the firmware's 11 s keepalive
# (WS_BROKER_KEEPALIVE_MS) expires the session, plus margin
DISCONNECT_TIMEOUT = 25
CHECKIN_TIMEOUT = 30
# How long a sleep watch waits for the wake: the timer, plus margin
WAKE_TIMEOUT = TIMER_S + SLEEP_TOLERANCE_S + 15


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


def enable_sleep_on_checkin(protomq, board_definition, sleep_config):
    """Answer every later checkin (one per wake) with sleepEnabled and
    sleep_config, as IO re-enables sleep at each wake. Returns that
    response."""
    response = checkin_response(board_definition, sleepEnabled=True,
                                sleepConfig=sleep_config)
    protomq.clear_autorespond()
    protomq.set_autorespond(
        "checkin.request", {"checkin": {"response": response}},
        match={"hardwareUid": board_definition.board_id})
    return response


def usb_sysfs_path(board):
    """The DUT's USB device in sysfs, from the board entry's hub location,
    e.g. /sys/bus/usb/devices/3-1.3.1."""
    loc = board.entry["location"]
    return Path("/sys/bus/usb/devices") / f"{loc['hub']}.{loc['port']}"


def line_time(serial, match):
    """When the serial line a serial.expect() just matched arrived, as
    time.time(): the latest line with that text."""
    return next(l.time for l in reversed(serial.lines())
                if l.text == match.string)


def watch_sleep(board, serial, protomq, uid, timeout, wake_re=None):
    """While the DUT sleeps, log whenever its USB device, serial port or
    broker connection changes. Stops at the first serial line matching
    wake_re (a compiled regex) if given, else once the broker drops it,
    or after timeout seconds. Returns the seconds until the broker
    dropped it, or None if it never did."""
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
            if wake_re is None:
                break
        if wake_re is not None and any(wake_re.search(l.text)
                                       for l in serial.lines(start)):
            log.info("t+%5.1fs: wake seen on serial", elapsed)
            break
        time.sleep(1)
    if dropped_at is None:
        log.info("broker still listed %s after %.1fs of sleep", uid, elapsed)
    return dropped_at


def assert_dropped(uid, dropped_at):
    assert dropped_at is not None, (
        f"broker still listed {uid} through its sleep: no MQTT DISCONNECT, "
        "and no keepalive expiry")


def expect_goodnight_and_sleep(serial, protomq, uid, mode, configured=True,
                               entering_loop=True):
    """The DUT says goodnight, disconnects from IO and WiFi, and sleeps in
    mode ("deep" or "light"). configured: it applied a SleepConfig just
    before (a runtime one, or one from a checkin it didn't skip), so it
    logs arming the timer first. entering_loop: run() switches to
    loopSleep() first, once per boot. Returns when the DUT's last line
    before sleeping arrived, as time.time()."""
    if configured:
        serial.expect(r"^\[sleep\] Timer wakeup set to %d seconds$" % TIMER_S,
                      timeout=GOODNIGHT_TIMEOUT)
        serial.expect(r"^Sleep Enabled: YES$", timeout=5)
    if entering_loop:
        serial.expect(r"^\[app\] Sleep enabled, entering loopSleep\(\)$",
                      timeout=GOODNIGHT_TIMEOUT)
    protomq.expect_d2b_message(uid, "sleep.goodnight",
                               timeout=GOODNIGHT_TIMEOUT)
    # MQTT DISCONNECT, then WiFi off. Match the failure line too, so a
    # failed disconnect fails here by name rather than as a timeout.
    for what in ("IO", "WiFi"):
        line = serial.expect(
            r"^\[app\] (Disconnected from|Failed to disconnect from) "
            r"%s\.$" % what, timeout=10).string
        assert line.startswith("[app] Disconnected from"), (
            f"DUT failed to disconnect from {what} before sleep: {line!r}")
    serial.expect(r"^\[app\] All components updated, entering sleep\.\.\.$",
                  timeout=5)
    if mode == "deep":
        last = serial.expect(r"^\[sleep\] Entering deep sleep$", timeout=5)
    else:
        serial.expect(r"^\[sleep\] Entering light sleep$", timeout=5)
        # Its last line: then it ends Serial and sleeps
        last = serial.expect(r"^\[sleep\] Starting light sleep NOW\.\.\.$",
                             timeout=5)
    return line_time(serial, last)


def expect_wake_checkin(protomq, uid, board_definition, wake_response,
                        timeout):
    """The DUT checks in after a wake, the broker answers wake_response,
    and the DUT publishes Complete. Returns when the broker received the
    checkin.Request, as time.time() (None if ProtoMQ doesn't timestamp)."""
    protomq.expect_d2b_message(
        uid, "checkin.request",
        match={"hardwareUid": board_definition.board_id}, timeout=timeout)
    checkin_at = protomq.last_d2b_time(uid)
    protomq.expect_b2d_message(uid, "checkin.response",
                               match=wake_response, timeout=5)
    protomq.expect_d2b_message(uid, "checkin.complete",
                               timeout=CHECKIN_TIMEOUT)
    return checkin_at


def check_wake_timing(cycle, asleep, reported, misses):
    """Add a miss for a wake measured on this host (asleep, seconds) or
    reported by the firmware (reported, seconds) off from TIMER_S by more
    than SLEEP_TOLERANCE_S."""
    if abs(asleep - TIMER_S) > SLEEP_TOLERANCE_S:
        misses.append(f"cycle {cycle}: DUT woke {asleep:.3f} s after "
                      f"sleeping, expected {TIMER_S} +/- "
                      f"{SLEEP_TOLERANCE_S} s")
    if abs(reported - TIMER_S) > SLEEP_TOLERANCE_S:
        misses.append(f"cycle {cycle}: firmware reports {reported} s asleep, "
                      f"expected {TIMER_S} +/- {SLEEP_TOLERANCE_S} s")


def log_wake_table(title, rows, extra_headers=()):
    """Log each cycle's wake: rows of (cycle, measured on this host in
    seconds, firmware reported in seconds, *extras), extras under
    extra_headers."""
    if not rows:
        return
    headers = ("firmware reported (s)",) + tuple(extra_headers)
    widths = [max(len(h), 8) for h in headers]
    lines = [f"{title}, {TIMER_S} s timer:",
             f"{'cycle':>5} | {'measured on Pi (s)':>18} | "
             f"{'vs timer (s)':>12} | "
             + " | ".join(f"{h:>{w}}" for h, w in zip(headers, widths))]
    for cycle, measured, *rest in rows:
        lines.append(f"{cycle:>5} | {measured:>18.3f} | "
                     f"{measured - TIMER_S:>+12.3f} | "
                     + " | ".join(f"{str(v):>{w}}"
                                  for v, w in zip(rest, widths)))
    measured = [r[1] for r in rows]
    lines.append(f"{'min':>5} | {min(measured):>18.3f}")
    lines.append(f"{'max':>5} | {max(measured):>18.3f}")
    lines.append(f"{'mean':>5} | {sum(measured) / len(measured):>18.3f}")
    log.info("\n".join(lines))
