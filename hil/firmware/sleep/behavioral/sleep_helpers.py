"""
Helpers shared by the sleep behavioral tests (test_sleep_deep.py,
test_sleep_light.py): the checkin response that re-enables sleep, the
goodnight and pre-sleep disconnects, watching the DUT while it sleeps,
timing serial lines, the per-cycle wake table, and the ext0 wake pin.

Times are time.time() on this host: the serial monitor stamps each line
when its first byte arrives, and ProtoMQ, also on this host, stamps each
delivery, so the two compare directly.
"""

import logging
import os
import re
import time
from dataclasses import dataclass
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


def enable_sleep_on_checkin(protomq, board_definition, sleep_config,
                            **extra):
    """Answer every later checkin (one per wake) with sleepEnabled and
    sleep_config, and extra fields (e.g. componentAdds), as IO re-enables
    sleep at each wake. Returns that response."""
    response = checkin_response(board_definition, sleepEnabled=True,
                                sleepConfig=sleep_config, **extra)
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


# What ConfigureSleep() logs arming the TIMER_S timer
TIMER_ARMED_RE = r"^\[sleep\] Timer wakeup set to %d seconds$" % TIMER_S


def expect_goodnight_and_sleep(serial, protomq, uid, mode, configured=True,
                               entering_loop=True, armed_re=TIMER_ARMED_RE):
    """The DUT says goodnight, disconnects from IO and WiFi, and sleeps in
    mode ("deep" or "light"). configured: it applied a SleepConfig just
    before (a runtime one, or one from a checkin it didn't skip), so it
    logs arming its wake source first: armed_re, the timer by default.
    entering_loop: run() switches to loopSleep() first, once per boot.
    Returns when the DUT's last line before sleeping arrived, as
    time.time()."""
    if configured:
        serial.expect(armed_re, timeout=GOODNIGHT_TIMEOUT)
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


# --- ext0 (RTC GPIO level) wake --------------------------------------------

# The DUT pin the digital_gpio probe drives (GPA2): an RTC GPIO, not a
# strapping pin, with internal pulls
EXT0_PIN = "D27"
# How long the DUT must stay asleep with the wake pin at its idle level, in
# seconds: before the probe drives it to the wake level, and after the wake
HOLD_S = 10
# How soon after the probe drives the wake level the DUT must wake, in s
EXT0_LATENCY_S = 2
# The firmware's sleep duration may differ from the one measured on this
# host by this much, in seconds (it rounds to whole seconds)
REPORTED_TOLERANCE_S = 3


@dataclass(frozen=True)
class Ext0Variant:
    """An ext0 wake: the wake pin's digital.Add direction (its pull, which
    ext0 takes), and the level that wakes the DUT; the pull holds the pin
    at the other level, its idle level, while the probe leaves it
    high-impedance."""
    name: str
    direction: str       # digital.Add gpioDirection
    direction_num: int   # ws_digital_Direction, as Handle_DigitalIO_Add() logs
    level: bool          # the wake level
    pull: str            # as ConfigureSleep() logs the pull

    @property
    def idle(self):
        return not self.level


EXT0_VARIANTS = {
    # Pulled down, idles LOW, wakes when driven HIGH
    "high": Ext0Variant("high", "D_INPUT_PULL_DOWN", 4, True, "DOWN"),
    # Pulled up, idles HIGH, wakes when driven LOW
    "low": Ext0Variant("low", "D_INPUT_PULL_UP", 2, False, "UP"),
}


def ext0_component_adds(variant):
    """The checkin.ComponentAdds adding the wake pin: an input with the
    variant's pull. The DUT publishes its level once per wake (sleep
    mode)."""
    return {"digitalAdds": [{"pin": EXT0_PIN,
                             "gpioDirection": variant.direction,
                             "sampleMode": "SM_EVENT"}]}


def ext0_sleep_config(mode, variant):
    """A SleepConfig in mode ("S_DEEP" or "S_LIGHT") waking on the wake pin
    at the variant's level."""
    return {"mode": mode, "ext0s": [{"pin": EXT0_PIN, "level": variant.level}]}


def ext0_armed_re(variant):
    """What ConfigureSleep() logs arming the variant's ext0 wake, the pull
    taken from the wake pin's digital component."""
    return (r"^\[sleep\] EXT0 wakeup set on pin %s \| Level %s \| "
            r"Pull: %s$" % (EXT0_PIN, "HIGH" if variant.level else "LOW",
                            variant.pull))


def expect_ext0_pin_added(serial, variant):
    """Expect Handle_DigitalIO_Add() to log adding the wake pin with the
    variant's pull."""
    serial.expect(r"^\[digital\] Added new pin:$", timeout=CHECKIN_TIMEOUT)
    serial.expect(rf"^Pin Name: {EXT0_PIN.lstrip('D')}$", timeout=5)
    serial.expect(rf"^Direction: {variant.direction_num}$", timeout=5)


def expect_ext0_pin_on_broker(protomq, uid, value):
    """The broker's next digital.Event: the wake pin, at value."""
    event = protomq.expect_d2b_message(uid, "digital.event",
                                       timeout=GOODNIGHT_TIMEOUT)
    assert event.get("pin") == EXT0_PIN, f"expected {EXT0_PIN}: {event}"
    # proto3 JSON leaves out a false bool
    got = event.get("value", {}).get("boolValue", False)
    assert got == value, f"expected {EXT0_PIN} {int(value)}: {event}"


def expect_ext0_pin_published(serial, protomq, uid, value, entering_loop):
    """loopSleep() publishes the wake pin's level once per wake, before the
    goodnight: value. entering_loop: run() enters loopSleep() first (once
    per boot)."""
    if entering_loop:
        serial.expect(r"^\[app\] Sleep enabled, entering loopSleep\(\)$",
                      timeout=GOODNIGHT_TIMEOUT)
    serial.expect(rf"^\[digital\] Publish Event: {EXT0_PIN} \| value: "
                  rf"{int(value)}$", timeout=GOODNIGHT_TIMEOUT)
    expect_ext0_pin_on_broker(protomq, uid, value)


def assert_stays_asleep(board, serial, protomq, uid, hold_s, wake_re):
    """For hold_s seconds, the wake pin at its idle level: the broker drops
    the DUT, and it neither wakes (no line matching wake_re, a compiled
    regex) nor reconnects."""
    since = serial.mark()
    assert_dropped(uid, watch_sleep(board, serial, protomq, uid, hold_s,
                                    wake_re=wake_re))
    woke = [l.text for l in serial.lines(since) if wake_re.search(l.text)]
    assert not woke, f"DUT woke with {EXT0_PIN} idle: {woke}"
    assert uid not in protomq.clients(), (
        f"DUT reconnected with {EXT0_PIN} idle")


def ext0_arrange(probe, serial, protomq, checkin, board_definition, mode,
                 variant):
    """Leave the wake pin high-impedance, so only its pull holds it idle,
    and check in adding it with the variant's pull; every later checkin
    re-adds it and re-enables sleep. Returns (uid, the wake checkin's
    response, the SleepConfig)."""
    probe.release(EXT0_PIN)
    adds = ext0_component_adds(variant)
    uid = checkin(adds, lambda: expect_ext0_pin_added(serial, variant))
    config = ext0_sleep_config(mode, variant)
    wake_response = enable_sleep_on_checkin(protomq, board_definition, config,
                                            componentAdds=adds)
    return uid, wake_response, config


def ext0_send_and_sleep(serial, protomq, uid, mode, variant, config):
    """Send the runtime SleepConfig: the DUT arms ext0, publishes the pin
    at its idle level, and sleeps. That publish is the regular loop's
    first event for the newly added pin, around the SleepConfig's
    arrival: loopSleep() finds the pin already sent and doesn't publish
    it again. Returns the DUT's last line before sleeping's time."""
    protomq.publish(uid, {"sleep": {"sleepConfig": config}})
    protomq.expect_b2d_message(uid, "sleep.sleepConfig", match=config,
                               timeout=5)
    serial.expect(ext0_armed_re(variant), timeout=GOODNIGHT_TIMEOUT)
    serial.expect(r"^Sleep Enabled: YES$", timeout=5)
    expect_ext0_pin_on_broker(protomq, uid, variant.idle)
    return expect_goodnight_and_sleep(serial, protomq, uid, mode,
                                      configured=False)


def drive_wake(probe, serial, variant, wake_line_re):
    """Drive the wake pin to the wake level, expect the first serial line
    matching wake_line_re within EXT0_LATENCY_S (+ margin), then drive the
    pin back to idle, or the DUT re-wakes as soon as it next sleeps.
    Returns (when the pin was driven, the wake line's match)."""
    driven_at = time.time()
    probe.drive(EXT0_PIN, variant.level)
    try:
        match = serial.expect(wake_line_re, timeout=EXT0_LATENCY_S + 3)
    finally:
        probe.drive(EXT0_PIN, variant.idle)
    return driven_at, match


def ext0_result(mode, variant, latency, asleep, reported, misses):
    """Log the ext0 wake's result as one EXT0RESULT line, and add misses
    for a slow wake or a firmware duration off from the measured one."""
    log.info("EXT0RESULT mode=%s variant=%s pin=%s pull=%s wake_level=%s "
             "latency_s=%.3f asleep_s=%.3f reported_s=%d", mode,
             variant.name, EXT0_PIN, variant.pull,
             "HIGH" if variant.level else "LOW", latency, asleep, reported)
    if latency > EXT0_LATENCY_S:
        misses.append(f"DUT woke {latency:.3f} s after {EXT0_PIN} went "
                      f"{'HIGH' if variant.level else 'LOW'}, expected "
                      f"within {EXT0_LATENCY_S} s")
    if abs(reported - asleep) > REPORTED_TOLERANCE_S:
        misses.append(f"firmware reports {reported} s asleep, measured "
                      f"{asleep:.3f} s (+/- {REPORTED_TOLERANCE_S} s)")


# --- I2C sensors ------------------------------------------------------------

# Two sensors on the i2c_bus probe's port (not behind its mux), both read
# at once after power-on (hil_sensors FAST)
I2C_SENSORS = ("bme688", "max44009")
# Their publish interval, in seconds. In sleep mode loopSleep() forces
# each driver's read as soon as it runs, so a wake's readings are due
# within one interval of its checkin completing, and before the goodnight.
I2C_PERIOD = 10


def i2c_sensors(i2c_devices, i2c_address_space, component_definition):
    """I2C_SENSORS as (i2c_helpers.I2cDevice, the Add's types map, plausible
    readings, address space) tuples, reading all their types."""
    import i2c_helpers as i2c
    from hil_sensors import SENSORS
    sensors = []
    for name in I2C_SENSORS:
        address, value_range, _, _ = SENSORS[name]
        dev = i2c.device(i2c_devices, component_definition, name, address)
        sensors.append((dev, dev.types(), value_range, i2c_address_space))
    return sensors


def i2c_component_adds(sensors):
    """The checkin.ComponentAdds adding sensors, each every I2C_PERIOD."""
    import i2c_helpers as i2c
    return {"i2cAdds": [i2c.add_msg(dev, space, types, period=I2C_PERIOD)
                        for dev, types, _, space in sensors]}


def _added(lines, sensors, variant):
    """Which of sensors (by name), and the ext0 pin if variant, lines show
    being added: Handle_Add()'s "Driver initialized:" then the driver's
    name, Handle_DigitalIO_Add()'s "Added new pin:" then its number."""
    added = set()
    for i, text in enumerate(lines[:-1]):
        if re.match(r"^\[i2c\] Driver initialized: ?$", text):
            added.add(lines[i + 1])
        if text == "[digital] Added new pin:" and \
                lines[i + 1] == f"Pin Name: {EXT0_PIN.lstrip('D')}":
            added.add(EXT0_PIN)
    wanted = {dev.name for dev, *_ in sensors}
    if variant is not None:
        wanted.add(EXT0_PIN)
    return added & wanted, wanted


def assert_added_since(serial, since, sensors, variant, label):
    """Every sensor (and the ext0 pin, if variant) was added since serial
    line since: the checkin's componentAdds, in whatever order it applies
    them."""
    added, wanted = _added([l.text for l in serial.lines(since)], sensors,
                           variant)
    assert added == wanted, (f"{label}: expected {sorted(wanted)} added, "
                             f"only {sorted(added)} were")


def assert_none_added_since(serial, since, label):
    """Nothing was added since serial line since: a skipped checkin."""
    lines = [l.text for l in serial.lines(since)]
    readds = [t for t in lines if re.match(r"^\[i2c\] Driver initialized", t)
              or t == "[digital] Added new pin:"]
    assert not readds, f"{label}: expected no component re-added: {readds}"


def i2c_readings_then_sleep(serial, protomq, uid, mode, sensors, variant,
                            label, misses, entering_loop):
    """After a checkin (its complete the broker's last expected D2B
    message): the DUT says goodnight and sleeps in mode, having published
    exactly one i2c.Event per sensor, each within I2C_PERIOD of the
    checkin completing and before the goodnight (and, if variant, the
    ext0 pin once, idle). Readings are checked against their plausible
    ranges. Returns (the DUT's last line before sleeping's time, {sensor
    name: seconds from checkin complete to its reading})."""
    import i2c_helpers as i2c
    complete_at = protomq.last_d2b_time(uid)
    slept_at = expect_goodnight_and_sleep(serial, protomq, uid, mode,
                                          configured=False,
                                          entering_loop=entering_loop)
    goodnight_at = protomq.last_d2b_time(uid)
    events = [(t, e) for t, e in protomq.d2b_timed_since(uid, "i2c.event", 0)
              if complete_at <= t <= goodnight_at]
    latencies = {}
    for dev, types, value_range, space in sensors:
        mine = [(t, e) for t, e in events
                if e.get("descriptor", {}).get("address") == dev.address]
        if len(mine) != 1:
            misses.append(f"{label}: {len(mine)} {dev.name} readings between "
                          "checkin complete and goodnight, expected 1")
            continue
        received, event = mine[0]
        values = i2c._check_event(dev, space, event, types, value_range)
        latencies[dev.name] = received - complete_at
        log.info("%s: %s reading %.2f s after checkin complete, %.2f s "
                 "before goodnight: %s", label, dev.name,
                 latencies[dev.name], goodnight_at - received, values)
        if latencies[dev.name] > I2C_PERIOD:
            misses.append(f"{label}: {dev.name} reading "
                          f"{latencies[dev.name]:.2f} s after checkin "
                          f"complete, expected within {I2C_PERIOD} s")
    if variant is not None:
        pins = [e for t, e in protomq.d2b_timed_since(uid, "digital.event", 0)
                if complete_at <= t <= goodnight_at
                and e.get("pin") == EXT0_PIN]
        values = [e.get("value", {}).get("boolValue", False) for e in pins]
        if values != [variant.idle]:
            misses.append(f"{label}: {EXT0_PIN} published {values} before "
                          f"goodnight, expected [{variant.idle}]")
    return slept_at, latencies


# --- "Stop sleeping" ------------------------------------------------------

# How long a DUT whose sleep was stopped must stay awake, in seconds:
# longer than the TIMER_S it slept for, so a sleep would show
STAY_AWAKE_S = TIMER_S + 15
# Any sign of sleeping: entering loopSleep(), entering a sleep, a deep
# sleep wake's reset, or a light sleep wake
SLEEPING_RE = re.compile(r"Sleep enabled, entering loopSleep|"
                         r"^\[sleep\] Entering (deep|light) sleep$|"
                         r"^rst:0x|Woke up from light sleep")


def stop_sleeping(protomq, board_definition):
    """IO's "stop sleeping": the device's sleep settings are wiped and
    sleep disabled. No message is sent: the broker answers the device's
    next checkin with sleepEnabled false and no SleepConfig. Returns that
    response."""
    response = checkin_response(board_definition)
    protomq.clear_autorespond()
    protomq.set_autorespond(
        "checkin.request", {"checkin": {"response": response}},
        match={"hardwareUid": board_definition.board_id})
    log.info("stop sleeping: next checkin answered without sleep")
    return response


def expect_sleep_stopped_checkin(protomq, uid, board_definition, response,
                                 timeout):
    """The DUT checks in after a wake and the broker answers response,
    which disables sleep: no sleepEnabled, no SleepConfig. The DUT
    publishes Complete."""
    protomq.expect_d2b_message(
        uid, "checkin.request",
        match={"hardwareUid": board_definition.board_id}, timeout=timeout)
    sent = protomq.expect_b2d_message(uid, "checkin.response",
                                      match=response, timeout=5)
    # proto3 JSON leaves out a false bool and an unset message
    assert not sent.get("sleepEnabled") and "sleepConfig" not in sent, (
        f"expected a checkin response disabling sleep: {sent}")
    protomq.expect_d2b_message(uid, "checkin.complete",
                               timeout=CHECKIN_TIMEOUT)


def assert_stays_awake(serial, protomq, uid, duration=STAY_AWAKE_S):
    """For duration seconds: the DUT sends no goodnight, stays connected,
    and logs no sign of sleeping."""
    since = serial.mark()
    protomq.assert_no_d2b_message(uid, "sleep.goodnight", within=duration)
    assert uid in protomq.clients(), (
        f"DUT left the broker within {duration} s of its sleep stopping")
    sleeping = [l.text for l in serial.lines(since)
                if SLEEPING_RE.search(l.text)]
    assert not sleeping, f"DUT slept after its sleep stopped: {sleeping}"
    log.info("DUT stayed awake and connected for %d s", duration)
