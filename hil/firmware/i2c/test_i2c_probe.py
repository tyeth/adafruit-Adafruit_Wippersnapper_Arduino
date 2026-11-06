"""
HIL tests for Adafruit Wippersnapper firmware's I2C Probe.

How Does This Work?
I2C devices (today a BME688, MAX44009, INA219, MAX17048, MLX90632,
ENS160, SEN66 and VCNL4040) are wired to one of the DUT's I2C ports. The
board entry's i2c_bus probe names the port (its i2cPortId in
the board's definition.json, whose SCL/SDA pins make the AddressSpace)
and lists every device on it (the i2c_devices and i2c_address_space
fixtures). The broker sends an i2c.Probe for that AddressSpace, and the
tests check the i2c.Probed the DUT publishes: one AddressSpaceResult
whose AddressSpace is the one sent, and whose found_addresses are the
devices it should find.

Exercises:
- `src/components/i2c/controller.cpp::Router()` (i2c.Probe)
- `src/components/i2c/controller.cpp::Handle_Probe()`
- `src/components/i2c/controller.cpp::findOrCreateBus()`
- `src/components/i2c/controller.cpp::publishProbed()`
- `src/components/i2c/controller.cpp::findOrCreateBus()` (invalid pins)
- `src/components/i2c/hardware.cpp::begin()` (a bus that can't start)
- `src/components/error/handler.cpp::publishDeviceError()` (with i2c_bus)
- `src/components/i2c/hardware.cpp::ProbeAddresses()` (a list, and all)
- `src/components/i2c/model.cpp::EncodeProbed()`
"""

import logging

import pytest


log = logging.getLogger("hil.i2c")

# Addresses where the DUT must find nothing: reserved addresses, which
# ProbeAddresses() skips (0x00-0x07 and 0x78-0x7F; 0x00 is also proto3's
# zero value), and 0x50, a valid address with nothing wired to it
INVALID_ADDRESSES = [0x00, 0x03, 0x07, 0x50, 0x78, 0x7F]
# AddressSpaces whose bus can't be created, and the line findOrCreateBus()
# logs for each: GPIOs that exist but are input-only (ESP32 GPIO36/39, no
# output driver or pull-ups, nothing wired), which begin() fails on; pin
# names with no GPIO number, which WsPinNameToNum() rejects; and GPIO
# numbers the ESP32 doesn't have, which begin() fails on
INVALID_BUSES = {
    "input_only_gpio": ({"pinScl": "D39", "pinSda": "D36"},
                        r"^\[i2c\] ERROR: Failed to initialize I2C bus!$"),
    "no_gpio_number": ({"pinScl": "SCL", "pinSda": "SDA"},
                       r"^\[i2c\] ERROR: Invalid SCL/SDA pin name!$"),
    "nonexistent_gpio": ({"pinScl": "D99", "pinSda": "D98"},
                         r"^\[i2c\] ERROR: Failed to initialize I2C bus!$"),
}
# How long after checkin the DUT may take to handle a message, as the
# checkin fixture allows for the checkin itself. A scan of all 112
# addresses takes well under a second.
CHECKIN_TIMEOUT = 30


def _hex(addresses):
    return [f"0x{a:02x}" for a in addresses]


def _probe(serial, protomq, uid, address_space, addresses=(),
           expect_probed=True):
    """Send an i2c.Probe of address_space for addresses (all of them if
    empty), and expect the DUT to decode it. Returns the i2c.Probed it
    publishes, or None if not expect_probed (to expect what the DUT
    publishes before it first)."""
    probe = {"addressSpaces": [address_space]}
    if addresses:
        probe["addresses"] = list(addresses)
    log.info("probing %s for %s", address_space,
             _hex(addresses) if addresses else "all addresses")
    protomq.publish(uid, {"i2c": {"probe": probe}})
    # proto3 JSON leaves out an empty list, so match on the AddressSpaces
    protomq.expect_b2d_message(uid, "i2c.probe",
                               match={"addressSpaces": [address_space]},
                               timeout=5)
    serial.expect(r"^\[i2c\] Probe decoded, address spaces=1$",
                  timeout=CHECKIN_TIMEOUT)
    if not expect_probed:
        return None
    return protomq.expect_d2b_message(uid, "i2c.probed",
                                      timeout=CHECKIN_TIMEOUT)


def _assert_probed(serial, probed, address_space, expected,
                   check_serial=True):
    """Expect probed to hold one AddressSpaceResult, for address_space,
    which found exactly the addresses in expected. Per i2c.proto's Probed,
    a successful probe that found nothing need not be returned, so when
    expected is empty, no result at all also passes."""
    results = probed.get("results", [])
    if not expected and not results:
        log.info("found nothing (no AddressSpaceResult returned)")
        return
    assert len(results) == 1, (
        f"expected one AddressSpaceResult, got {len(results)}: {probed}")
    result = results[0]
    # Exact, not ProtoMQ's partial match: the DUT echoes the AddressSpace
    # as sent, with no mux fields
    assert result.get("addressSpace") == address_space, (
        f"expected the AddressSpace sent, {address_space}, got "
        f"{result.get('addressSpace')}: {probed}")
    # proto3 JSON leaves out an empty foundAddresses
    found = sorted(result.get("foundAddresses", []))
    log.info("found %s", _hex(found))
    assert found == sorted(expected), (
        f"expected found addresses {_hex(sorted(expected))}, got "
        f"{_hex(found)}: {probed}")
    if check_serial:
        serial.expect(rf"^\[i2c\] Probed address space 0, found "
                      rf"{len(expected)} devices\.$", timeout=5)


@pytest.fixture
def invalid_addresses(i2c_devices):
    """INVALID_ADDRESSES, none of which may be a device on the bus."""
    wired = set(INVALID_ADDRESSES) & set(i2c_devices.addresses)
    if wired:
        pytest.fail(f"i2c_bus probe lists {_hex(sorted(wired))}, which the "
                    "tests expect nothing at: change INVALID_ADDRESSES",
                    pytrace=False)
    return INVALID_ADDRESSES


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_probe_present(board, secrets_name, flashed_board, serial,
                           protomq, checkin, i2c_devices, i2c_address_space,
                           device_address):
    # --- Arrange ------------------------------------------------------
    uid = checkin()

    # --- Act ----------------------------------------------------------
    probed = _probe(serial, protomq, uid, i2c_address_space,
                    [device_address])

    # --- Assert -------------------------------------------------------
    _assert_probed(serial, probed, i2c_address_space, [device_address])


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_probe_invalid(board, secrets_name, flashed_board, serial,
                           protomq, checkin, i2c_address_space,
                           invalid_addresses):
    # --- Arrange ------------------------------------------------------
    uid = checkin()

    # --- Act ----------------------------------------------------------
    probed = _probe(serial, protomq, uid, i2c_address_space,
                    invalid_addresses)

    # --- Assert -------------------------------------------------------
    # A result for the AddressSpace, but nothing found
    _assert_probed(serial, probed, i2c_address_space, [])


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_probe_valid_and_invalid(board, secrets_name, flashed_board,
                                     serial, protomq, checkin, i2c_devices,
                                     i2c_address_space, invalid_addresses):
    # --- Arrange ------------------------------------------------------
    uid = checkin()

    # --- Act ----------------------------------------------------------
    probed = _probe(serial, protomq, uid, i2c_address_space,
                    invalid_addresses + i2c_devices.addresses)

    # --- Assert -------------------------------------------------------
    # Only the devices are found
    _assert_probed(serial, probed, i2c_address_space, i2c_devices.addresses)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_probe_all(board, secrets_name, flashed_board, serial, protomq,
                       checkin, i2c_devices, i2c_address_space):
    # --- Arrange ------------------------------------------------------
    uid = checkin()

    # --- Act ----------------------------------------------------------
    # No addresses: ProbeAddresses() scans all 112, 0x08-0x77
    probed = _probe(serial, protomq, uid, i2c_address_space)

    # --- Assert -------------------------------------------------------
    # Every device on the bus, and nothing else
    _assert_probed(serial, probed, i2c_address_space, i2c_devices.addresses)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
@pytest.mark.parametrize("bus", INVALID_BUSES)
def test_i2c_probe_invalid_bus(board, secrets_name, flashed_board, serial,
                               protomq, checkin, bus):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    address_space, log_line = INVALID_BUSES[bus]
    mark = protomq.d2b_mark(uid)

    # --- Act ----------------------------------------------------------
    _probe(serial, protomq, uid, address_space, expect_probed=False)

    # --- Assert -------------------------------------------------------
    # findOrCreateBus() can't create the bus...
    serial.expect(log_line, timeout=5)
    serial.assert_none(r"^\[i2c\] I2C bus initialized successfully!$",
                       duration=1)
    # ...so the DUT reports it as a device error, a bus belongs to the
    # device, not to a component: error.device naming the bus as sent
    # (DeviceError.i2c_bus) with a message...
    error = protomq.expect_d2b_message(uid, "error.device",
                                       match={"i2cBus": address_space},
                                       timeout=CHECKIN_TIMEOUT)
    assert error.get("i2cBus") == address_space, (
        f"expected an error.device for the bus sent, {address_space}, "
        f"got {error}")
    assert error.get("message"), f"expected an error message: {error}"
    # ...and the Probed, sent before or after the error, whose result
    # found nothing (its AddressSpace may be empty, the error names the bus)
    earlier = protomq.d2b_since(uid, "i2c.probed", mark)
    probed = earlier[0] if earlier else protomq.expect_d2b_message(
        uid, "i2c.probed", timeout=CHECKIN_TIMEOUT)
    found = [a for r in probed.get("results", [])
             for a in r.get("foundAddresses", [])]
    assert not found, f"expected nothing found, got {_hex(found)}: {probed}"
    # ...and no component error, before or after either
    component = protomq.d2b_since(uid, "error.component", mark)
    assert not component, (
        f"expected an error.device, not an error.component: {component}")
