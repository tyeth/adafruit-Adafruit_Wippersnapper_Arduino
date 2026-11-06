"""
HIL tests for Adafruit Wippersnapper firmware's I2C sensor API: adding,
reading, modifying and removing an I2C sensor, and the errors for Adds
and Removes the DUT rejects. Sensor-agnostic: what's checked is the API,
not the readings, which test_i2c_sensors.py checks.

How Does This Work?
API_DEVICE, an I2C sensor with at least 5 subcomponents (today a BME688
at 0x77), is wired to one of the DUT's I2C ports; the board entry's
i2c_bus probe names the port and lists the device (the i2c_devices and
i2c_address_space fixtures). Its component definition in
Wippersnapper_Components lists its subcomponents; a test picks some by
position, and the Add's types map keys each one's ws.sensor.Type by that
0-based position (i2c_helpers.py). The broker sends
the Add, and the tests check that the DUT adds the driver without a
component error, then publishes an i2c.Event for the device holding a
sensor event per type in the Add, under the same keys, at the Add's
period; and that Removes and Modifies stop the events or change them.

The rejected-Add and Remove tests need no device answering: they send
Adds the DUT turns down before (or on) talking to the bus, and a Remove
with nothing added.

Exercises:
- `src/components/i2c/controller.cpp::Router()` (Add, Remove, Modify)
- `src/components/i2c/controller.cpp::Handle_Add()` (and each rejection)
- `src/components/i2c/controller.cpp::Handle_Remove()`
- `src/components/i2c/controller.cpp::Handle_Modify()`
- `src/components/i2c/controller.cpp::RemoveDriver()`
- `src/components/i2c/controller.cpp::findOrCreateBus()` (bad pins)
- `src/components/i2c/controller.cpp::update()`
- `src/components/i2c/drivers/drvBase.h::EnableSensorReads()`
- `src/components/i2c/drivers/drvBase.h::SetSensorPeriod()`
- `src/components/i2c/model.cpp::AddI2cDeviceSensorEvent()`
- `src/components/i2c/model.cpp::EncodeI2cDeviceEvent()`
- `src/components/error/handler.cpp::publishComponentError()` (i2c)
"""

import pytest

import i2c_helpers as i2c
from i2c_helpers import (CHECKIN_TIMEOUT, FIRST_EVENT_TIMEOUT, INTERVAL_LATE,
                         PERIOD)


# The sensor the API tests drive: its driver/component name and address.
# Any supported I2C sensor with at least 5 subcomponents will do.
API_DEVICE = ("bme688", 0x77)
# Subcomponents to read, by position in the definition's subcomponents
# array, which is also each one's key in the Add's types map
ONE_TYPE = (0,)
TWO_TYPES = (0, 1)
# Every other subcomponent, so the keys skip: 0, 2, 4
SPARSE_TYPES = (0, 2, 4)
# The Modify's shorter period, in seconds
MODIFIED_PERIOD = 3
# How many intervals between events to time after the Modify
MODIFIED_INTERVALS = 3
# An address with nothing wired to it, in API_DEVICE's i2cAddresses (the
# BME688's other address)
EMPTY_ADDRESS = 0x76
# Rejected Adds: {id: (changes to a valid Add, the error.component message
# Handle_Add() publishes, a line it logs or None)}. The bus cases are the
# AddressSpaces test_i2c_probe.py's INVALID_BUSES can't create, which
# Handle_Add() reports as a component error.
BUS_ERROR = "Failed to find/create I2C bus for device!"
INVALID_ADDS = {
    "empty_name": ({"name": ""}, "Empty device name in I2C add message!",
                   None),
    "zero_address": ({"address": 0x00},
                     "Invalid device address in I2C add message!", None),
    # 0x7F is reserved (10-bit addressing), nothing can ACK it
    "reserved_address": ({"address": 0x7F},
                         "Failed to initialize driver, check wiring + "
                         "address!",
                         r"^\[i2c\] ERROR: Failed to initialize I2C driver!$"),
    "unknown_driver": ({"name": "notadriver"}, "Failed to create driver!",
                       r"^\[i2c\] Creating sensor driver\.\.\.$"),
    "input_only_gpio": ({"addressSpace": {"pinScl": "D39", "pinSda": "D36"}},
                        BUS_ERROR,
                        r"^\[i2c\] ERROR: Failed to initialize I2C bus!$"),
    "no_gpio_number": ({"addressSpace": {"pinScl": "SCL", "pinSda": "SDA"}},
                       BUS_ERROR,
                       r"^\[i2c\] ERROR: Invalid SCL/SDA pin name!$"),
    "nonexistent_gpio": ({"addressSpace": {"pinScl": "D99", "pinSda": "D98"}},
                         BUS_ERROR,
                         r"^\[i2c\] ERROR: Failed to initialize I2C bus!$"),
}


@pytest.fixture
def api_device(i2c_devices, component_definition):
    """API_DEVICE as an i2c_helpers.I2cDevice."""
    return i2c.device(i2c_devices, component_definition, *API_DEVICE)


# --- Add, read, Modify, Remove: need API_DEVICE answering ------------------


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
@pytest.mark.parametrize("indexes", [ONE_TYPE, TWO_TYPES, SPARSE_TYPES],
                         ids=["one_type", "two_types", "sparse_keys"])
def test_i2c_add_event(board, secrets_name, flashed_board, serial, protomq,
                       checkin, i2c_address_space, api_device, indexes):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    types = api_device.types_at(indexes)

    # --- Act ----------------------------------------------------------
    i2c.add(serial, protomq, uid, api_device, i2c_address_space, types)

    # --- Assert -------------------------------------------------------
    # The driver reads at once, then every PERIOD, under the Add's keys
    first, _ = i2c.expect_event(protomq, uid, api_device, i2c_address_space,
                                FIRST_EVENT_TIMEOUT, types)
    second, _ = i2c.expect_event(protomq, uid, api_device, i2c_address_space,
                                 PERIOD + INTERVAL_LATE + 5, types)
    i2c.assert_period([first, second])
    # No component error, from the Add or either read
    i2c.assert_no_component_error(protomq, uid, mark)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_remove_stops_events(board, secrets_name, flashed_board, serial,
                                 protomq, checkin, i2c_address_space,
                                 api_device):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    types = api_device.types_at(ONE_TYPE)
    i2c.add(serial, protomq, uid, api_device, i2c_address_space, types)
    i2c.expect_event(protomq, uid, api_device, i2c_address_space,
                     FIRST_EVENT_TIMEOUT, types)

    # --- Act ----------------------------------------------------------
    # Sent just after an event, so none is in flight
    i2c.remove(protomq, uid, api_device, i2c_address_space)

    # --- Assert -------------------------------------------------------
    # No event for a period and then some, when the next would be due
    i2c.assert_no_events(protomq, uid)
    # ...and no component error: Handle_Remove() found the driver
    i2c.assert_no_component_error(protomq, uid, mark)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_modify_period(board, secrets_name, flashed_board, serial,
                           protomq, checkin, i2c_address_space, api_device):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    types = api_device.types_at(ONE_TYPE)
    i2c.add(serial, protomq, uid, api_device, i2c_address_space, types)
    i2c.expect_event(protomq, uid, api_device, i2c_address_space,
                     FIRST_EVENT_TIMEOUT, types)

    # --- Act ----------------------------------------------------------
    # Sent just after an event, so none at the old period is in flight
    i2c.modify(serial, protomq, uid, api_device, i2c_address_space, types,
               period=MODIFIED_PERIOD)

    # --- Assert -------------------------------------------------------
    # The new driver reads at once, then every MODIFIED_PERIOD
    times = [i2c.expect_event(protomq, uid, api_device, i2c_address_space,
                              FIRST_EVENT_TIMEOUT, types)[0]]
    for _ in range(MODIFIED_INTERVALS):
        times.append(i2c.expect_event(protomq, uid, api_device,
                                      i2c_address_space,
                                      MODIFIED_PERIOD + INTERVAL_LATE + 5,
                                      types)[0])
    i2c.assert_period(times, MODIFIED_PERIOD)
    i2c.assert_no_component_error(protomq, uid, mark)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_modify_add_type(board, secrets_name, flashed_board, serial,
                             protomq, checkin, i2c_address_space,
                             api_device):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    old_types = api_device.types_at(ONE_TYPE)
    i2c.add(serial, protomq, uid, api_device, i2c_address_space, old_types)
    i2c.expect_event(protomq, uid, api_device, i2c_address_space,
                     FIRST_EVENT_TIMEOUT, old_types)

    # --- Act ----------------------------------------------------------
    # Sent just after an event, so none with only the old type is in flight
    types = api_device.types_at(TWO_TYPES)
    i2c.modify(serial, protomq, uid, api_device, i2c_address_space, types)

    # --- Assert -------------------------------------------------------
    # The new driver reads at once, both the old type and the new
    i2c.expect_event(protomq, uid, api_device, i2c_address_space,
                     FIRST_EVENT_TIMEOUT, types)
    i2c.assert_no_component_error(protomq, uid, mark)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_modify_remove_type(board, secrets_name, flashed_board, serial,
                                protomq, checkin, i2c_address_space,
                                api_device):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    old_types = api_device.types_at(TWO_TYPES)
    i2c.add(serial, protomq, uid, api_device, i2c_address_space, old_types)
    i2c.expect_event(protomq, uid, api_device, i2c_address_space,
                     FIRST_EVENT_TIMEOUT, old_types)

    # --- Act ----------------------------------------------------------
    # Sent just after an event, so none with both types is in flight
    types = api_device.types_at(ONE_TYPE)
    i2c.modify(serial, protomq, uid, api_device, i2c_address_space, types)

    # --- Assert -------------------------------------------------------
    # The new driver reads at once, only the type kept: expect_event()
    # matches the keys exactly, so the dropped type's key must be gone
    i2c.expect_event(protomq, uid, api_device, i2c_address_space,
                     FIRST_EVENT_TIMEOUT, types)
    i2c.assert_no_component_error(protomq, uid, mark)


# --- Rejected Adds and Removes: no device needs to answer ------------------


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_add_wrong_address(board, secrets_name, flashed_board, serial,
                               protomq, checkin, i2c_devices,
                               i2c_address_space, api_device):
    # --- Arrange ------------------------------------------------------
    if EMPTY_ADDRESS in i2c_devices.addresses:
        pytest.fail(f"i2c_bus probe lists 0x{EMPTY_ADDRESS:02x}, which this "
                    "test expects nothing at: change EMPTY_ADDRESS",
                    pytrace=False)
    uid = checkin()
    add = i2c.add_msg(api_device, i2c_address_space,
                      api_device.types_at(ONE_TYPE))
    add["descriptor"]["address"] = EMPTY_ADDRESS

    # --- Act ----------------------------------------------------------
    protomq.publish(uid, {"i2c": {"add": add}})

    # --- Assert -------------------------------------------------------
    # begin() gets no ACK, so Handle_Add() rejects it...
    protomq.expect_b2d_message(uid, "i2c.add", match=add, timeout=5)
    serial.expect(r"^\[i2c\] ERROR: Failed to initialize I2C driver!$",
                  timeout=CHECKIN_TIMEOUT)
    i2c.expect_component_error(
        protomq, uid, "Failed to initialize driver, check wiring + address!",
        address=EMPTY_ADDRESS)
    # ...and never reads it
    i2c.assert_no_events(protomq, uid)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_remove_before_add(board, secrets_name, flashed_board, serial,
                               protomq, checkin, i2c_address_space,
                               api_device):
    # --- Arrange ------------------------------------------------------
    # Nothing added, so there's no driver to remove
    uid = checkin()

    # --- Act ----------------------------------------------------------
    i2c.remove(protomq, uid, api_device, i2c_address_space)

    # --- Assert -------------------------------------------------------
    i2c.expect_component_error(protomq, uid,
                               "Failed to remove I2C device from bus",
                               address=api_device.address)
    i2c.assert_no_events(protomq, uid, within=FIRST_EVENT_TIMEOUT)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
@pytest.mark.parametrize("case", INVALID_ADDS)
def test_i2c_add_invalid(board, secrets_name, flashed_board, serial, protomq,
                         checkin, i2c_address_space, api_device, case):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    changes, message, log_line = INVALID_ADDS[case]
    add = i2c.add_msg(api_device, i2c_address_space,
                      api_device.types_at(ONE_TYPE))
    for field, value in changes.items():
        if field in ("address", "addressSpace"):
            add["descriptor"][field] = value
        else:
            add[field] = value

    # --- Act ----------------------------------------------------------
    protomq.publish(uid, {"i2c": {"add": add}})

    # --- Assert -------------------------------------------------------
    # Matched on nothing: proto3 JSON leaves out the empty name and zero
    # address, and it's the only Add sent
    protomq.expect_b2d_message(uid, "i2c.add", timeout=5)
    if log_line:
        serial.expect(log_line, timeout=CHECKIN_TIMEOUT)
    i2c.expect_component_error(protomq, uid, message)
    serial.assert_none(r"^\[i2c\] Driver initialized: ?$")
    # A new driver reads at once, so none in this long means none at all
    i2c.assert_no_events(protomq, uid, within=FIRST_EVENT_TIMEOUT)
