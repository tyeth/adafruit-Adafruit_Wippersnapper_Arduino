"""
HIL tests for Adafruit Wippersnapper firmware's I2C sensor drivers: every
sensor wired to the DUT is added at once, each reading every type in its
component definition; an i2c.Event holding all of a sensor's types
arrives at the broker from each; and once they're removed no more do.
The I2C API itself (Modify, rejected Adds, ...) is test_i2c_api.py's.

How Does This Work?
The sensors in SENSORS are wired to the DUT's I2C port at their Adafruit
breakouts' default addresses, each listed in the board entry's i2c_bus
probe (the i2c_devices and i2c_address_space fixtures). A sensor's
component definition in Wippersnapper_Components
(components/i2c/<name>/definition.json) lists its subcomponents; the
Add's types map keys each one's ws.sensor.Type by its 0-based position
in that list (i2c_helpers.py). After one checkin the broker sends an Add
per sensor, the test waits a period and checks the readings in the
i2c.Events the DUT publishes after it, then sends a Remove per sensor.
Every sensor is checked, whichever fail; the log ends with a PASS/FAIL
line per sensor.

checkin() power-cycles the DUT and the sensors with it, so sensors with a
warm-up start it: the SEN66 publishes some types before the rest
(collect_full_events() waits for an event with all of them), the ENS160
nothing for 3 minutes, so it's only in the slow run.

Exercises:
- `src/components/i2c/controller.cpp::Handle_Add()`
- `src/components/i2c/controller.cpp::Handle_Remove()`
- `src/components/i2c/controller.cpp::update()` (several drivers)
- each sensor's `src/components/i2c/drivers/drv*.h::begin()` and
  `getEvent*()`
"""

import logging
import time

import pytest

import i2c_helpers as i2c
from i2c_helpers import FIRST_EVENT_TIMEOUT, PERIOD, REMOVED_BUFFER


log = logging.getLogger("hil.i2c")


# The Adds' period, in seconds: short, so the check that the removed
# sensors publish nothing (a period plus REMOVED_BUFFER) is short too
SENSOR_PERIOD = 3
# How long after the last Remove reaches the DUT an event it published
# before handling it may still arrive
REMOVE_SETTLE = 1.0
# Which run a sensor is in: every run, only the slow one, or none
FAST, SLOW, DISABLED = "fast", "slow", "disabled"

# {driver/component name: (address, plausible bench readings per
# ws.sensor.Type, how long its first full event may take, run)}
SENSORS = {
    "bme688": (0x77, {
        "T_AMBIENT_TEMPERATURE": (5.0, 50.0),
        "T_AMBIENT_TEMPERATURE_FAHRENHEIT": (41.0, 122.0),
        "T_RELATIVE_HUMIDITY": (1.0, 99.0),
        "T_PRESSURE": (800.0, 1100.0),
        "T_ALTITUDE": (-1000.0, 3000.0),
        "T_GAS_RESISTANCE": (1.0, 1e8),
    }, FIRST_EVENT_TIMEOUT, FAST),
    "max44009": (0x4A, {
        "T_LUX": (0.0, 188000.0),
    }, FIRST_EVENT_TIMEOUT, FAST),
    "ina219": (0x40, {
        "T_VOLTAGE": (-0.5, 32.5),
        "T_CURRENT": (-3200.0, 3200.0),
    }, FIRST_EVENT_TIMEOUT, FAST),
    # Powered from its LiPo cell: without one it doesn't answer
    "max17048": (0x36, {
        "T_VOLTAGE": (3.0, 4.35),
        "T_UNITLESS_PERCENT": (0.0, 110.0),
    }, FIRST_EVENT_TIMEOUT, FAST),
    # Adafruit product 6403, the medical-accuracy "D" part
    "mlx90632d_med": (0x3A, {
        "T_AMBIENT_TEMPERATURE": (5.0, 50.0),
        "T_AMBIENT_TEMPERATURE_FAHRENHEIT": (41.0, 122.0),
        "T_OBJECT_TEMPERATURE": (-20.0, 100.0),
        "T_OBJECT_TEMPERATURE_FAHRENHEIT": (-4.0, 212.0),
    }, FIRST_EVENT_TIMEOUT, FAST),
    "vcnl4040": (0x60, {
        "T_LIGHT": (0.0, 6553.5),
        "T_PROXIMITY": (0.0, 65534.0),
    }, FIRST_EVENT_TIMEOUT, FAST),
    # 3 minute warm-up after power-on, then a backed-off read's retry
    "ens160": (0x53, {
        "T_TVOC": (0.0, 65000.0),
        "T_ECO2": (400.0, 65000.0),
        "T_RAW": (1.0, 5.0),
    }, 180 + PERIOD + 15, SLOW),
    # PM after 30 s, the VOC/NOx indexes after 60 s
    "sen66": (0x6B, {
        "T_AMBIENT_TEMPERATURE": (5.0, 50.0),
        "T_AMBIENT_TEMPERATURE_FAHRENHEIT": (41.0, 122.0),
        "T_RELATIVE_HUMIDITY": (1.0, 99.0),
        "T_PM10_STD": (0.0, 1000.0),
        "T_PM25_STD": (0.0, 1000.0),
        "T_PM100_STD": (0.0, 1000.0),
        "T_VOC_INDEX": (1.0, 500.0),
        "T_NOX_INDEX": (1.0, 500.0),
        "T_CO2": (300.0, 40000.0),
    }, 60 + 2 * PERIOD + 15, DISABLED),
}


@pytest.mark.parametrize("runs", [
    pytest.param((FAST,), id="fast"),
    pytest.param((FAST, SLOW), id="with_slow", marks=pytest.mark.slow),
])
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_all_i2c_sensor_types_add_read_remove(board, secrets_name,
                                              flashed_board, serial, protomq,
                                              checkin, i2c_address_space,
                                              i2c_devices,
                                              component_definition, runs):
    # --- Arrange ------------------------------------------------------
    sensors = []
    timeout = 0
    for name, (address, value_range, first_event, run) in SENSORS.items():
        if run not in runs:
            continue
        dev = i2c.device(i2c_devices, component_definition, name, address)
        sensors.append((dev, dev.types(), value_range))
        timeout = max(timeout, first_event)
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    # Why each sensor failed, {name: why}: one failing sensor mustn't hide
    # the others' results
    failures = {}

    # --- Act ----------------------------------------------------------
    added = []
    for dev, types, value_range in sensors:
        try:
            i2c.add(serial, protomq, uid, dev, i2c_address_space, types,
                    period=SENSOR_PERIOD)
        except AssertionError as e:
            errors = [err.get("message") for err in protomq.d2b_since(
                uid, "error.component", mark)
                if err.get("i2c", {}).get("address") == dev.address]
            failures[dev.name] = (f"Add failed: {errors or e}")
            continue
        added.append((dev, types, value_range))
    # The events read at the period, not the first read straight after the
    # Add
    time.sleep(SENSOR_PERIOD)
    period_mark = protomq.d2b_mark(uid)

    # --- Assert -------------------------------------------------------
    # An event from every sensor holding every type in its definition,
    # each in range
    found, failed = i2c.collect_full_events(protomq, uid, added,
                                            i2c_address_space, period_mark,
                                            timeout)
    for dev, _, _ in added:
        if dev.address in failed:
            failures[dev.name] = f"Event: {failed[dev.address]}"

    # --- Act ----------------------------------------------------------
    for dev, _, _ in added:
        i2c.remove(protomq, uid, dev, i2c_address_space)
    time.sleep(REMOVE_SETTLE)
    removed = protomq.d2b_mark(uid)

    # --- Assert -------------------------------------------------------
    # No more events once they're removed, for a period and then some
    time.sleep(SENSOR_PERIOD + REMOVED_BUFFER)
    by_address = {dev.address: dev.name for dev, _, _ in added}
    for event in protomq.d2b_since(uid, "i2c.event", removed):
        name = by_address.get(event.get("descriptor", {}).get("address"),
                              str(event.get("descriptor")))
        failures.setdefault(name, f"Remove: event after it: {event}")
    # Component errors other than the failed Adds'
    for err in protomq.d2b_since(uid, "error.component", mark):
        name = by_address.get(err.get("i2c", {}).get("address"))
        if name:
            failures.setdefault(name, f"error.component: {err}")

    for dev, _, _ in sensors:
        if dev.name in failures:
            log.error("FAIL %-14s 0x%02x  %s", dev.name, dev.address,
                      failures[dev.name])
        else:
            log.info("PASS %-14s 0x%02x  %s", dev.name, dev.address,
                     found[dev.address])
    assert not failures, "\n".join(
        f"{name}: {why}" for name, why in failures.items())
