"""
HIL tests for Adafruit Wippersnapper firmware's I2C sensor drivers, with
a BME680.

How Does This Work?
A BME680 at 0x77 is wired to one of the DUT's I2C ports; the board
entry's i2c_bus probe names the port and lists the device (the
i2c_devices and i2c_address_space fixtures). The broker sends an i2c.Add
for it, and the tests check that the DUT adds the driver without a
component error, then publishes an i2c.Event for the device, holding a
sensor event per type in the Add, at the Add's period. Further tests
remove the device, and modify its period and its types, and check the
events stop, or follow the new settings.

Exercises:
- `src/components/i2c/controller.cpp::Router()` (i2c.Add)
- `src/components/i2c/controller.cpp::Handle_Add()`
- `src/components/i2c/controller.cpp::Router()` (i2c.Remove, i2c.Modify)
- `src/components/i2c/controller.cpp::Handle_Remove()`
- `src/components/i2c/controller.cpp::Handle_Modify()`
- `src/components/i2c/controller.cpp::RemoveDriver()`
- `src/components/i2c/controller.cpp::update()`
- `src/components/i2c/drivers/drvBase.h::EnableSensorReads()`
- `src/components/i2c/drivers/drvBase.h::SetSensorPeriod()`
- `src/components/i2c/drivers/drvBme680.h::begin()`
- `src/components/i2c/drivers/drvBme680.h::getEvent*()`
- `src/components/i2c/model.cpp::AddI2cDeviceSensorEvent()`
- `src/components/i2c/model.cpp::EncodeI2cDeviceEvent()`
"""

import logging

import pytest


log = logging.getLogger("hil.i2c")

# The BME680's address and its driver's name in CreateI2cSensorDrv()
ADDRESS = 0x77
NAME = "bme680"
# ws.sensor.Type values for each of the BME680's sensors (its component's
# ambient-temp, ambient-temp-fahrenheit, humidity, pressure, altitude and
# gas-resistance), in the order the Add lists them
AMBIENT_TEMP = "T_AMBIENT_TEMPERATURE"
AMBIENT_TEMP_F = "T_AMBIENT_TEMPERATURE_FAHRENHEIT"
HUMIDITY = "T_RELATIVE_HUMIDITY"
PRESSURE = "T_PRESSURE"
ALTITUDE = "T_ALTITUDE"
GAS_RESISTANCE = "T_GAS_RESISTANCE"
ALL_TYPES = (AMBIENT_TEMP, AMBIENT_TEMP_F, HUMIDITY, PRESSURE, ALTITUDE,
             GAS_RESISTANCE)
# Read every PERIOD seconds
PERIOD = 10
# The Modify's shorter period, in seconds
MODIFIED_PERIOD = 3
# How many intervals between events to time after the Modify
MODIFIED_INTERVALS = 3
# How long past the period to watch for events after a Remove
REMOVED_BUFFER = 5
# How long after checkin the DUT may take to handle a message, as the
# checkin fixture allows for the checkin itself.
CHECKIN_TIMEOUT = 30
# A new driver's previous read is 24 hours ago, so update() reads it on its
# next pass after the Add; allow for the BME680's conversion (gas heater
# included) and the publish.
FIRST_EVENT_TIMEOUT = 10
# How far an interval between events may be from the period, in seconds.
# update() restarts the period after the read and the publish, so an
# interval is the period plus the conversion, plus the loop's and the
# network's latency.
INTERVAL_EARLY = 0.5
INTERVAL_LATE = 2.0
# Plausible indoor readings, per type. Altitude is from the default
# sea-level pressure, so it's only bounded loosely; gas resistance depends
# on the air and the heater, so it's only required to be a measurement.
VALUE_RANGE = {
    AMBIENT_TEMP: (5.0, 50.0),
    AMBIENT_TEMP_F: (41.0, 122.0),
    HUMIDITY: (1.0, 99.0),
    PRESSURE: (800.0, 1100.0),
    ALTITUDE: (-1000.0, 3000.0),
    GAS_RESISTANCE: (1.0, 1e8),
}
# How far a Fahrenheit value may be from its Celsius value converted: the
# event's floats are single precision
F_TOLERANCE = 0.05


def _descriptor(address_space):
    """The BME680's i2c.Descriptor on address_space."""
    return {"addressSpace": address_space, "address": ADDRESS}


def _add_msg(address_space, types, period=PERIOD):
    """An i2c.Add for the BME680 on address_space, reading types every
    period. The types map's keys are 1, 2, ... in the order of types."""
    return {
        "descriptor": _descriptor(address_space),
        "name": NAME,
        "period": period,
        "types": {str(key): kind for key, kind in enumerate(types, 1)},
    }


def _expect_driver_initialized(serial):
    """Expect Handle_Add() to create and initialize the BME680's driver."""
    serial.expect(r"^\[i2c\] Creating sensor driver\.\.\.$",
                  timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^\[i2c\] Driver initialized: ?$", timeout=5)
    serial.expect(rf"^{NAME}$", timeout=5)


def _add(serial, protomq, uid, address_space, types, period=PERIOD):
    """Send an i2c.Add for the BME680 reading types every period, and
    expect the DUT to decode it and initialize the driver."""
    add = _add_msg(address_space, types, period)
    log.info("adding %s at 0x%02x with %s every %ss", NAME, ADDRESS,
             list(types), period)
    protomq.publish(uid, {"i2c": {"add": add}})
    protomq.expect_b2d_message(uid, "i2c.add", match=add, timeout=5)
    _expect_driver_initialized(serial)


def _remove(protomq, uid, address_space):
    """Send an i2c.Remove for the BME680. Handle_Remove() logs nothing when
    it succeeds; the caller checks for a component error."""
    remove = {"descriptor": _descriptor(address_space)}
    log.info("removing %s at 0x%02x", NAME, ADDRESS)
    protomq.publish(uid, {"i2c": {"remove": remove}})
    protomq.expect_b2d_message(uid, "i2c.remove", match=remove, timeout=5)


def _modify(serial, protomq, uid, address_space, types, period=PERIOD):
    """Send an i2c.Modify that replaces the BME680 with one reading types
    every period, and expect the DUT to remove it, then initialize the new
    driver."""
    modify = {"remove": {"descriptor": _descriptor(address_space)},
              "add": _add_msg(address_space, types, period)}
    log.info("modifying %s at 0x%02x to %s every %ss", NAME, ADDRESS,
             list(types), period)
    protomq.publish(uid, {"i2c": {"modify": modify}})
    protomq.expect_b2d_message(uid, "i2c.modify", match=modify, timeout=5)
    _expect_driver_initialized(serial)
    # Handle_Modify() logs this before the Add if Handle_Remove() failed
    serial.assert_none(r"^\[i2c\] WARNING: Remove failed in modify")


def _expect_event(protomq, uid, address_space, timeout, types):
    """Expect the broker to get an i2c.Event for the BME680 on
    address_space, holding a sensor event per type in types, keyed as the
    Add keyed them, in that order, each a plausible reading. Returns when
    the broker received it, in seconds since the epoch, and {type: value}.

    ProtoMQ decodes the events map into a JSON object, whose integer keys
    come out in ascending order whatever their order on the wire, so the
    order checked is that of the keys."""
    event = protomq.expect_d2b_message(
        uid, "i2c.event", match={"descriptor": {"address": ADDRESS}},
        timeout=timeout)
    descriptor = event.get("descriptor", {})
    # Exact, not ProtoMQ's partial match: the DUT sends the bus's pins, and
    # no mux fields for a device on the bus
    assert descriptor == {"addressSpace": address_space, "address": ADDRESS}, (
        f"expected descriptor {address_space} at 0x{ADDRESS:02x}, got "
        f"{descriptor}: {event}")
    events = event.get("events", {})
    expected = {str(key): kind for key, kind in enumerate(types, 1)}
    got = {key: e.get("type") for key, e in
           sorted(events.items(), key=lambda item: int(item[0]))}
    assert list(got.items()) == list(expected.items()), (
        f"expected sensor events {expected}, got {got}: {event}")
    values = {}
    for sensor_event in events.values():
        kind = sensor_event["type"]
        value = sensor_event.get("floatValue")
        assert value is not None, f"expected a floatValue for {kind}: {event}"
        low, high = VALUE_RANGE[kind]
        assert low <= value <= high, (
            f"expected {kind} {low}-{high}, got {value}: {event}")
        values[kind] = value
    received = protomq.last_d2b_time(uid)
    log.info("0x%02x: %s at %s", ADDRESS, values, received)
    return received, values


def _assert_period(times, period=PERIOD):
    """Expect the events received at times to be period apart."""
    if None in times:
        pytest.fail("ProtoMQ doesn't timestamp deliveries, can't time the "
                    "events", pytrace=False)
    intervals = [b - a for a, b in zip(times, times[1:])]
    log.info("intervals at period %ss: %s", period,
             ", ".join(f"{i:.2f}s" for i in intervals))
    for interval in intervals:
        assert (period - INTERVAL_EARLY <= interval
                <= period + INTERVAL_LATE), (
            f"expected events every {period}s (-{INTERVAL_EARLY}/"
            f"+{INTERVAL_LATE}s), got intervals "
            f"{[round(i, 2) for i in intervals]}")


def _assert_no_component_error(protomq, uid, mark):
    """Expect no error.component from the DUT since mark."""
    errors = protomq.d2b_since(uid, "error.component", mark)
    assert not errors, f"expected no error.component, got {errors}"


@pytest.fixture
def bme680(i2c_devices):
    """Fail unless the i2c_bus probe lists a device at ADDRESS."""
    if ADDRESS not in i2c_devices.addresses:
        pytest.fail(f"i2c_bus probe doesn't list 0x{ADDRESS:02x}, the "
                    f"{NAME} these tests add", pytrace=False)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
@pytest.mark.parametrize("types", [(AMBIENT_TEMP,), (AMBIENT_TEMP, HUMIDITY),
                                   ALL_TYPES],
                         ids=["ambient_temp", "temp_humidity", "all_types"])
def test_i2c_bme680_add_event(board, secrets_name, flashed_board, serial,
                              protomq, checkin, i2c_address_space, bme680,
                              types):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)

    # --- Act ----------------------------------------------------------
    _add(serial, protomq, uid, i2c_address_space, types)

    # --- Assert -------------------------------------------------------
    # The driver reads at once, then every PERIOD
    first, values = _expect_event(protomq, uid, i2c_address_space,
                                  FIRST_EVENT_TIMEOUT, types)
    second, _ = _expect_event(protomq, uid, i2c_address_space,
                              PERIOD + INTERVAL_LATE + 5, types)
    _assert_period([first, second])
    if AMBIENT_TEMP_F in values:
        # Both units are of the same reading
        expected_f = values[AMBIENT_TEMP] * 9 / 5 + 32
        assert abs(values[AMBIENT_TEMP_F] - expected_f) <= F_TOLERANCE, (
            f"{values[AMBIENT_TEMP_F]} F isn't {values[AMBIENT_TEMP]} C "
            f"({expected_f:.3f} F)")
    # No component error, from the Add or either read
    _assert_no_component_error(protomq, uid, mark)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_bme680_remove_stops_events(board, secrets_name, flashed_board,
                                        serial, protomq, checkin,
                                        i2c_address_space, bme680):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    _add(serial, protomq, uid, i2c_address_space, (AMBIENT_TEMP,))
    _expect_event(protomq, uid, i2c_address_space, FIRST_EVENT_TIMEOUT,
                  (AMBIENT_TEMP,))

    # --- Act ----------------------------------------------------------
    # Sent just after an event, so none is in flight
    _remove(protomq, uid, i2c_address_space)

    # --- Assert -------------------------------------------------------
    # No event for a period and then some, when the next would be due
    protomq.assert_no_d2b_message(uid, "i2c.event",
                                  within=PERIOD + REMOVED_BUFFER)
    # ...and no component error: Handle_Remove() found the driver
    _assert_no_component_error(protomq, uid, mark)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_bme680_modify_period(board, secrets_name, flashed_board, serial,
                                  protomq, checkin, i2c_address_space,
                                  bme680):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    types = (AMBIENT_TEMP,)
    _add(serial, protomq, uid, i2c_address_space, types)
    _expect_event(protomq, uid, i2c_address_space, FIRST_EVENT_TIMEOUT,
                  types)

    # --- Act ----------------------------------------------------------
    # Sent just after an event, so none at the old period is in flight
    _modify(serial, protomq, uid, i2c_address_space, types,
            period=MODIFIED_PERIOD)

    # --- Assert -------------------------------------------------------
    # The new driver reads at once, then every MODIFIED_PERIOD
    times = [_expect_event(protomq, uid, i2c_address_space,
                           FIRST_EVENT_TIMEOUT, types)[0]]
    for _ in range(MODIFIED_INTERVALS):
        times.append(_expect_event(protomq, uid, i2c_address_space,
                                   MODIFIED_PERIOD + INTERVAL_LATE + 5,
                                   types)[0])
    _assert_period(times, MODIFIED_PERIOD)
    _assert_no_component_error(protomq, uid, mark)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_bme680_modify_add_type(board, secrets_name, flashed_board,
                                    serial, protomq, checkin,
                                    i2c_address_space, bme680):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    _add(serial, protomq, uid, i2c_address_space, (AMBIENT_TEMP,))
    _expect_event(protomq, uid, i2c_address_space, FIRST_EVENT_TIMEOUT,
                  (AMBIENT_TEMP,))

    # --- Act ----------------------------------------------------------
    # Sent just after an event, so none with only the old type is in flight
    types = (AMBIENT_TEMP, HUMIDITY)
    _modify(serial, protomq, uid, i2c_address_space, types)

    # --- Assert -------------------------------------------------------
    # The new driver reads at once, both the old type and the new
    _expect_event(protomq, uid, i2c_address_space, FIRST_EVENT_TIMEOUT,
                  types)
    _assert_no_component_error(protomq, uid, mark)
