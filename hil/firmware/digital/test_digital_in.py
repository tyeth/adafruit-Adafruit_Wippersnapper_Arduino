"""
HIL tests for Adafruit Wippersnapper firmware's digitalIO component reading
input pins: the digital.Event messages the DUT publishes when the probe
drives a level on a pin the broker added as an input.

Exercises:
- `src/components/digitalIO/controller.cpp::Handle_DigitalIO_Add()`
- `src/components/digitalIO/controller.cpp::update()`
- `src/components/digitalIO/controller.cpp::EncodePublishPinEvent()`
- `src/components/digitalIO/hardware.cpp::CheckEvent()`

The probe drives the DUT's input pin through the expander pin wired to it.
It leaves that pin an input (not driving) through the power cut and the
checkin, and only drives it once the DUT is checked in.
"""

import re
import time

import pytest

from dio_helpers import CHECKIN_TIMEOUT, checkin

# digital.Direction values, as Handle_DigitalIO_Add() prints them
DIRECTIONS = {"D_INPUT": 1, "D_INPUT_PULL_UP": 2}
# How long the DUT may take to publish a pin's first value after adding it
FIRST_EVENT_TIMEOUT = 5
# How long the pin is left at its starting level after the first event, for
# the DUT to settle; no events may come in meanwhile
SETTLE_TIME = 0.5
# How long the DUT may take to publish an event after the probe drives an
# edge, and the window that event must be the only one in
EVENT_TIMEOUT = 1


def _event_re(pin, value=None):
    """Matches EncodePublishPinEvent()'s log line for pin, with value if
    given."""
    value = r"[01]" if value is None else int(value)
    return re.compile(rf"^\[dio\] Publish Event: {pin} \| value: {value}$")


def _serial_events(serial, pin, since):
    """The lines from since on that log an event for pin."""
    regex = _event_re(pin)
    return [l.text for l in serial.lines(since) if regex.search(l.text)]


def _add_input(serial, protomq, uid, pin, direction="D_INPUT",
               sample_mode="SM_EVENT"):
    """Send a digital.Add for pin as an input with sample_mode and expect
    the DUT to add it."""
    add = {"pin": pin, "gpioDirection": direction, "sampleMode": sample_mode}
    protomq.publish(uid, {"digital": {"add": add}})
    protomq.expect_b2d_message(uid, "digital.add", match=add, timeout=5)

    serial.expect(r"^\[dio\] Added new pin:$", timeout=CHECKIN_TIMEOUT)
    serial.expect(rf"^Pin Name: {pin.lstrip('D')}$", timeout=5)
    serial.expect(rf"^Direction: {DIRECTIONS[direction]}$", timeout=5)


def _expect_event(serial, protomq, uid, pin, value, timeout):
    """Expect the DUT to log and publish an event of value for pin, and
    that the next digital.Event the broker gets is that one."""
    serial.expect(_event_re(pin, value), timeout=timeout)
    serial.expect(r"^\[dio\] Published!$", timeout=timeout)

    # No match: the next event of any pin or value, so a stray one fails
    event = protomq.expect_d2b_message(uid, "digital.event", timeout=timeout)
    assert event.get("pin") == pin, f"expected an event for {pin}: {event}"
    sensor_event = event.get("value", {})
    assert sensor_event.get("type") == "T_BOOLEAN", (
        f"expected a T_BOOLEAN sensor event: {event}")
    # proto3 JSON leaves out a false bool
    assert sensor_event.get("boolValue", False) == value, (
        f"expected value {int(value)}: {event}")


def _edge_test(board, board_definition, serial, power, device_uid, protomq,
               probe, pin, start, end):
    """Check the DUT in, drive pin to start, add it as an SM_EVENT input,
    then drive it to end and expect exactly one event of end."""
    try:
        # --- Arrange --------------------------------------------------
        # The probe doesn't drive pin through the power cut and checkin
        probe.release(pin)
        uid = checkin(board, board_definition, serial, power, device_uid,
                      protomq)

        # Hold pin at start, then add it. CheckEvent() publishes the
        # first value it reads after the add, start
        probe.drive(pin, start)
        added = serial.mark()
        _add_input(serial, protomq, uid, pin, "D_INPUT", "SM_EVENT")
        _expect_event(serial, protomq, uid, pin, start,
                      timeout=FIRST_EVENT_TIMEOUT)

        # Nothing else is published while pin stays at start
        time.sleep(SETTLE_TIME)
        events = _serial_events(serial, pin, added)
        assert len(events) == 1, (
            f"expected only the first event for {pin} before driving it: "
            f"{events}")
        protomq.assert_no_d2b_message(uid, "digital.event", within=0.1)

        # --- Act ------------------------------------------------------
        driven = serial.mark()
        t0 = time.monotonic()
        probe.drive(pin, end)

        # --- Assert ---------------------------------------------------
        # One event of end within EVENT_TIMEOUT, and no others
        _expect_event(serial, protomq, uid, pin, end, timeout=EVENT_TIMEOUT)
        time.sleep(max(EVENT_TIMEOUT - (time.monotonic() - t0), 0))
        events = _serial_events(serial, pin, driven)
        assert len(events) == 1, (
            f"expected exactly one event for {pin} after driving it "
            f"{int(start)} -> {int(end)}: {events}")
        protomq.assert_no_d2b_message(uid, "digital.event", within=0.1)
    finally:
        probe.release(pin)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_digital_input_event_rising_edge(board, board_definition, secrets_name,
                                         flashed_board, serial, power,
                                         device_uid, protomq, probe):
    _edge_test(board, board_definition, serial, power, device_uid, protomq,
               probe, "D32", start=False, end=True)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_digital_input_event_falling_edge(board, board_definition,
                                          secrets_name, flashed_board, serial,
                                          power, device_uid, protomq, probe):
    _edge_test(board, board_definition, serial, power, device_uid, protomq,
               probe, "D32", start=True, end=False)
