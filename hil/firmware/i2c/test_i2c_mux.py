"""
HIL tests for Adafruit Wippersnapper firmware's I2C mux: sensors on a mux
channel read alongside sensors on the bus.

How Does This Work?
The board entry's i2c_bus probe in hil-boards.json lists the mux wired to
the DUT's I2C port and the addresses on each of its channels (the i2c_mux
fixture). The broker adds the mux, then each sensor in
hil_sensors.MUX_SENSORS on the channel the probe lists its address on,
and a sensor in hil_sensors.SENSORS on the bus, and expects full i2c.Events
from all of them, twice, so update() switches the mux between reads.

Exercises:
- `src/components/i2c/controller.cpp::Handle_Add()` (the mux, then a
  device on a mux channel, then one on the bus)
- `src/components/i2c/controller.cpp::InitMux()`
- `src/components/i2c/controller.cpp::SelectDriverMux()`
- `src/components/i2c/controller.cpp::update()`
- `src/components/i2c/model.cpp::SetI2cDeviceEventDeviceDescripton()` (mux address/channel)
"""

import logging

import pytest

import i2c_helpers as i2c
import dataclasses
import time

from hil_sensors import FAST, MUX_SENSORS, SENSORS
from i2c_helpers import (CHECKIN_TIMEOUT, FIRST_EVENT_TIMEOUT, INTERVAL_LATE,
                         PERIOD, REMOVED_BUFFER)


log = logging.getLogger("hil.i2c")

# A crash on the DUT, from boot on
CRASH = r"Guru Meditation|Backtrace:|abort\(\) was called"
# How long after a Remove reaches the DUT an event it published before
# handling it may still arrive
REMOVE_SETTLE = 1.0
# Where a PCA954x mux may be: 0x70 plus its A0-A2 pins
MUX_ADDRESSES = range(0x70, 0x78)

# How many full events to expect from each of the devices at one address
# on different mux channels
SAME_ADDRESS_EVENTS = 4

# The Modify's shorter period, in seconds, and how many intervals between
# events to time after it, as test_i2c_api.py's test_i2c_modify_period
MODIFIED_PERIOD = 3
MODIFIED_INTERVALS = 3

# How many full events to expect from each sensor: a second one means the
# mux was switched back to its channel after the bus sensor's read
EVENTS = 2


@pytest.fixture
def mux_sensors(i2c_mux, i2c_address_space, component_definition):
    """[(I2cDevice, types, value_range, address space, first-event
    timeout)] for every sensor in MUX_SENSORS on every mux channel the
    i2c_bus probe lists its address on, in channel order."""
    sensors = []
    for name, (address, value_range, first_event, _) in MUX_SENSORS.items():
        dev, channels = i2c.mux_device(i2c_mux, component_definition, name,
                                       address)
        for channel in channels:
            space = i2c.mux_address_space(i2c_address_space, i2c_mux,
                                          channel)
            sensors.append((dev, dev.types(), value_range, space,
                            first_event))
    if not sensors:
        pytest.skip("hil_sensors.MUX_SENSORS has no sensors")
    return sensors


@pytest.fixture
def bus_sensor(i2c_devices, i2c_address_space, component_definition):
    """(I2cDevice, types, value_range, address space, first-event timeout)
    for the first fast sensor in SENSORS the i2c_bus probe lists on the
    bus."""
    for name, (address, value_range, first_event, speed) in SENSORS.items():
        if speed == FAST and address in i2c_devices.addresses:
            dev = i2c.device(i2c_devices, component_definition, name,
                             address)
            return (dev, dev.types(), value_range, i2c_address_space,
                    first_event)
    pytest.skip("the i2c_bus probe lists no fast sensor from SENSORS")


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_mux_and_bus_sensors(board, secrets_name, flashed_board, serial,
                                 protomq, checkin, i2c_address_space,
                                 i2c_mux, mux_sensors, bus_sensor):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    start = protomq.d2b_mark(uid)
    sensors = mux_sensors + [bus_sensor]

    # --- Act ----------------------------------------------------------
    i2c.add_mux(serial, protomq, uid, i2c_address_space, i2c_mux)
    for dev, types, _, space, _ in sensors:
        i2c.add(serial, protomq, uid, dev, space, types)

    # --- Assert -------------------------------------------------------
    # EVENTS full events from every sensor, each with the descriptor it
    # was added with: the mux's address and channel, or the bus alone.
    # full_events_on() matches the whole descriptor, so sensors at the
    # same address on different channels don't mix.
    for dev, types, value_range, space, first in sensors:
        timeout = first + EVENTS * (PERIOD + INTERVAL_LATE + 5)
        events = i2c.full_events_on(protomq, uid, dev, space, types, start,
                                    EVENTS, timeout, value_range)
        for n, (_, values) in enumerate(events, 1):
            log.info("event %d from %s at 0x%02x on %s: %s", n, dev.name,
                     dev.address, space, values)
    i2c.assert_no_component_error(protomq, uid, start)


# --- Mux error handling ---------------------------------------------------


def _errors_for(protomq, uid, mark, address):
    """The error.components and error.devices since mark that name
    address, as the device of an i2c descriptor or the mux of a bus."""
    errors = [e for e in protomq.d2b_since(uid, "error.component", mark)
              if e.get("i2c", {}).get("address") == address]
    errors += [e for e in protomq.d2b_since(uid, "error.device", mark)
               if e.get("i2cBus", {}).get("muxAddress") == address]
    return errors


def _expect_error_for(protomq, uid, mark, address, timeout=CHECKIN_TIMEOUT):
    """Wait up to timeout for an error since mark naming address (see
    _errors_for()); the errors."""
    deadline = time.monotonic() + timeout
    while True:
        errors = _errors_for(protomq, uid, mark, address)
        if errors or time.monotonic() >= deadline:
            return errors
        time.sleep(0.2)


def _events_from(protomq, uid, mark, address):
    return [e for e in protomq.d2b_since(uid, "i2c.event", mark)
            if e.get("descriptor", {}).get("address") == address]


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_mux_channel_out_of_range(board, secrets_name, flashed_board,
                                      serial, protomq, checkin,
                                      i2c_address_space, i2c_mux,
                                      mux_sensors):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    i2c.add_mux(serial, protomq, uid, i2c_address_space, i2c_mux)
    dev, types, _, _, _ = mux_sensors[0]
    # The first channel past the mux's last, e.g. 4 on a PCA9546 (0-3)
    channel = len(i2c_mux.channels)
    space = i2c.mux_address_space(i2c_address_space, i2c_mux, channel)
    mark = protomq.d2b_mark(uid)
    serial_mark = serial.mark()

    # --- Act ----------------------------------------------------------
    msg = i2c.add_msg(dev, space, types)
    log.info("adding %s at 0x%02x on %s channel %d, which it doesn't have",
             dev.name, dev.address, i2c_mux.type, channel)
    protomq.publish(uid, {"i2c": {"add": msg}})
    protomq.expect_b2d_message(uid, "i2c.add", match=msg, timeout=5)

    # --- Assert -------------------------------------------------------
    # Handle_Add() rejects the channel with an error naming the device...
    errors = _expect_error_for(protomq, uid, mark, dev.address)
    assert errors, (f"expected an error for {dev.name} on {i2c_mux.type} "
                    f"channel {channel}, got none")
    log.info("error: %s", errors)
    # ...before creating a driver, not by failing to begin() it on
    # whatever channel was left selected
    serial.assert_none(r"\[i2c\] Creating sensor driver\.\.\.",
                       since=serial_mark, duration=1)
    # ...and never reads it
    time.sleep(PERIOD + REMOVED_BUFFER)
    events = _events_from(protomq, uid, mark, dev.address)
    assert not events, f"expected no i2c.event from {dev.name}: {events}"


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_mux_add_absent(board, secrets_name, flashed_board, serial,
                            protomq, checkin, i2c_devices, i2c_address_space,
                            i2c_mux):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    # The first mux address with nothing wired to it
    absent = next((a for a in MUX_ADDRESSES
                   if a not in i2c_devices.addresses), None)
    if absent is None:
        pytest.fail("every PCA954x address is wired on the i2c_bus probe",
                    pytrace=False)
    mux = dataclasses.replace(i2c_mux, address=absent)
    mark = protomq.d2b_mark(uid)

    # --- Act ----------------------------------------------------------
    msg = {"descriptor": i2c.mux_descriptor(i2c_address_space, mux),
           "name": mux.type}
    log.info("adding %s at 0x%02x, where nothing answers", mux.type, absent)
    protomq.publish(uid, {"i2c": {"add": msg}})
    protomq.expect_b2d_message(uid, "i2c.add", match=msg, timeout=5)

    # --- Assert -------------------------------------------------------
    # Handle_Add() finds nothing there and says so
    errors = _expect_error_for(protomq, uid, mark, absent)
    assert errors, (f"expected an error adding a {mux.type} at "
                    f"0x{absent:02x}, where nothing answers, got none")
    log.info("error: %s", errors)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_mux_readd_same(board, secrets_name, flashed_board, serial,
                            protomq, checkin, i2c_devices, i2c_address_space,
                            i2c_mux):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    i2c.add_mux(serial, protomq, uid, i2c_address_space, i2c_mux)

    # --- Act ----------------------------------------------------------
    # InitMux() keeps the mux it has
    i2c.add_mux(serial, protomq, uid, i2c_address_space, i2c_mux,
                already=True)

    # --- Assert -------------------------------------------------------
    # No error...
    errors = _errors_for(protomq, uid, mark, i2c_mux.address)
    assert not errors, f"expected no error re-adding the mux: {errors}"
    # ...and each channel still finds just its own devices, and the bus's
    for channel, found in i2c_mux.channels.items():
        space = i2c.mux_address_space(i2c_address_space, i2c_mux, channel)
        expected = sorted(set(i2c_devices.addresses) | set(found))
        protomq.publish(uid, {"i2c": {"probe": {"addressSpaces": [space]}}})
        probed = protomq.expect_d2b_message(uid, "i2c.probed",
                                            timeout=CHECKIN_TIMEOUT)
        results = probed.get("results", [])
        got = sorted(results[0].get("foundAddresses", [])) if results else []
        assert got == expected, (
            f"channel {channel}: expected "
            f"{[hex(a) for a in expected]}, found {[hex(a) for a in got]}")
    serial.assert_none(CRASH)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_mux_remove_with_children(board, secrets_name, flashed_board,
                                      serial, protomq, checkin,
                                      i2c_address_space, i2c_mux,
                                      mux_sensors, bus_sensor):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    i2c.add_mux(serial, protomq, uid, i2c_address_space, i2c_mux)
    child, child_types, child_range, child_space, first = mux_sensors[0]
    # A sensor on the bus too: the mux's removal must leave it be
    bus_dev, bus_types, bus_range, _, bus_first = bus_sensor
    mark = protomq.d2b_mark(uid)
    i2c.add(serial, protomq, uid, child, child_space, child_types)
    i2c.add(serial, protomq, uid, bus_dev, i2c_address_space, bus_types)
    for dev, types, value_range, space, timeout in (
            (child, child_types, child_range, child_space, first),
            (bus_dev, bus_types, bus_range, i2c_address_space, bus_first)):
        _, failed = i2c.collect_full_events(
            protomq, uid, [(dev, types, value_range)], space, mark, timeout)
        assert not failed, f"{dev.name} before the remove: {failed}"

    # --- Act ----------------------------------------------------------
    msg = {"descriptor": i2c.mux_descriptor(i2c_address_space, i2c_mux)}
    log.info("removing %s at 0x%02x with %s on channel %s still added",
             i2c_mux.type, i2c_mux.address, child.name,
             child_space.get("muxChannel", 0))
    removed = protomq.d2b_mark(uid)
    protomq.publish(uid, {"i2c": {"remove": msg}})
    protomq.expect_b2d_message(uid, "i2c.remove", match=msg, timeout=5)
    time.sleep(REMOVE_SETTLE)
    settled = protomq.d2b_mark(uid)
    time.sleep(PERIOD + REMOVED_BUFFER)

    # --- Assert -------------------------------------------------------
    # The DUT didn't crash...
    serial.assert_none(CRASH)
    # ...and the bus sensor still reads, on the bus
    _, failed = i2c.collect_full_events(
        protomq, uid, [(bus_dev, bus_types, bus_range)], i2c_address_space,
        settled, bus_first)
    assert not failed, (f"{bus_dev.name}, on the bus, after the mux's "
                        f"remove: {failed}")
    rejected = _errors_for(protomq, uid, removed, i2c_mux.address)
    child_events = _events_from(protomq, uid, settled, child.address)
    if rejected:
        # Either Handle_Remove() refused, so the mux and its child stay,
        # and the child still reads through it...
        log.info("remove rejected: %s", rejected)
        _, failed = i2c.collect_full_events(
            protomq, uid, [(child, child_types, child_range)], child_space,
            settled, first)
        assert not failed, (f"the mux's remove was rejected, but {child.name}"
                            f" stopped reading through it: {failed}")
    else:
        # ...or it removed the mux and its child with it: no reads through
        # a mux that's gone, and no errors from trying
        assert not child_events, (
            f"expected no i2c.event from {child.name} after its mux's "
            f"remove: {child_events}")
        child_errors = _errors_for(protomq, uid, removed, child.address)
        assert not child_errors, (
            f"expected no error from {child.name} after its mux's remove: "
            f"{child_errors}")


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_mux_modify_period(board, secrets_name, flashed_board, serial,
                               protomq, checkin, i2c_address_space, i2c_mux,
                               mux_sensors):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    i2c.add_mux(serial, protomq, uid, i2c_address_space, i2c_mux)
    dev, types, value_range, space, first = mux_sensors[0]
    i2c.add(serial, protomq, uid, dev, space, types)
    i2c.expect_event(protomq, uid, dev, space, first, types, value_range)

    # --- Act ----------------------------------------------------------
    # Sent just after an event, so none at the old period is in flight
    i2c.modify(serial, protomq, uid, dev, space, types,
               period=MODIFIED_PERIOD)

    # --- Assert -------------------------------------------------------
    # The new driver, on the same mux channel, reads at once, then every
    # MODIFIED_PERIOD
    times = [i2c.expect_event(protomq, uid, dev, space, FIRST_EVENT_TIMEOUT,
                              types, value_range)[0]]
    for _ in range(MODIFIED_INTERVALS):
        times.append(i2c.expect_event(protomq, uid, dev, space,
                                      MODIFIED_PERIOD + INTERVAL_LATE + 5,
                                      types, value_range)[0])
    i2c.assert_period(times, MODIFIED_PERIOD)
    i2c.assert_no_component_error(protomq, uid, mark)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_mux_add_wrong_channel(board, secrets_name, flashed_board,
                                   serial, protomq, checkin,
                                   i2c_address_space, i2c_mux, mux_sensors):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    i2c.add_mux(serial, protomq, uid, i2c_address_space, i2c_mux)
    dev, types, _, _, _ = mux_sensors[0]
    # The mux's last channel the i2c_bus probe doesn't list it on
    channel = max((c for c, found in i2c_mux.channels.items()
                   if dev.address not in found), default=None)
    if channel is None:
        pytest.fail(f"every {i2c_mux.type} channel lists 0x{dev.address:02x}",
                    pytrace=False)
    space = i2c.mux_address_space(i2c_address_space, i2c_mux, channel)
    add = i2c.add_msg(dev, space, types)
    log.info("adding %s at 0x%02x on %s channel %d, which it isn't on",
             dev.name, dev.address, i2c_mux.type, channel)

    # --- Act ----------------------------------------------------------
    protomq.publish(uid, {"i2c": {"add": add}})

    # --- Assert -------------------------------------------------------
    # begin() gets no ACK on that channel, so Handle_Add() rejects it...
    protomq.expect_b2d_message(uid, "i2c.add", match=add, timeout=5)
    serial.expect(r"^\[i2c\] ERROR: Failed to initialize I2C driver!$",
                  timeout=CHECKIN_TIMEOUT)
    i2c.expect_component_error(
        protomq, uid, "Failed to initialize driver, check wiring + address!",
        address=dev.address)
    # ...and never reads it
    i2c.assert_no_events(protomq, uid)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_mux_same_address(board, secrets_name, flashed_board, serial,
                              protomq, checkin, i2c_address_space, i2c_mux,
                              mux_sensors):
    # --- Arrange ------------------------------------------------------
    # The first address the i2c_bus probe lists on more than one channel
    by_address = {}
    for sensor in mux_sensors:
        by_address.setdefault(sensor[0].address, []).append(sensor)
    same = next((s for s in by_address.values() if len(s) > 1), None)
    if same is None:
        pytest.skip("the i2c_bus probe's mux lists no address on more than "
                    "one channel")
    dev = same[0][0]
    channels = [space.get("muxChannel", 0) for _, _, _, space, _ in same]
    uid = checkin()
    mark = protomq.d2b_mark(uid)

    # --- Act ----------------------------------------------------------
    # The mux, then a driver per channel at the same address: Handle_Add()
    # removes any driver at the address and channel first, so the second
    # mustn't replace the first
    i2c.add_mux(serial, protomq, uid, i2c_address_space, i2c_mux)
    for dev, types, _, space, _ in same:
        log.info("adding %s at 0x%02x on %s channel %d", dev.name,
                 dev.address, i2c_mux.type, space.get("muxChannel", 0))
        i2c.add(serial, protomq, uid, dev, space, types)

    # --- Assert -------------------------------------------------------
    # Each channel's device publishes full events carrying its own
    # channel, every PERIOD: both read, neither twice per period
    readings = {}
    for dev, types, value_range, space, first in same:
        channel = space.get("muxChannel", 0)
        timeout = first + SAME_ADDRESS_EVENTS * (PERIOD + INTERVAL_LATE + 5)
        events = i2c.full_events_on(protomq, uid, dev, space, types, mark,
                                    SAME_ADDRESS_EVENTS, timeout,
                                    value_range)
        readings[channel] = [values for _, values in events]
        for n, values in enumerate(readings[channel], 1):
            log.info("channel %d event %d: %s", channel, n, values)
        i2c.assert_period([received for received, _ in events])
    # Every event at the address names the mux and a channel it's mapped
    # to in hil-boards.json: none read through another channel, or
    # reported as on the bus
    for event in protomq.d2b_since(uid, "i2c.event", mark):
        got = event.get("descriptor", {})
        if got.get("address") != dev.address:
            continue
        space = got.get("addressSpace", {})
        assert (space.get("muxAddress") == i2c_mux.address
                and space.get("muxChannel", 0) in channels), (
            f"expected 0x{dev.address:02x} only on {i2c_mux.type} "
            f"0x{i2c_mux.address:02x} channels {channels}, got {got}")
    # The channels' readings aren't copies of each other: if the mux
    # stayed on one channel, the drivers would read the same chip.
    # Two chips can agree on a reading, but not on every one.
    first_channel, *others = channels
    for channel in others:
        assert readings[channel] != readings[first_channel], (
            f"channel {channel}'s readings match channel {first_channel}'s, "
            f"every one: is the mux switching? {readings[channel]}")
    i2c.assert_no_component_error(protomq, uid, mark)


# --- Mux channel state and child lifecycle --------------------------------


def _probe_found(protomq, uid, address_space, addresses):
    """Send an i2c.Probe of address_space for addresses, and return the
    sorted addresses its i2c.Probed found (none if it has no result)."""
    protomq.publish(uid, {"i2c": {"probe": {"addressSpaces": [address_space],
                                            "addresses": list(addresses)}}})
    probed = protomq.expect_d2b_message(uid, "i2c.probed",
                                        timeout=CHECKIN_TIMEOUT)
    results = probed.get("results", [])
    return sorted(results[0].get("foundAddresses", [])) if results else []


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_mux_bus_read_leaves_channels_off(board, secrets_name,
                                              flashed_board, serial, protomq,
                                              checkin, i2c_devices,
                                              i2c_address_space, i2c_mux,
                                              bus_sensor):
    # --- Arrange ------------------------------------------------------
    # The devices on mux channel 0, the channel a bus device's driver
    # stores (the Add's descriptor has no muxChannel, a zero value)
    hidden = [a for a in i2c_mux.channels.get(0, [])
              if a not in i2c_devices.addresses]
    if not hidden:
        pytest.skip("the i2c_bus probe's mux lists nothing on channel 0")
    dev, types, value_range, space, first = bus_sensor
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    i2c.add_mux(serial, protomq, uid, i2c_address_space, i2c_mux)
    i2c.add(serial, protomq, uid, dev, space, types)

    # --- Act ----------------------------------------------------------
    # The bus sensor reads: update() calls SelectDriverMux() first
    i2c.full_events_on(protomq, uid, dev, space, types, mark, 1, first,
                       value_range)

    # --- Assert -------------------------------------------------------
    # A bus device's read selects no mux channel, so channel 0's devices
    # stay off the bus: a Probe of the bus alone, which selects nothing,
    # doesn't find them
    found = _probe_found(protomq, uid, i2c_address_space, hidden)
    assert not found, (
        f"after {dev.name}'s read on the bus, {i2c_mux.type} channel 0's "
        f"{[hex(a) for a in found]} answer on the bus: the read left "
        "channel 0 selected")
    i2c.assert_no_component_error(protomq, uid, mark)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_mux_remove_child_wrong_channel(board, secrets_name,
                                            flashed_board, serial, protomq,
                                            checkin, i2c_address_space,
                                            i2c_mux, mux_sensors):
    # --- Arrange ------------------------------------------------------
    dev, types, value_range, space, first = mux_sensors[0]
    # The first channel the i2c_bus probe doesn't list the child on
    wrong = next((c for c, found in i2c_mux.channels.items()
                  if dev.address not in found), None)
    if wrong is None:
        pytest.fail(f"every {i2c_mux.type} channel lists 0x{dev.address:02x}",
                    pytrace=False)
    wrong_space = i2c.mux_address_space(i2c_address_space, i2c_mux, wrong)
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    i2c.add_mux(serial, protomq, uid, i2c_address_space, i2c_mux)
    i2c.add(serial, protomq, uid, dev, space, types)
    i2c.full_events_on(protomq, uid, dev, space, types, mark, 1, first,
                       value_range)

    # --- Act ----------------------------------------------------------
    log.info("removing %s at 0x%02x on channel %d, it's on channel %d",
             dev.name, dev.address, wrong, space.get("muxChannel", 0))
    removed = protomq.d2b_mark(uid)
    i2c.remove(protomq, uid, dev, wrong_space)

    # --- Assert -------------------------------------------------------
    # Handle_Remove() finds no driver at that address on that channel...
    i2c.expect_component_error(
        protomq, uid, "Failed to remove I2C device from MUX channel",
        address=dev.address)
    # ...and leaves the child on its own channel reading
    i2c.full_events_on(protomq, uid, dev, space, types, removed, EVENTS,
                       EVENTS * (PERIOD + INTERVAL_LATE + 5), value_range)
    serial.assert_none(CRASH)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_i2c_mux_remove_and_readd(board, secrets_name, flashed_board, serial,
                                  protomq, checkin, i2c_address_space,
                                  i2c_mux, mux_sensors):
    # --- Arrange ------------------------------------------------------
    dev, types, value_range, space, first = mux_sensors[0]
    uid = checkin()
    mark = protomq.d2b_mark(uid)
    i2c.add_mux(serial, protomq, uid, i2c_address_space, i2c_mux)
    i2c.add(serial, protomq, uid, dev, space, types)
    i2c.full_events_on(protomq, uid, dev, space, types, mark, 1, first,
                       value_range)
    # The mux's remove takes its child with it
    msg = {"descriptor": i2c.mux_descriptor(i2c_address_space, i2c_mux)}
    protomq.publish(uid, {"i2c": {"remove": msg}})
    protomq.expect_b2d_message(uid, "i2c.remove", match=msg, timeout=5)
    time.sleep(REMOVE_SETTLE)
    gone = protomq.d2b_mark(uid)
    time.sleep(PERIOD + REMOVED_BUFFER)
    events = _events_from(protomq, uid, gone, dev.address)
    assert not events, (
        f"expected no i2c.event from {dev.name} after its mux's remove: "
        f"{events}")

    # --- Act ----------------------------------------------------------
    # The mux, then its child, again: RemoveMux() left nothing behind
    readded = protomq.d2b_mark(uid)
    i2c.add_mux(serial, protomq, uid, i2c_address_space, i2c_mux)
    i2c.add(serial, protomq, uid, dev, space, types)

    # --- Assert -------------------------------------------------------
    # The child reads through the mux again, on its channel, with no error
    i2c.full_events_on(protomq, uid, dev, space, types, readded, EVENTS,
                       first + EVENTS * (PERIOD + INTERVAL_LATE + 5),
                       value_range)
    i2c.assert_no_component_error(protomq, uid, readded)
    serial.assert_none(CRASH)
