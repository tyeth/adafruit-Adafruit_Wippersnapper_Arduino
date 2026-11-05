"""
HIL tests for Adafruit Wippersnapper firmware's I2C sensor drivers, with
a BME680.

How Does This Work?
A BME680 at 0x77 is wired to one of the DUT's I2C ports; the board
entry's i2c_bus probe names the port and lists the device (the
i2c_devices and i2c_address_space fixtures). The broker sends an i2c.Add
for it, and the tests check that the DUT adds the driver without a
component error, then publishes an i2c.Event for the device, holding a
sensor event per type in the Add, at the Add's period.

Exercises:
- `src/components/i2c/controller.cpp::Router()` (i2c.Add)
- `src/components/i2c/controller.cpp::Handle_Add()`
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


def _add_msg(address_space, types):
    """An i2c.Add for the BME680 on address_space, reading types every
    PERIOD. The types map's keys are 1, 2, ... in the order of types."""
    return {
        "descriptor": {"addressSpace": address_space, "address": ADDRESS},
        "name": NAME,
        "period": PERIOD,
        "types": {str(key): kind for key, kind in enumerate(types, 1)},
    }


def _add(serial, protomq, uid, address_space, types):
    """Send an i2c.Add for the BME680 reading types, and expect the DUT to
    decode it and initialize the driver."""
    add = _add_msg(address_space, types)
    log.info("adding %s at 0x%02x with %s", NAME, ADDRESS, list(types))
    protomq.publish(uid, {"i2c": {"add": add}})
    protomq.expect_b2d_message(uid, "i2c.add", match=add, timeout=5)
    serial.expect(r"^\[i2c\] Creating sensor driver\.\.\.$",
                  timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^\[i2c\] Driver initialized: ?$", timeout=5)
    serial.expect(rf"^{NAME}$", timeout=5)


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


def _assert_period(first, second):
    """Expect the events received at first and second to be PERIOD apart."""
    if None in (first, second):
        pytest.fail("ProtoMQ doesn't timestamp deliveries, can't time the "
                    "events", pytrace=False)
    interval = second - first
    log.info("interval at period %ss: %.2fs", PERIOD, interval)
    assert PERIOD - INTERVAL_EARLY <= interval <= PERIOD + INTERVAL_LATE, (
        f"expected events every {PERIOD}s (-{INTERVAL_EARLY}/"
        f"+{INTERVAL_LATE}s), got an interval of {interval:.2f}s")


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
    _assert_period(first, second)
    if AMBIENT_TEMP_F in values:
        # Both units are of the same reading
        expected_f = values[AMBIENT_TEMP] * 9 / 5 + 32
        assert abs(values[AMBIENT_TEMP_F] - expected_f) <= F_TOLERANCE, (
            f"{values[AMBIENT_TEMP_F]} F isn't {values[AMBIENT_TEMP]} C "
            f"({expected_f:.3f} F)")
    # No component error, from the Add or either read
    _assert_no_component_error(protomq, uid, mark)
