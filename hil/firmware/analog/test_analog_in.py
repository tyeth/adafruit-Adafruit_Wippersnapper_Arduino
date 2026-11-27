"""
HIL tests for Adafruit Wippersnapper firmware's analog component's input
handling.

How Does This Work?
A DAC "probe" drives the DUT's analog input to a known voltage, so the
values the DUT publishes can be checked against it.

Exercises:
- `src/components/analog/controller.cpp::Router()`
- `src/components/analog/controller.cpp::Handle_AnalogInAdd()`
- `src/components/analog/controller.cpp::Handle_AnalogInRemove()`
- `src/components/analog/controller.cpp::Router()` (analog.Modify)
- `src/components/analog/controller.cpp::update()`
- `src/components/analog/controller.cpp::EncodePublishPinEvent()`
- `src/components/analog/hardware.cpp::checkTimer()`
- `src/components/analog/hardware.cpp::checkEvent()`
- `src/components/analog/hardware.cpp::readValueRaw()`
- `src/components/analog/hardware.cpp::readVoltage()`
"""

import logging
import re
import statistics
import time

import pytest

import hil_board_utils
import hil_protomq

log = logging.getLogger("hil.analog")

# How long after checkin the DUT may take to handle a message, as the
# checkin fixture allows for the checkin itself.
CHECKIN_TIMEOUT = 30
# ws.sensor.Type values, as Handle_AnalogInAdd() prints them
READ_MODES = {"T_VOLTAGE": 15, "T_RAW": 18}
# Full scale of a T_RAW value: the firmware scales every ADC read to
# DEFAULT_ADC_RESOLUTION (src/components/analog/hardware.h) bits
RAW_MAX = 2 ** 16
# The DAC's output for the T_RAW tests, mid-scale so the ESP32 ADC's
# nonlinear ends don't matter
RAW_VOLTS = 1.65
# How far a T_RAW value (the sweep: the median of a detent's values) may be
# from the DAC's voltage, as a fraction of full scale. The ESP32's
# uncalibrated ADC reads low at the bottom of its range: 0.33V read a
# median of ~2900 (0.15V), 5.7% under, on the Feather ESP32 V2.
RAW_TOLERANCE = 0.08
# How far a T_VOLTAGE value may be from the DAC's voltage, in volts: the
# median of a test's values, and each one. On the ESP32, readVoltage() is
# one calibrated analogReadMilliVolts() read, so values have the ADC's
# noise spikes. Over 10 runs at 1.65V (30 values) the median was at most
# 0.043V off (usually ~25mV low), and single values -0.065V to +0.227V.
VOLTAGE_TOLERANCE = 0.1
VOLTAGE_SPIKE_TOLERANCE = 0.3
# The voltage sweep's version of RAW_NOISE, in volts: the same 5% of a
# 3.3V full scale. Not yet measured in T_VOLTAGE SM_EVENT mode.
VOLTAGE_NOISE = 0.165
# AI-MOD-01: the range a T_RAW value at 1.65V (mid-scale) must be in, as
# fractions of RAW_MAX, deliberately loose for the ESP32 ADC's
# nonlinearity; and how far consecutive T_RAW values may differ. The ADC's
# noise put single values up to 4672 (7.1%) from a held level's median.
RAW_PLAUSIBLE = (0.3, 0.7)
RAW_STABILITY_TOLERANCE = 0.1 * RAW_MAX
# AI-MOD-02: SM_EVENT's expected behaviour. HYSTERESIS_V is the change in
# volts an SM_EVENT pin must see before it publishes; the test steps the
# DAC by twice that, clear of the threshold. INITIAL_EVENT_ON_EVENT_MODE:
# whether switching to SM_EVENT publishes the current value once.
# EVENT_LATENCY_MAX: how long after the change its event may reach the
# broker, from the pot sweep's worst 0.356s with margin. NOTE: the firmware
# has no hysteresis today; checkEvent() publishes on any change of
# _value_raw.
HYSTERESIS_V = 0.1
# Skips the tests that need SM_EVENT to ignore the ADC's noise, until the
# firmware has a change threshold; remove it then.
NO_EVENT_THRESHOLD = pytest.mark.skip(
    reason="firmware: SM_EVENT has no change threshold; checkEvent() "
           "publishes on any change, so ADC noise floods (~20/s)")
INITIAL_EVENT_ON_EVENT_MODE = True
EVENT_LATENCY_MAX = 0.5
# How long an SM_EVENT pin held at a steady voltage must publish nothing,
# after its initial event
QUIET_WINDOW = 20
# How long after a voltage change to wait for an SM_EVENT pin's events
CHANGE_WINDOW = 30
# How far a T_VOLTAGE value the DUT publishes may be from the one it logs,
# which Print rounds to 2 decimals
VOLTAGE_LOG_ROUNDING = 0.005
# How far most of a detent's values must be from their median, as a
# fraction of full scale, and the share of them that must be. The ESP32's
# ADC noise has rare spikes: one held detent's 58 values were 1024-7680
# around a median of 3008, all but the 7680 (7.1%) within 5%. One spike
# is always allowed, since it can't move the median, for short rests with
# only a few values.
RAW_NOISE = 0.05
RAW_NOISE_INLIERS = 0.9
# SM_TIMER: the Add.period, and how many publishes the test checks
TIMER_PERIOD = 10
TIMER_SAMPLES = 3
# How far each gap between timer publishes, as the DUT logs them, may be
# from the period
TIMER_TOLERANCE = 0.35
# SM_EVENT sweep: a detented potentiometer across the MCU's reference
# voltage, turned one detent at a time from fully CCW to fully CW and
# back. Its range splits into POT_DETENTS equal bands, and each detent
# sits at the centre of its band, away from the band edges and the ends
# of the ADC's range, where it clips.
POT_DETENTS = 11
# How long the pot rests at each detent, from the DAC write until the next,
# and how long after the write the DUT's values count toward the detent.
# DETENT_SETTLE must cover the worst latency from the DAC write to the
# first value near the detent, as the DUT logs it and as ProtoMQ receives
# it, both timed on this host's clock (ProtoMQ runs here). On the Feather
# ESP32 V2 that was 0.356s at ProtoMQ over 9 full sweeps (180 detents),
# usually 20-80ms: the firmware loop sometimes stalls ~0.4s between
# publishes. The values after DETENT_SETTLE need a few for a stable median:
# a 0.6s rest (1-2 values) failed 2 of 4 sweeps, 0.75s (4-5 values) passed
# 4 of 4, so this is 0.75s with margin.
DETENT_DWELL = 1.0
DETENT_SETTLE = 0.5


def _power_on(board, power):
    """Power the DUT on, if it isn't, and wait for its serial port."""
    loc = board.entry["location"]
    hil_board_utils._power_on(loc["hub"], loc["port"], **power)
    port = str(hil_board_utils.wait_for_port(board.entry, timeout=15))
    if port != board.port:
        pytest.fail(f"board '{board.name}' came back on {port}, not "
                    f"{board.port}", pytrace=False)


@pytest.fixture(scope="module", autouse=True)
def powered_after_module(board, power):
    """Leave the DUT powered on for the tests after this module's."""
    yield
    _power_on(board, power)


@pytest.fixture(autouse=True)
def powered_between_tests(board, power, powered_after_module):
    """Power the DUT on before the test (a previous test left it off) and
    off after it. Autouse, so it's set up before the serial monitor opens
    the port and torn down after it closes."""
    _power_on(board, power)
    loc = board.entry["location"]
    yield
    hil_board_utils._power_off(loc["hub"], loc["port"], **power)
    hil_board_utils.wait_for_port_gone(board.entry, timeout=10)
    hil_board_utils.clear_port(board.name)


@pytest.fixture
def analog_pin(dac_probe):
    """The DUT's analog input pin under test: the first pin in the board
    entry's analog_input probe's pin_map, which the DAC probe drives."""
    return next(iter(dac_probe.pin_map))


# The value part of encodeAnalogInEventRaw()'s and
# encodeAnalogInEventVoltage()'s log lines; group 1 is the value, a whole
# number for T_RAW, volts to 2 decimals for T_VOLTAGE
EVENT_VALUE_RE = {"T_RAW": r"Raw Value: (\d+)\.\d+",
                  "T_VOLTAGE": r"Voltage: (\d+\.\d+)"}


def _event_re(pin, read_mode="T_RAW"):
    """Matches the DUT's log line for a read_mode event of pin; group 1 is
    the value."""
    return re.compile(rf"^\[analog\] Pin: {pin} \| {EVENT_VALUE_RE[read_mode]}$")


def _serial_events(serial, pin, since, read_mode="T_RAW"):
    """The SerialLines from since on that log a read_mode event for pin."""
    regex = _event_re(pin, read_mode)
    return [l for l in serial.lines(since) if regex.search(l.text)]


def _ref_voltage(board_definition):
    """The board definition's mcuRefVoltage, in volts."""
    return float(board_definition.definition["mcuRefVoltage"])


def _add_msg(pin, read_mode, sample_mode, period=None, ref_voltage=None):
    """An analog.Add for pin, with period and ref_voltage if given."""
    add = {"pin": pin, "readMode": read_mode, "sampleMode": sample_mode}
    if period is not None:
        add["period"] = period
    if ref_voltage is not None:
        add["refVoltage"] = ref_voltage
    return add


def _expect_added(serial, pin, read_mode, period=None):
    """Expect Handle_AnalogInAdd() to log adding pin."""
    serial.expect(r"^\[analog\] Added new pin:$", timeout=CHECKIN_TIMEOUT)
    serial.expect(rf"^Pin Name: {pin}$", timeout=5)
    serial.expect(rf"^Period: {(period or 0) * 1000}\.00$", timeout=5)
    serial.expect(rf"^Read Mode: {READ_MODES[read_mode]}$", timeout=5)


def _removed_re(pin):
    """Matches Handle_AnalogInRemove()'s log line for pin."""
    return re.compile(rf"^\[analog\] Removed pin: {pin}$")


def _add(serial, protomq, uid, pin, read_mode, sample_mode, period=None,
         ref_voltage=None):
    """Send an analog.Add for pin, with period and ref_voltage if given,
    and expect the DUT to add it."""
    add = _add_msg(pin, read_mode, sample_mode, period, ref_voltage)
    protomq.publish(uid, {"analog": {"add": add}})
    protomq.expect_b2d_message(uid, "analog.add", match=add, timeout=5)
    _expect_added(serial, pin, read_mode, period)


def _modify(serial, protomq, uid, pin, read_mode, sample_mode, period=None,
            ref_voltage=None):
    """Send an analog.Modify removing pin and adding it back with these
    settings, and expect the DUT to remove then add it. Returns the
    time.time() it was sent."""
    add = _add_msg(pin, read_mode, sample_mode, period, ref_voltage)
    modify = {"remove": {"pin": pin}, "add": add}
    sent = time.time()
    protomq.publish(uid, {"analog": {"modify": modify}})
    protomq.expect_b2d_message(uid, "analog.modify", match=modify, timeout=5)
    serial.expect(_removed_re(pin), timeout=CHECKIN_TIMEOUT)
    _expect_added(serial, pin, read_mode, period)
    return sent


def _remove(serial, protomq, uid, pin):
    """Send an analog.Remove for pin and expect the DUT to remove it.
    Returns the time.time() it was sent and the index of the DUT's
    removal log line."""
    sent = time.time()
    protomq.publish(uid, {"analog": {"remove": {"pin": pin}}})
    protomq.expect_b2d_message(uid, "analog.remove", match={"pin": pin},
                               timeout=5)
    m = serial.expect(_removed_re(pin), timeout=CHECKIN_TIMEOUT)
    line = next(l for l in reversed(serial.lines()) if l.text == m.string)
    return sent, line.index


def _collect_events(protomq, uid, pin, duration):
    """Every analog.event for pin the broker gets in the next duration
    seconds, after the last one expected, as (time.time() it was received,
    the event's sensor value)."""
    events = []
    deadline = time.monotonic() + duration
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return events
        try:
            event = protomq.expect_d2b_message(
                uid, "analog.event", match={"pin": pin}, timeout=remaining)
        except hil_protomq.ProtoMQTimeout:
            return events
        events.append((protomq.last_d2b_time(uid), event.get("value", {})))


def _expect_event(serial, protomq, uid, pin, read_mode, timeout):
    """Expect the DUT to log and publish a read_mode event for pin, and
    that the next analog.event the broker gets is that one. Returns the
    published value."""
    m = serial.expect(_event_re(pin, read_mode), timeout=timeout)
    serial.expect(r"Published!$", timeout=5)

    # No match: the next event of any pin or value, so a stray one fails
    event = protomq.expect_d2b_message(uid, "analog.event", timeout=5)
    assert event.get("pin") == pin, f"expected an event for {pin}: {event}"
    sensor_event = event.get("value", {})
    assert sensor_event.get("type") == read_mode, (
        f"expected a {read_mode} sensor event: {event}")
    value = sensor_event.get("floatValue")
    if read_mode == "T_RAW":
        logged_ok = value == int(m.group(1))
    else:
        logged_ok = (value is not None and
                     abs(value - float(m.group(1))) <= VOLTAGE_LOG_ROUNDING)
    assert logged_ok, (
        f"published {value}, but the DUT logged {m.group(1)}: {event}")
    return value


def _assert_raw_near(pin, value, volts, ref_voltage, what=""):
    """value, a T_RAW reading of pin, is within RAW_TOLERANCE of volts."""
    expected = volts / ref_voltage * RAW_MAX
    assert abs(value - expected) <= RAW_TOLERANCE * RAW_MAX, (
        f"{pin} read {value:.0f}{what}, expected {expected:.0f} "
        f"+/- {RAW_TOLERANCE * RAW_MAX:.0f} for the DAC's {volts:.3f}V "
        f"of a {ref_voltage}V reference")


def _expect_published(protomq, uid, pin, value, timeout, read_mode="T_RAW"):
    """Expect the broker to get a read_mode analog.event of value for pin,
    as the DUT logged it, skipping any others it got first. float_value
    is in a oneof, so the JSON has it even when it's 0. A T_VOLTAGE value
    is logged to 2 decimals, so it matches to VOLTAGE_LOG_ROUNDING."""
    if read_mode == "T_RAW":
        return protomq.expect_d2b_message(
            uid, "analog.event", timeout=timeout,
            match={"pin": pin, "value": {"type": "T_RAW",
                                         "floatValue": float(value)}})
    deadline = time.monotonic() + timeout
    while True:
        event = protomq.expect_d2b_message(
            uid, "analog.event", match={"pin": pin, "value": {"type": read_mode}},
            timeout=max(deadline - time.monotonic(), 0.1))
        if abs(event["value"].get("floatValue", 0) - value) <= VOLTAGE_LOG_ROUNDING:
            return event


def _sweep_scale(read_mode, volts, ref_voltage):
    """For a sweep detent at volts: the value a read_mode reading should
    have, how far a detent's median may be from it, and how far most of
    its values may be from their median."""
    if read_mode == "T_RAW":
        return (volts / ref_voltage * RAW_MAX, RAW_TOLERANCE * RAW_MAX,
                RAW_NOISE * RAW_MAX)
    return volts, VOLTAGE_TOLERANCE, VOLTAGE_NOISE


def _fmt(read_mode, value):
    """value as the DUT logs a read_mode reading."""
    return f"{value:.0f}" if read_mode == "T_RAW" else f"{value:.3f}V"


def _turn_to_detent(serial, protomq, uid, dac_probe, pin, volts, ref_voltage,
                    previous=None, read_mode="T_RAW"):
    """Turn the simulated pot to volts and rest there for DETENT_DWELL.
    The median of the values the DUT logs from DETENT_SETTLE after the DAC
    write must be near volts, and most of them near that median; the last
    must reach the broker. Values are read_mode readings. Returns the
    median, and the latency from the
    write to the first value nearer that median than previous, the last
    detent's median, as logged on serial and as received by ProtoMQ, in
    seconds; without previous, the first value is timed.

    SM_EVENT publishes every change, so the ADC's noise makes a stream of
    values at a held detent; a steady read publishes once, maybe before
    DETENT_SETTLE, so with none after it the last value since the turn
    counts."""
    regex = _event_re(pin, read_mode)
    parse = int if read_mode == "T_RAW" else float
    expected, tolerance, noise = _sweep_scale(read_mode, volts, ref_voltage)
    since = serial.mark()
    written = time.time()  # SerialLine.time's and ProtoMQ's clock
    volts = dac_probe.set_voltage(pin, volts)
    time.sleep(DETENT_DWELL)

    events = [(l.time, parse(regex.search(l.text).group(1)))
              for l in _serial_events(serial, pin, since, read_mode)]
    assert events, f"{pin} published nothing in {DETENT_DWELL}s at {volts:.3f}V"
    values = [v for t, v in events if t >= written + DETENT_SETTLE]
    if not values:
        values = [events[-1][1]]
    median = statistics.median(values)
    shown = (f"{len(values)} values, {_fmt(read_mode, min(values))}-"
             f"{_fmt(read_mode, max(values))}")

    # Latency: the first value after the write that's nearer this detent
    # than the last one. The bands' tolerances overlap, and the ADC reads
    # every level a little low, so nearness to the measured medians is
    # what tells the detents apart
    first = next(((i, t, v) for i, (t, v) in enumerate(events)
                  if t >= written and (previous is None or
                                       abs(v - median) < abs(v - previous))),
                 None)
    assert first, (f"{pin} published nothing nearer "
                   f"{_fmt(read_mode, median)} than "
                   f"{_fmt(read_mode, previous)} after the DAC went to "
                   f"{volts:.3f}V: {[v for _, v in events]}")
    serial_latency = first[1] - written
    _expect_published(protomq, uid, pin, first[2], timeout=2, read_mode=read_mode)
    broker_latency = protomq.last_d2b_time(uid) - written

    log.info("%s at %.3fV (expected %s): median %s of %s; reached in "
             "%.3fs serial, %.3fs broker", pin, volts,
             _fmt(read_mode, expected), _fmt(read_mode, median), shown,
             serial_latency, broker_latency)
    assert abs(median - expected) <= tolerance, (
        f"{pin} read a median of {_fmt(read_mode, median)} ({shown}), "
        f"expected {_fmt(read_mode, expected)} +/- "
        f"{_fmt(read_mode, tolerance)} for the DAC's {volts:.3f}V of a "
        f"{ref_voltage}V reference")
    outliers = [v for v in values if abs(v - median) > noise]
    allowed = max(1, (1 - RAW_NOISE_INLIERS) * len(values))
    assert len(outliers) <= allowed, (
        f"{pin} at {volts:.3f}V read {len(outliers)} of {shown} over "
        f"{_fmt(read_mode, noise)} from their median "
        f"{_fmt(read_mode, median)}, more than {allowed:.0f}: {outliers}")
    # The last value reached the broker too, unless it's the one timed
    if first[0] != len(events) - 1:
        _expect_published(protomq, uid, pin, events[-1][1], timeout=2,
                          read_mode=read_mode)
    return median, serial_latency, broker_latency


def _at_end(value, read_mode="T_RAW", ref_voltage=None):
    """Whether a read_mode value is within its sweep tolerance of either
    end of the scale, where the ADC clips, so neighbouring detents may
    read the same."""
    if read_mode == "T_RAW":
        full, tolerance = RAW_MAX, RAW_TOLERANCE * RAW_MAX
    else:
        full, tolerance = ref_voltage, VOLTAGE_TOLERANCE
    return value <= tolerance or value >= full - tolerance


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_analog_input_add_timer_raw_fixed_voltage(board, board_definition, secrets_name,
                                    flashed_board, serial, power, device_uid,
                                    protomq, dac_probe, analog_pin, checkin):
    # --- Arrange ------------------------------------------------------
    # The DAC is released through the power cut and checkin, then drives
    # the pin to mid-scale
    uid = checkin()
    ref_voltage = _ref_voltage(board_definition)
    volts = dac_probe.set_voltage(analog_pin, RAW_VOLTS)

    # --- Act ----------------------------------------------------------
    added = serial.mark()
    _add(serial, protomq, uid, analog_pin, "T_RAW", "SM_TIMER",
         period=TIMER_PERIOD, ref_voltage=ref_voltage)

    # --- Assert -------------------------------------------------------
    # checkTimer() fires once millis() passes the period, so the first
    # publish may come right away; each after that comes a period later
    for _ in range(TIMER_SAMPLES):
        value = _expect_event(serial, protomq, uid, analog_pin, "T_RAW",
                              timeout=TIMER_PERIOD + TIMER_TOLERANCE + 1)
        _assert_raw_near(analog_pin, value, volts, ref_voltage)

    lines = _serial_events(serial, analog_pin, added)
    assert len(lines) == TIMER_SAMPLES, (
        f"expected {TIMER_SAMPLES} timer publishes for {analog_pin}: "
        f"{[l.text for l in lines]}")
    gaps = [b.time - a.time for a, b in zip(lines, lines[1:])]
    shown = ", ".join(f"{g:.3f}" for g in gaps)
    assert all(abs(g - TIMER_PERIOD) <= TIMER_TOLERANCE for g in gaps), (
        f"publishes should be {TIMER_PERIOD}s +/- {TIMER_TOLERANCE}s apart: "
        f"[{shown}]")


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_analog_input_add_timer_voltage_fixed_voltage(
        board, board_definition, secrets_name, flashed_board, serial, power,
        device_uid, protomq, dac_probe, analog_pin, checkin):
    # --- Arrange ------------------------------------------------------
    # The DAC is released through the power cut and checkin, then drives
    # the pin to mid-scale
    uid = checkin()
    ref_voltage = _ref_voltage(board_definition)
    volts = dac_probe.set_voltage(analog_pin, RAW_VOLTS)

    # --- Act ----------------------------------------------------------
    added = serial.mark()
    _add(serial, protomq, uid, analog_pin, "T_VOLTAGE", "SM_TIMER",
         period=TIMER_PERIOD, ref_voltage=ref_voltage)

    # --- Assert -------------------------------------------------------
    # checkTimer() fires once millis() passes the period, so the first
    # publish may come right away; each after that comes a period later
    values = []
    for _ in range(TIMER_SAMPLES):
        value = _expect_event(serial, protomq, uid, analog_pin, "T_VOLTAGE",
                              timeout=TIMER_PERIOD + TIMER_TOLERANCE + 1)
        log.info("%s read %.3fV, DAC at %.3fV: %+.3fV", analog_pin, value,
                 volts, value - volts)
        assert abs(value - volts) <= VOLTAGE_SPIKE_TOLERANCE, (
            f"{analog_pin} read {value:.3f}V, expected the DAC's "
            f"{volts:.3f}V +/- {VOLTAGE_SPIKE_TOLERANCE}V")
        values.append(value)
    median = statistics.median(values)
    assert abs(median - volts) <= VOLTAGE_TOLERANCE, (
        f"{analog_pin} read a median of {median:.3f}V "
        f"({', '.join(f'{v:.3f}' for v in values)}), expected the DAC's "
        f"{volts:.3f}V +/- {VOLTAGE_TOLERANCE}V")

    lines = _serial_events(serial, analog_pin, added, "T_VOLTAGE")
    assert len(lines) == TIMER_SAMPLES, (
        f"expected {TIMER_SAMPLES} timer publishes for {analog_pin}: "
        f"{[l.text for l in lines]}")
    gaps = [b.time - a.time for a, b in zip(lines, lines[1:])]
    shown = ", ".join(f"{g:.3f}" for g in gaps)
    assert all(abs(g - TIMER_PERIOD) <= TIMER_TOLERANCE for g in gaps), (
        f"publishes should be {TIMER_PERIOD}s +/- {TIMER_TOLERANCE}s apart: "
        f"[{shown}]")

def _pot_sweep(checkin, serial, protomq, board_definition, dac_probe, pin,
               read_mode):
    """Check the DUT in and add pin as a read_mode SM_EVENT input across a
    simulated detented pot, then turn it one detent at a time CW to the top
    and CCW back to the bottom. Each detent's median must be near its
    voltage, higher than the last on the way up and lower on the way down,
    unless both clip at an end, and the latency to it within
    DETENT_SETTLE."""
    # --- Arrange ------------------------------------------------------
    # The simulated pot is across the MCU's reference voltage, starting at
    # its fully CCW detent
    uid = checkin()
    ref_voltage = _ref_voltage(board_definition)
    top = min(ref_voltage, dac_probe.max_voltage)
    detents = [top * (i + 0.5) / POT_DETENTS for i in range(POT_DETENTS)]
    dac_probe.set_voltage(pin, detents[0])
    # No period or ref_voltage: SM_EVENT doesn't use a period, and native
    # pins use the checkin's reference voltage
    _add(serial, protomq, uid, pin, read_mode, "SM_EVENT")

    # --- Act / Assert -------------------------------------------------
    sweep = detents[1:] + detents[-2::-1]
    readings = []
    latencies = []
    previous = None
    for volts in sweep:
        value, serial_latency, broker_latency = _turn_to_detent(
            serial, protomq, uid, dac_probe, pin, volts, ref_voltage,
            previous[1] if previous else None, read_mode)
        readings.append((volts, value))
        latencies.append((serial_latency, broker_latency))
        if previous is not None:
            prev_volts, prev_value = previous
            rising = volts > prev_volts
            moved = value > prev_value if rising else value < prev_value
            clipped = (_at_end(value, read_mode, ref_voltage) and
                       _at_end(prev_value, read_mode, ref_voltage))
            assert moved or clipped, (
                f"{pin} read {_fmt(read_mode, prev_value)} at "
                f"{prev_volts:.3f}V then {_fmt(read_mode, value)} at "
                f"{volts:.3f}V, should have "
                f"{'risen' if rising else 'fallen'}; sweep so far: "
                f"{[(f'{v:.3f}V', _fmt(read_mode, r)) for v, r in readings]}")
        previous = (volts, value)

    # The settle time the values above relied on covered every detent's
    # latency, on serial and at the broker
    worst = max(max(pair) for pair in latencies)
    log.info("latency to the first value near a detent: serial max %.3fs, "
             "broker max %.3fs, over %d detents",
             max(l[0] for l in latencies), max(l[1] for l in latencies),
             len(latencies))
    assert worst <= DETENT_SETTLE, (
        f"{pin} took up to {worst:.3f}s to publish a value near a new "
        f"detent, over DETENT_SETTLE {DETENT_SETTLE}s: {latencies}")


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_analog_input_event_raw_pot_sweep(board, board_definition,
                                          secrets_name, flashed_board, serial,
                                          power, device_uid, protomq,
                                          dac_probe, analog_pin, checkin):
    _pot_sweep(checkin, serial, protomq, board_definition, dac_probe,
               analog_pin, "T_RAW")


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_analog_input_event_voltage_pot_sweep(board, board_definition,
                                              secrets_name, flashed_board,
                                              serial, power, device_uid,
                                              protomq, dac_probe, analog_pin,
                                              checkin):
    """The pot sweep in T_VOLTAGE."""
    _pot_sweep(checkin, serial, protomq, board_definition, dac_probe,
               analog_pin, "T_VOLTAGE")


def _assert_voltage_near(pin, value, volts, what=""):
    """value, a single T_VOLTAGE reading of pin, is within
    VOLTAGE_SPIKE_TOLERANCE of volts. Single readings carry the ADC's noise
    spikes (up to +0.227V seen), so VOLTAGE_TOLERANCE is for medians."""
    assert abs(value - volts) <= VOLTAGE_SPIKE_TOLERANCE, (
        f"{pin} read {value:.3f}V{what}, expected the DAC's {volts:.3f}V "
        f"+/- {VOLTAGE_SPIKE_TOLERANCE}V")


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_analog_input_modify_read_mode(board, board_definition, secrets_name,
                                       flashed_board, serial, power,
                                       device_uid, protomq, dac_probe,
                                       analog_pin, checkin):
    """AI-MOD-01: Modify switches the read mode from T_VOLTAGE to T_RAW,
    the old mode stops, and the new one reports a plausible, stable
    value."""
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    ref_voltage = _ref_voltage(board_definition)
    volts = dac_probe.set_voltage(analog_pin, 1.65)
    _add(serial, protomq, uid, analog_pin, "T_VOLTAGE", "SM_TIMER",
         period=TIMER_PERIOD, ref_voltage=ref_voltage)
    # A1: two T_VOLTAGE events near the DAC's voltage
    for _ in range(2):
        value = _expect_event(serial, protomq, uid, analog_pin, "T_VOLTAGE",
                              timeout=TIMER_PERIOD + TIMER_TOLERANCE + 1)
        _assert_voltage_near(analog_pin, value, volts)

    # --- Act ----------------------------------------------------------
    _modify(serial, protomq, uid, analog_pin, "T_RAW", "SM_TIMER",
            period=TIMER_PERIOD, ref_voltage=ref_voltage)
    events = _collect_events(protomq, uid, analog_pin, 3 * TIMER_PERIOD)

    # --- Assert -------------------------------------------------------
    types = [e.get("type") for _, e in events]
    assert "T_RAW" in types, f"no T_RAW event after the Modify: {events}"
    # A2: nothing but T_RAW from the first T_RAW event on
    after = types[types.index("T_RAW"):]
    assert set(after) == {"T_RAW"}, (
        f"{analog_pin} published {after} after its first T_RAW event")
    raw = [e.get("floatValue") for _, e in events if e.get("type") == "T_RAW"]
    log.info("%s T_RAW values after the Modify: %s", analog_pin, raw)
    # A3: at least 2 T_RAW events in 3 periods
    assert len(raw) >= 2, (
        f"expected at least 2 T_RAW events in {3 * TIMER_PERIOD}s: {raw}")
    # A4: each plausible for mid-scale
    low, high = (f * RAW_MAX for f in RAW_PLAUSIBLE)
    assert all(low <= v <= high for v in raw), (
        f"{analog_pin} T_RAW values {raw} should be {low:.0f}-{high:.0f} "
        f"for {volts:.3f}V")
    # A5: stable from one to the next
    steps = [abs(b - a) for a, b in zip(raw, raw[1:])]
    assert all(d <= RAW_STABILITY_TOLERANCE for d in steps), (
        f"{analog_pin} T_RAW values {raw} changed by {steps}, more than "
        f"{RAW_STABILITY_TOLERANCE:.0f}")


@NO_EVENT_THRESHOLD
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_analog_input_modify_sample_mode(board, board_definition,
                                         secrets_name, flashed_board, serial,
                                         power, device_uid, protomq,
                                         dac_probe, analog_pin, checkin):
    """AI-MOD-02: Modify switches the sample mode from SM_TIMER to
    SM_EVENT, which stops the periodic publishes and publishes on a change
    of more than HYSTERESIS_V."""
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    ref_voltage = _ref_voltage(board_definition)
    volts = dac_probe.set_voltage(analog_pin, 1.0)
    _add(serial, protomq, uid, analog_pin, "T_VOLTAGE", "SM_TIMER",
         period=TIMER_PERIOD, ref_voltage=ref_voltage)
    # A1: two timer events near the DAC's voltage
    for _ in range(2):
        value = _expect_event(serial, protomq, uid, analog_pin, "T_VOLTAGE",
                              timeout=TIMER_PERIOD + TIMER_TOLERANCE + 1)
        _assert_voltage_near(analog_pin, value, volts)

    # --- Act ----------------------------------------------------------
    _modify(serial, protomq, uid, analog_pin, "T_VOLTAGE", "SM_EVENT",
            period=TIMER_PERIOD, ref_voltage=ref_voltage)
    held = _collect_events(protomq, uid, analog_pin, 2 * TIMER_PERIOD)
    changed_at = time.time()
    new_volts = dac_probe.set_voltage(analog_pin, 1.0 + 2 * HYSTERESIS_V)
    changed = _collect_events(protomq, uid, analog_pin, EVENT_LATENCY_MAX + 2)

    # --- Assert -------------------------------------------------------
    # A2: the timer stopped; at most the initial event while held
    expected = 1 if INITIAL_EVENT_ON_EVENT_MODE else 0
    assert len(held) == expected, (
        f"expected {expected} event(s) in {2 * TIMER_PERIOD}s at a held "
        f"{volts:.3f}V after switching to SM_EVENT, got {len(held)}: "
        f"{[e.get('floatValue') for _, e in held]}")
    # A3: exactly one event for the step
    assert len(changed) == 1, (
        f"expected 1 event after the DAC went {volts:.3f}V -> "
        f"{new_volts:.3f}V, got {len(changed)}: "
        f"{[e.get('floatValue') for _, e in changed]}")
    received, event = changed[0]
    # A4: soon after the change
    latency = received - changed_at
    assert latency <= EVENT_LATENCY_MAX, (
        f"the event reached the broker {latency:.3f}s after the change, "
        f"over {EVENT_LATENCY_MAX}s")
    # A5: near the new voltage
    _assert_voltage_near(analog_pin, event.get("floatValue"), new_volts)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_analog_input_add_remove(board, board_definition, secrets_name,
                                 flashed_board, serial, power, device_uid,
                                 protomq, dac_probe, analog_pin, checkin):
    """AI-ADDREM-01: readings start after an Add, stop after a Remove, and
    the pin can be added again."""
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    ref_voltage = _ref_voltage(board_definition)
    volts = dac_probe.set_voltage(analog_pin, 1.65)
    _add(serial, protomq, uid, analog_pin, "T_VOLTAGE", "SM_TIMER",
         period=TIMER_PERIOD, ref_voltage=ref_voltage)
    # A1: one event near the DAC's voltage
    value = _expect_event(serial, protomq, uid, analog_pin, "T_VOLTAGE",
                          timeout=TIMER_PERIOD + TIMER_TOLERANCE + 1)
    _assert_voltage_near(analog_pin, value, volts)

    # --- Act ----------------------------------------------------------
    _, removed = _remove(serial, protomq, uid, analog_pin)
    removed_events = _collect_events(protomq, uid, analog_pin,
                                     2 * TIMER_PERIOD)
    readded = serial.mark()
    _add(serial, protomq, uid, analog_pin, "T_VOLTAGE", "SM_TIMER",
         period=TIMER_PERIOD, ref_voltage=ref_voltage)
    readded_events = _collect_events(protomq, uid, analog_pin,
                                     2 * TIMER_PERIOD)

    # --- Assert -------------------------------------------------------
    # A2: nothing published while removed, by the DUT's log order: no
    # event line between its removal line and the re-add, and none at
    # the broker meanwhile
    regex = _event_re(analog_pin, "T_VOLTAGE")
    logged = [l.text for l in serial.lines(removed + 1)[:readded - removed - 1]
              if regex.search(l.text)]
    assert not logged, (
        f"{analog_pin} logged events after it was removed: {logged}")
    assert not removed_events, (
        f"the broker got events for {analog_pin} after it was removed: "
        f"{[e.get('floatValue') for _, e in removed_events]}")
    # A3: publishing again after the re-add
    assert readded_events, (
        f"no events for {analog_pin} in {2 * TIMER_PERIOD}s after adding "
        f"it again")


@NO_EVENT_THRESHOLD
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_analog_input_event_quiet_when_steady(board, board_definition,
                                              secrets_name, flashed_board,
                                              serial, power, device_uid,
                                              protomq, dac_probe, analog_pin,
                                              checkin):
    """An SM_EVENT pin held at a steady 1.65V publishes its initial value
    (if INITIAL_EVENT_ON_EVENT_MODE), then nothing: the ADC's noise
    mustn't count as a change."""
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    volts = dac_probe.set_voltage(analog_pin, 1.65)

    # --- Act ----------------------------------------------------------
    _add(serial, protomq, uid, analog_pin, "T_RAW", "SM_EVENT")
    if INITIAL_EVENT_ON_EVENT_MODE:
        _expect_event(serial, protomq, uid, analog_pin, "T_RAW",
                      timeout=EVENT_LATENCY_MAX + 1)
    events = _collect_events(protomq, uid, analog_pin, QUIET_WINDOW)

    # --- Assert -------------------------------------------------------
    values = [e.get("floatValue") for _, e in events]
    assert not events, (
        f"{analog_pin} held at {volts:.3f}V published {len(events)} SM_EVENT "
        f"values in {QUIET_WINDOW}s, expected none: {values[:20]}"
        f"{' ...' if len(values) > 20 else ''}")


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_analog_input_event_voltage_publishes_on_change(
        board, board_definition, secrets_name, flashed_board, serial, power,
        device_uid, protomq, dac_probe, analog_pin, checkin):
    """A T_VOLTAGE SM_EVENT pin publishes when its voltage changes by far
    more than the ADC's noise. Only checks that it publishes, not how
    often, so it doesn't depend on SM_EVENT having a threshold."""
    # --- Arrange ------------------------------------------------------
    # Added at 1.0V; whatever it publishes while it settles is skipped
    uid = checkin()
    volts = dac_probe.set_voltage(analog_pin, 1.0)
    _add(serial, protomq, uid, analog_pin, "T_VOLTAGE", "SM_EVENT")
    _collect_events(protomq, uid, analog_pin, 1)

    # --- Act ----------------------------------------------------------
    changed_at = time.time()
    new_volts = dac_probe.set_voltage(analog_pin, 2.0)
    events = _collect_events(protomq, uid, analog_pin, CHANGE_WINDOW)

    # --- Assert -------------------------------------------------------
    # At least one event near the new voltage within CHANGE_WINDOW, and the
    # first within EVENT_LATENCY_MAX
    near = [(t - changed_at, e.get("floatValue")) for t, e in events
            if e.get("type") == "T_VOLTAGE"
            and abs(e.get("floatValue", 0) - new_volts) <= VOLTAGE_SPIKE_TOLERANCE]
    assert near, (
        f"{analog_pin} published no T_VOLTAGE event near {new_volts:.3f}V "
        f"in {CHANGE_WINDOW}s after the DAC went {volts:.3f}V -> "
        f"{new_volts:.3f}V; got {[e for _, e in events]}")
    latency, value = near[0]
    log.info("%s published %.3fV %.3fs after the change", analog_pin, value,
             latency)
    assert latency <= EVENT_LATENCY_MAX, (
        f"{analog_pin}'s first event near {new_volts:.3f}V reached the "
        f"broker {latency:.3f}s after the change, over {EVENT_LATENCY_MAX}s")
