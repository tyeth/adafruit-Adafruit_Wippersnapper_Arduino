"""
HIL tests for Adafruit Wippersnapper firmware's analog component's input
handling.

How Does This Work?
A DAC "probe" drives the DUT's analog input to a known voltage, so the
values the DUT publishes can be checked against it.

Exercises:
- `src/components/analog/controller.cpp::Router()`
- `src/components/analog/controller.cpp::Handle_AnalogInAdd()`
- `src/components/analog/controller.cpp::update()`
- `src/components/analog/controller.cpp::EncodePublishPinEvent()`
- `src/components/analog/hardware.cpp::checkTimer()`
- `src/components/analog/hardware.cpp::checkEvent()`
- `src/components/analog/hardware.cpp::readValueRaw()`
"""

import logging
import re
import statistics
import time

import pytest

import hil_board_utils

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
# How far most of a detent's values must be from their median, as a
# fraction of full scale, and the share of them that must be. The ESP32's
# ADC noise has rare spikes: one held detent's 58 values were 1024-7680
# around a median of 3008, all but the 7680 (7.1%) within 5%.
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
# it, both timed on this host's clock (ProtoMQ runs here). Two 20-detent
# sweeps on the Feather ESP32 V2 saw at most 0.178s on serial and 0.197s
# at ProtoMQ; this is that worst case with ~2x margin.
DETENT_DWELL = 3
DETENT_SETTLE = 0.4


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


def _event_re(pin):
    """Matches encodeAnalogInEventRaw()'s log line for pin; group 1 is
    the value."""
    return re.compile(rf"^\[analog\] Pin: {pin} \| Raw Value: (\d+)\.\d+$")


def _serial_events(serial, pin, since):
    """The SerialLines from since on that log an event for pin."""
    regex = _event_re(pin)
    return [l for l in serial.lines(since) if regex.search(l.text)]


def _ref_voltage(board_definition):
    """The board definition's mcuRefVoltage, in volts."""
    return float(board_definition.definition["mcuRefVoltage"])


def _add(serial, protomq, uid, pin, read_mode, sample_mode, period=None,
         ref_voltage=None):
    """Send an analog.Add for pin, with period and ref_voltage if given,
    and expect the DUT to add it."""
    add = {"pin": pin, "readMode": read_mode, "sampleMode": sample_mode}
    if period is not None:
        add["period"] = period
    if ref_voltage is not None:
        add["refVoltage"] = ref_voltage
    protomq.publish(uid, {"analog": {"add": add}})
    protomq.expect_b2d_message(uid, "analog.add", match=add, timeout=5)
    serial.expect(r"^\[analog\] Added new pin:$", timeout=CHECKIN_TIMEOUT)
    serial.expect(rf"^Pin Name: {pin}$", timeout=5)
    serial.expect(rf"^Period: {(period or 0) * 1000}\.00$", timeout=5)
    serial.expect(rf"^Read Mode: {READ_MODES[read_mode]}$", timeout=5)


def _expect_raw_event(serial, protomq, uid, pin, timeout):
    """Expect the DUT to log and publish a T_RAW event for pin, and that
    the next analog.event the broker gets is that one. Returns the
    published value."""
    m = serial.expect(_event_re(pin), timeout=timeout)
    serial.expect(r"Published!$", timeout=5)

    # No match: the next event of any pin or value, so a stray one fails
    event = protomq.expect_d2b_message(uid, "analog.event", timeout=5)
    assert event.get("pin") == pin, f"expected an event for {pin}: {event}"
    sensor_event = event.get("value", {})
    assert sensor_event.get("type") == "T_RAW", (
        f"expected a T_RAW sensor event: {event}")
    value = sensor_event.get("floatValue")
    assert value == int(m.group(1)), (
        f"published {value}, but the DUT logged {m.group(1)}: {event}")
    return value


def _assert_raw_near(pin, value, volts, ref_voltage, what=""):
    """value, a T_RAW reading of pin, is within RAW_TOLERANCE of volts."""
    expected = volts / ref_voltage * RAW_MAX
    assert abs(value - expected) <= RAW_TOLERANCE * RAW_MAX, (
        f"{pin} read {value:.0f}{what}, expected {expected:.0f} "
        f"+/- {RAW_TOLERANCE * RAW_MAX:.0f} for the DAC's {volts:.3f}V "
        f"of a {ref_voltage}V reference")


def _expect_published(protomq, uid, pin, value, timeout):
    """Expect the broker to get a T_RAW analog.event of value for pin,
    skipping any others it got first. float_value is in a oneof, so the
    JSON has it even when it's 0."""
    return protomq.expect_d2b_message(
        uid, "analog.event", timeout=timeout,
        match={"pin": pin, "value": {"type": "T_RAW",
                                     "floatValue": float(value)}})


def _turn_to_detent(serial, protomq, uid, dac_probe, pin, volts, ref_voltage,
                    previous=None):
    """Turn the simulated pot to volts and rest there for DETENT_DWELL.
    The median of the values the DUT logs from DETENT_SETTLE after the DAC
    write must be near volts, and most of them near that median; the last
    must reach the broker. Returns the median, and the latency from the
    write to the first value nearer that median than previous, the last
    detent's median, as logged on serial and as received by ProtoMQ, in
    seconds; without previous, the first value is timed.

    SM_EVENT publishes every change, so the ADC's noise makes a stream of
    values at a held detent; a steady read publishes once, maybe before
    DETENT_SETTLE, so with none after it the last value since the turn
    counts."""
    regex = _event_re(pin)
    expected = volts / ref_voltage * RAW_MAX
    since = serial.mark()
    written = time.time()  # SerialLine.time's and ProtoMQ's clock
    volts = dac_probe.set_voltage(pin, volts)
    time.sleep(DETENT_DWELL)

    events = [(l.time, int(regex.search(l.text).group(1)))
              for l in _serial_events(serial, pin, since)]
    assert events, f"{pin} published nothing in {DETENT_DWELL}s at {volts:.3f}V"
    values = [v for t, v in events if t >= written + DETENT_SETTLE]
    if not values:
        values = [events[-1][1]]
    median = statistics.median(values)
    shown = f"{len(values)} values, {min(values)}-{max(values)}"

    # Latency: the first value after the write that's nearer this detent
    # than the last one. The bands' tolerances overlap, and the ADC reads
    # every level a little low, so nearness to the measured medians is
    # what tells the detents apart
    first = next(((i, t, v) for i, (t, v) in enumerate(events)
                  if t >= written and (previous is None or
                                       abs(v - median) < abs(v - previous))),
                 None)
    assert first, (f"{pin} published nothing nearer {median:.0f} than "
                   f"{previous:.0f} after the DAC went to {volts:.3f}V: "
                   f"{[v for _, v in events]}")
    serial_latency = first[1] - written
    _expect_published(protomq, uid, pin, first[2], timeout=2)
    broker_latency = protomq.last_d2b_time(uid) - written

    log.info("%s at %.3fV (expected %.0f): median %.0f of %s; reached in "
             "%.3fs serial, %.3fs broker", pin, volts, expected, median, shown,
             serial_latency, broker_latency)
    _assert_raw_near(pin, median, volts, ref_voltage,
                     f" (median of {shown})")
    noise = RAW_NOISE * RAW_MAX
    outliers = [v for v in values if abs(v - median) > noise]
    assert len(outliers) <= (1 - RAW_NOISE_INLIERS) * len(values), (
        f"{pin} at {volts:.3f}V read {len(outliers)} of {shown} over "
        f"{noise:.0f} from their median {median:.0f}, more than "
        f"{1 - RAW_NOISE_INLIERS:.0%}: {outliers}")
    # The last value reached the broker too, unless it's the one timed
    if first[0] != len(events) - 1:
        _expect_published(protomq, uid, pin, events[-1][1], timeout=2)
    return median, serial_latency, broker_latency


def _at_end(value):
    """Whether a T_RAW value is within RAW_TOLERANCE of either end of the
    scale, where the ADC clips, so neighbouring detents may read the
    same."""
    tolerance = RAW_TOLERANCE * RAW_MAX
    return value <= tolerance or value >= RAW_MAX - tolerance


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
        value = _expect_raw_event(serial, protomq, uid, analog_pin,
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
def test_analog_input_event_raw_pot_sweep(board, board_definition,
                                          secrets_name, flashed_board, serial,
                                          power, device_uid, protomq,
                                          dac_probe, analog_pin, checkin):
    # --- Arrange ------------------------------------------------------
    # The simulated pot is across the MCU's reference voltage, starting at
    # its fully CCW detent
    uid = checkin()
    ref_voltage = _ref_voltage(board_definition)
    top = min(ref_voltage, dac_probe.max_voltage)
    detents = [top * (i + 0.5) / POT_DETENTS for i in range(POT_DETENTS)]
    dac_probe.set_voltage(analog_pin, detents[0])
    # No period or ref_voltage: SM_EVENT doesn't use a period, and native
    # pins use the checkin's reference voltage
    _add(serial, protomq, uid, analog_pin, "T_RAW", "SM_EVENT")

    # --- Act / Assert -------------------------------------------------
    # One detent at a time, CW to the top then CCW back to the bottom. Each
    # detent's median must be near its voltage, higher than the last on the
    # way up and lower on the way down, unless both clip at an end
    sweep = detents[1:] + detents[-2::-1]
    readings = []
    latencies = []
    previous = None
    for volts in sweep:
        value, serial_latency, broker_latency = _turn_to_detent(
            serial, protomq, uid, dac_probe, analog_pin, volts, ref_voltage,
            previous[1] if previous else None)
        readings.append((volts, value))
        latencies.append((serial_latency, broker_latency))
        if previous is not None:
            prev_volts, prev_value = previous
            rising = volts > prev_volts
            moved = value > prev_value if rising else value < prev_value
            assert moved or (_at_end(value) and _at_end(prev_value)), (
                f"{analog_pin} read {prev_value:.0f} at {prev_volts:.3f}V "
                f"then {value:.0f} at {volts:.3f}V, should have "
                f"{'risen' if rising else 'fallen'}; sweep so far: "
                f"{[(f'{v:.3f}V', int(r)) for v, r in readings]}")
        previous = (volts, value)

    # The settle time the values above relied on covered every detent's
    # latency, on serial and at the broker
    worst = max(max(pair) for pair in latencies)
    log.info("latency to the first value near a detent: serial max %.3fs, "
             "broker max %.3fs, over %d detents",
             max(l[0] for l in latencies), max(l[1] for l in latencies),
             len(latencies))
    assert worst <= DETENT_SETTLE, (
        f"{analog_pin} took up to {worst:.3f}s to publish a value near a "
        f"new detent, over DETENT_SETTLE {DETENT_SETTLE}s: {latencies}")
