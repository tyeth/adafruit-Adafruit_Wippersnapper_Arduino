"""
HIL tests for Adafruit Wippersnapper firmware's BME680 driver: its
readings, its settings, and its stability over a long run. The I2C API
itself (Add, Remove, Modify, the types map's keys, rejected Adds) is
test_i2c_api.py's.

How Does This Work?
A BME680 at 0x77 is wired to one of the DUT's I2C ports; the board
entry's i2c_bus probe names the port and lists the device (the
i2c_devices and i2c_address_space fixtures). Its component definition in
Wippersnapper_Components (components/i2c/bme680/definition.json) lists
its subcomponents; a test names the ones to read, and the Add's types map
keys each one's ws.sensor.Type by its 0-based position in that list
(i2c_helpers.py). The broker sends the Add, and the
tests check the readings in the i2c.Events the DUT publishes.

Exercises:
- `src/components/i2c/drivers/drvBme680.h::begin()`
- `src/components/i2c/drivers/drvBme680.h::configureDefaults()`
- `src/components/i2c/drivers/drvBme680.h::ReadSensorData()`
- `src/components/i2c/drivers/drvBme680.h::getEvent*()`
- `src/components/i2c/drivers/drvBme680.h::setSeaLevelPressure()`
- `src/components/i2c/drivers/drvBase.cpp::configure()`
- `src/components/i2c/drivers/drvBase.h::getEventAmbientTempF()`
"""

import logging

import pytest

import i2c_helpers as i2c
from i2c_helpers import FIRST_EVENT_TIMEOUT, INTERVAL_LATE, PERIOD


log = logging.getLogger("hil.i2c")

# The BME680's driver/component name and address
DEVICE = ("bme680", 0x77)
# Subcomponents of the definition's to read (None reads all of them)
PRESSURE_ALTITUDE = ("pressure", "altitude")
ALL_SUBCOMPONENTS = None
# The ws.sensor.Type values the DUT sends back, to check the readings
AMBIENT_TEMP = "T_AMBIENT_TEMPERATURE"
AMBIENT_TEMP_F = "T_AMBIENT_TEMPERATURE_FAHRENHEIT"
HUMIDITY = "T_RELATIVE_HUMIDITY"
PRESSURE = "T_PRESSURE"
ALTITUDE = "T_ALTITUDE"
GAS_RESISTANCE = "T_GAS_RESISTANCE"
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
# The sea_level_pressure setting to send, in hPa: far enough from the
# driver's default (SEALEVELPRESSURE_HPA, 1013.25) that the altitude moves
# by hundreds of metres. getEventAltitude()'s formula, with the event's
# pressure, must give the event's altitude to within ALTITUDE_TOLERANCE m
# (single-precision floats), and must not give the default's.
SEA_LEVEL_PRESSURE = 1050.0
DEFAULT_SEA_LEVEL_PRESSURE = 1013.25
ALTITUDE_TOLERANCE = 1.0
ALTITUDE_MOVED = 100.0
# The stability run: all subcomponents for STABILITY_PERIODS periods
# (about 5 minutes at PERIOD), every reading in range, every interval on
# time, no failed read, and each reading's spread over the run at most
# STABILITY_SPREAD (an indoor bench: the gas heater warms the die a little)
STABILITY_PERIODS = 30
STABILITY_SPREAD = {
    AMBIENT_TEMP: 3.0,
    AMBIENT_TEMP_F: 5.4,
    HUMIDITY: 10.0,
    PRESSURE: 3.0,
}


@pytest.fixture
def bme680(i2c_devices, component_definition):
    """The BME680 as an i2c_helpers.I2cDevice."""
    return i2c.device(i2c_devices, component_definition, *DEVICE)


def _altitude(pressure, sea_level):
    """getEventAltitude()'s altitude, in m, for pressure and sea_level in
    hPa."""
    return 44330.0 * (1.0 - (pressure / sea_level) ** 0.1903)


def _assert_fahrenheit(values):
    """Expect values' Fahrenheit reading to be its Celsius one converted."""
    expected_f = values[AMBIENT_TEMP] * 9 / 5 + 32
    assert abs(values[AMBIENT_TEMP_F] - expected_f) <= F_TOLERANCE, (
        f"{values[AMBIENT_TEMP_F]} F isn't {values[AMBIENT_TEMP]} C "
        f"({expected_f:.3f} F)")


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_bme680_all_types(board, secrets_name, flashed_board, serial,
                              protomq, checkin, i2c_address_space, bme680):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    types = bme680.types(ALL_SUBCOMPONENTS)

    # --- Act ----------------------------------------------------------
    i2c.add(serial, protomq, uid, bme680, i2c_address_space, types)

    # --- Assert -------------------------------------------------------
    # Every reading in range (gas included: the heater reached its target),
    # at once and again a period later
    first, values = i2c.expect_event(protomq, uid, bme680, i2c_address_space,
                                     FIRST_EVENT_TIMEOUT, types, VALUE_RANGE)
    second, _ = i2c.expect_event(protomq, uid, bme680, i2c_address_space,
                                 PERIOD + INTERVAL_LATE + 5, types,
                                 VALUE_RANGE)
    i2c.assert_period([first, second])
    # Both temperature units are of the same reading
    _assert_fahrenheit(values)
    i2c.assert_no_component_error(protomq, uid, mark)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_bme680_add_sea_level_pressure(board, secrets_name,
                                           flashed_board, serial, protomq,
                                           checkin, i2c_address_space,
                                           bme680):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    types = bme680.types(PRESSURE_ALTITUDE)
    settings = {"sea_level_pressure": {"floatValue": SEA_LEVEL_PRESSURE}}

    # --- Act ----------------------------------------------------------
    i2c.add(serial, protomq, uid, bme680, i2c_address_space, types,
            settings=settings)

    # --- Assert -------------------------------------------------------
    # configure() applies it with no "Failed to apply setting" or
    # "Unknown setting key" error...
    _, values = i2c.expect_event(protomq, uid, bme680, i2c_address_space,
                                 FIRST_EVENT_TIMEOUT, types,
                                 {PRESSURE: VALUE_RANGE[PRESSURE]})
    i2c.assert_no_component_error(protomq, uid, mark)
    # ...so the altitude is from the pressure sent, not the default
    pressure, altitude = values[PRESSURE], values[ALTITUDE]
    expected = _altitude(pressure, SEA_LEVEL_PRESSURE)
    default = _altitude(pressure, DEFAULT_SEA_LEVEL_PRESSURE)
    log.info("%.2f hPa: altitude %.2f m, expected %.2f m at %s hPa "
             "(%.2f m at the default)", pressure, altitude, expected,
             SEA_LEVEL_PRESSURE, default)
    assert abs(altitude - expected) <= ALTITUDE_TOLERANCE, (
        f"expected altitude {expected:.2f} m for {pressure} hPa at sea-level "
        f"{SEA_LEVEL_PRESSURE} hPa, got {altitude:.2f} m")
    assert abs(altitude - default) >= ALTITUDE_MOVED, (
        f"altitude {altitude:.2f} m is the default sea-level pressure's "
        f"({default:.2f} m): sea_level_pressure wasn't applied")


@pytest.mark.slow
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_bme680_stability(board, secrets_name, flashed_board, serial,
                              protomq, checkin, i2c_address_space, bme680):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    types = bme680.types(ALL_SUBCOMPONENTS)

    # --- Act ----------------------------------------------------------
    i2c.add(serial, protomq, uid, bme680, i2c_address_space, types)
    first, values = i2c.expect_event(protomq, uid, bme680, i2c_address_space,
                                     FIRST_EVENT_TIMEOUT, types, VALUE_RANGE)
    times, readings = [first], [values]
    # The first read may be retried while the sensor settles; none after
    settled = serial.mark()
    for _ in range(STABILITY_PERIODS):
        received, values = i2c.expect_event(
            protomq, uid, bme680, i2c_address_space,
            PERIOD + INTERVAL_LATE + 5, types, VALUE_RANGE)
        times.append(received)
        readings.append(values)

    # --- Assert -------------------------------------------------------
    # Every event held every type in range (expect_event), on time...
    i2c.assert_period(times)
    # ...each a full read: no read retried or metric dropped once settled
    serial.assert_none(r"^\[i2c\] Driver read failed", since=settled)
    serial.assert_none(r"^\[i2c\] WARNING: Failed to read sensor key=",
                       since=settled)
    # ...Fahrenheit always the Celsius reading's...
    for values in readings:
        _assert_fahrenheit(values)
    # ...and steady over the run
    for kind, limit in STABILITY_SPREAD.items():
        series = [values[kind] for values in readings]
        spread = max(series) - min(series)
        log.info("%s over %d events: %.2f-%.2f (spread %.2f)", kind,
                 len(series), min(series), max(series), spread)
        assert spread <= limit, (
            f"{kind} spread {spread:.2f} over {len(series)} events, more "
            f"than {limit}: {[round(v, 2) for v in series]}")
    i2c.assert_no_component_error(protomq, uid, mark)
