"""
HIL tests for Adafruit Wippersnapper firmware's UART component, with a
PMS5003 particulate sensor.

How Does This Work?
A PLANTOWER PMS5003 is wired to the DUT's UART: its TX to the DUT's RX pin
and its RX to the DUT's TX pin. The board entry's uart_sensor probe names
them (the uart_pins fixture). The broker adds the sensor with every
subcomponent in its Wippersnapper_Components definition
(components/uart/pms5003/definition.json), checks the events the DUT
publishes, then removes it and checks the DUT publishes no more. Other
tests modify its period, and remove it without adding it first.

Exercises:
- `src/components/uart/controller.cpp::Router()`
- `src/components/uart/controller.cpp::Router()` (uart.Modify)
- `src/components/uart/controller.cpp::Handle_UartAdd()` (pm25aqi)
- `src/components/uart/controller.cpp::Handle_UartRemove()`
- `src/components/uart/controller.cpp::Handle_UartRemove()` (port not found)
- `src/components/uart/controller.cpp::update()`
- `src/components/uart/hardware.cpp::ConfigureSerial()`
- `src/components/uart/model.cpp::ConfigureUartInputEventMsg()`
- `src/components/uart/model.cpp::AddUartInputEvent()`
- `src/components/uart/drivers/drvUartPm25.h`
"""

import logging

import pytest

import i2c_helpers  # on sys.path from uart/conftest.py


log = logging.getLogger("hil.uart")

# The component's name in Wippersnapper_Components, and the Add's name
NAME = "pms5003"
# Read every PERIOD seconds
PERIOD = 10
# The Add's serial settings the definition doesn't give
FORMAT = "PF_8N1"
SERIAL_TIMEOUT_MS = 1000
# How long after checkin the DUT may take to handle a message, as the
# checkin fixture allows for the checkin itself.
CHECKIN_TIMEOUT = 30
# drvUartPm25::begin() waits 3 s for the sensor to boot before the Add
# finishes
ADD_TIMEOUT = CHECKIN_TIMEOUT + 5
# update() reads a new driver on its next pass after the Add, but allow a
# full period in case it waits one, plus the read and the publish
FIRST_EVENT_TIMEOUT = PERIOD + 10
# How long to wait for the first event holding readings, skipping events
# whose reads all failed (no events), and how often to poll the broker
DATA_TIMEOUT = 180
DATA_POLL_INTERVAL = 0.5
# How long past the period to watch for events after a Remove
REMOVED_BUFFER = 5
# The Modify's shorter period, in seconds. The sensor stays powered
# through the Modify, so its readings have already settled.
MODIFIED_PERIOD = 10
# How many intervals between events to time after the Modify
MODIFIED_INTERVALS = 3
# How far an interval between events may be from the period, in seconds.
# update() restarts the period after the read and the publish, so an
# interval is the period plus the read (up to a PMS5003 frame, ~1 s), plus
# the loop's and the network's latency.
INTERVAL_EARLY = 0.5
INTERVAL_LATE = 2.0


def _add_msg(component, pin_rx, pin_tx, period=PERIOD):
    """A uart.Add for the PMS5003 on pin_rx/pin_tx, reading every
    subcomponent in its definition every period seconds."""
    definition = component.definition
    return {
        "name": NAME,
        "descriptor": {"pinRx": pin_rx, "pinTx": pin_tx},
        "cfgSerial": {"baudRate": definition["baudRate"], "format": FORMAT,
                      "timeout": SERIAL_TIMEOUT_MS,
                      "swSerialInvert": definition["inverted"]},
        "cfgDevice": {"pm25aqi": {"isPm1006": False, "period": period,
                                  "types": _types(component)}},
    }


def _types(component):
    """The Add's types map for every subcomponent in the definition,
    {str(index): ws.sensor.Type name}."""
    try:
        return i2c_helpers.subcomponent_types(component)
    except i2c_helpers.ComponentDefinitionError as e:
        pytest.fail(str(e), pytrace=False)


def _add(serial, protomq, uid, add):
    """Send add and expect Handle_UartAdd() to add the PMS5003."""
    protomq.publish(uid, {"uart": {"add": add}})
    protomq.expect_b2d_message(uid, "uart.add", match=add, timeout=5)
    _expect_added(serial)


def _expect_added(serial):
    """Expect Handle_UartAdd() to add the PMS5003."""
    serial.expect(r"^\[uart\] Added PM2\.5 AQI device!$",
                  timeout=CHECKIN_TIMEOUT)
    serial.expect(rf"^\[uart\] Added UART device: {NAME}$",
                  timeout=ADD_TIMEOUT)


def _removed_re(descriptor):
    """Handle_UartRemove()'s log line for descriptor: it names the port by
    the RX pin's number."""
    return rf"^\[uart\] Removed UART device on port {int(descriptor['pinRx'][1:])}$"


def _modify(serial, protomq, uid, add):
    """Send a uart.Modify replacing the device on add's descriptor with
    add, and expect the DUT to remove then re-add it."""
    modify = {"remove": {"descriptor": add["descriptor"]}, "add": add}
    protomq.publish(uid, {"uart": {"modify": modify}})
    protomq.expect_b2d_message(uid, "uart.modify", match=modify, timeout=5)
    serial.expect(_removed_re(add["descriptor"]), timeout=CHECKIN_TIMEOUT)
    _expect_added(serial)


def _remove(serial, protomq, uid, descriptor):
    """Send a uart.Remove for descriptor and expect Handle_UartRemove() to
    remove the device on its RX pin's port."""
    remove = {"descriptor": descriptor}
    protomq.publish(uid, {"uart": {"remove": remove}})
    protomq.expect_b2d_message(uid, "uart.remove", match=remove, timeout=5)
    serial.expect(_removed_re(descriptor), timeout=CHECKIN_TIMEOUT)


def _expect_event(protomq, uid, add, timeout, interval=0.05,
                  with_data=False):
    """Expect the broker to get a uart.event from the device add added:
    its descriptor the Add's, holding a sensor event per type in the Add,
    in the Add's order, each a non-negative float. If with_data, skip
    events holding no sensor events (every read failed) until one does.
    Polls the broker every interval seconds. Returns when the broker
    received it, in seconds since the epoch, and {type: value}."""
    descriptor = add["descriptor"]
    match = {"descriptor": {"pinRx": descriptor["pinRx"]}}
    if with_data:
        match["events"] = "*"
    event = protomq.expect_d2b_message(uid, "uart.event", match=match,
                                       timeout=timeout, interval=interval)
    assert event.get("descriptor") == descriptor, (
        f"expected the event's descriptor to be the Add's {descriptor}, "
        f"got {event.get('descriptor')}: {event}")
    types = add["cfgDevice"]["pm25aqi"]["types"]
    expected = [types[k] for k in sorted(types, key=int)]
    events = event.get("events", [])
    got = [e.get("type") for e in events]
    assert got == expected, (
        f"expected sensor events of {expected}, got {got}: {event}")
    values = {}
    for sensor_event in events:
        kind = sensor_event["type"]
        value = sensor_event.get("floatValue")
        assert value is not None, f"expected a floatValue for {kind}: {event}"
        # A particle concentration can't be negative; 0 is clean air
        assert value >= 0, f"expected {kind} >= 0, got {value}: {event}"
        values[kind] = value
    received = protomq.last_d2b_time(uid)
    log.info("%s: %s at %s", descriptor, values, received)
    return received, values


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_uart_pms5003_add_events_remove(board, secrets_name, flashed_board,
                                        serial, protomq, checkin, uart_pins,
                                        component_definition):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    pin_rx, pin_tx = uart_pins
    add = _add_msg(component_definition("uart", NAME), pin_rx, pin_tx)

    # --- Act ----------------------------------------------------------
    _add(serial, protomq, uid, add)

    # --- Assert -------------------------------------------------------
    # One event holding every subcomponent, from the Add's descriptor.
    # Wait for one with readings, skipping any whose reads all failed.
    _expect_event(protomq, uid, add, DATA_TIMEOUT,
                  interval=DATA_POLL_INTERVAL, with_data=True)

    # --- Act ----------------------------------------------------------
    # Sent just after an event, so none is in flight
    _remove(serial, protomq, uid, add["descriptor"])

    # --- Assert -------------------------------------------------------
    protomq.assert_no_d2b_message(uid, "uart.event",
                                  within=PERIOD + REMOVED_BUFFER)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_uart_pms5003_modify_period(board, secrets_name, flashed_board,
                                    serial, protomq, checkin, uart_pins,
                                    component_definition):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    pin_rx, pin_tx = uart_pins
    component = component_definition("uart", NAME)
    add = _add_msg(component, pin_rx, pin_tx)
    _add(serial, protomq, uid, add)
    _expect_event(protomq, uid, add, FIRST_EVENT_TIMEOUT)
    modified = _add_msg(component, pin_rx, pin_tx, period=MODIFIED_PERIOD)

    # --- Act ----------------------------------------------------------
    # Sent just after an event, so none at the old period is in flight
    _modify(serial, protomq, uid, modified)

    # --- Assert -------------------------------------------------------
    # The re-added sensor reads on update()'s next pass, then every
    # MODIFIED_PERIOD, each event still from the Add's descriptor with
    # every subcomponent
    times = [_expect_event(protomq, uid, modified,
                           MODIFIED_PERIOD + 10)[0]]
    for _ in range(MODIFIED_INTERVALS):
        times.append(_expect_event(protomq, uid, modified,
                                   MODIFIED_PERIOD + INTERVAL_LATE + 5)[0])
    if None in times:
        pytest.fail("ProtoMQ doesn't timestamp deliveries, can't time the "
                    "events", pytrace=False)
    intervals = [b - a for a, b in zip(times, times[1:])]
    log.info("intervals after modify to %ss: %s", MODIFIED_PERIOD,
             ", ".join(f"{i:.2f}s" for i in intervals))
    for interval in intervals:
        assert (MODIFIED_PERIOD - INTERVAL_EARLY <= interval
                <= MODIFIED_PERIOD + INTERVAL_LATE), (
            f"expected events every {MODIFIED_PERIOD}s "
            f"(-{INTERVAL_EARLY}/+{INTERVAL_LATE}s), got intervals "
            f"{[round(i, 2) for i in intervals]}")

    # Remove it, just after an event so none is in flight, and expect no
    # more events
    _remove(serial, protomq, uid, modified["descriptor"])
    protomq.assert_no_d2b_message(uid, "uart.event",
                                  within=MODIFIED_PERIOD + REMOVED_BUFFER)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_uart_remove_without_add(board, secrets_name, flashed_board, serial,
                                 protomq, checkin, uart_pins):
    # --- Arrange ------------------------------------------------------
    # Nothing added on the sensor's pins, so there's no port to remove
    uid = checkin()
    pin_rx, pin_tx = uart_pins
    descriptor = {"pinRx": pin_rx, "pinTx": pin_tx}
    remove = {"descriptor": descriptor}

    # --- Act ----------------------------------------------------------
    protomq.publish(uid, {"uart": {"remove": remove}})

    # --- Assert -------------------------------------------------------
    # Handle_UartRemove() finds no port on the RX pin and publishes an
    # error for the Remove's descriptor (it doesn't log it)
    protomq.expect_b2d_message(uid, "uart.remove", match=remove, timeout=5)
    protomq.expect_d2b_message(
        uid, "error.component",
        match={"uart": descriptor, "message": "Port not found for removal"},
        timeout=CHECKIN_TIMEOUT)
    serial.assert_none(_removed_re(descriptor), duration=1)
