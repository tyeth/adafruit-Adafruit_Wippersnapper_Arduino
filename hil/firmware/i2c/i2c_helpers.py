"""
Shared helpers for the I2C sensor HIL tests (test_i2c_api.py and
test_i2c_sensors.py).

Component definitions: load_sensor_types() maps each sensor type string
in a Wippersnapper_Components checkout's components/sensors.json to its
ws.sensor.Type in Wippersnapper_Protobuf's sensor.proto: the entry's
typeEnum, which must be a value in the enum; an entry without one
(sensors.json before typeEnum) maps to the value whose name it spells
(see _match_enum). load_component_definition() reads a component's
definition.json and checks its subcomponents against that map, and
subcomponent_types() keys an Add's types by each subcomponent's 0-based
position in the definition's subcomponents array:

    sensor_types = load_sensor_types("~/Wippersnapper_Components",
                                     "~/Wippersnapper_Protobuf")
    bme680 = load_component_definition("~/Wippersnapper_Components", "i2c",
                                       "bme680", sensor_types)
    subcomponent_types(bme680, ["ambient-temp", "humidity"])
    # {"0": "T_AMBIENT_TEMPERATURE", "2": "T_RELATIVE_HUMIDITY"}

The sensor_types and component_definition fixtures (i2c/conftest.py)
wrap the first two.

Devices and messages: a test gets an I2cDevice from device() (in a
fixture): the sensor's driver name, its address on the i2c_bus probe's
port, and its component definition. device.types() turns the
subcomponents to read into an Add's types map, and the helpers below send
Adds, Removes and Modifies built from it and check the events and errors
the DUT publishes.
"""

import json
import logging
import math
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest


log = logging.getLogger("hil.i2c")

# Read every PERIOD seconds, unless a test says otherwise
PERIOD = 10
# How long past the period to watch for events after a Remove
REMOVED_BUFFER = 5
# How long after checkin the DUT may take to handle a message, as the
# checkin fixture allows for the checkin itself.
CHECKIN_TIMEOUT = 30
# A new driver's previous read is 24 hours ago, so update() reads it on its
# next pass after the Add; allow for a slow conversion (e.g. a gas heater)
# and the publish.
FIRST_EVENT_TIMEOUT = 10
# How far an interval between events may be from the period, in seconds.
# update() restarts the period after the read and the publish, so an
# interval is the period plus the conversion, plus the loop's and the
# network's latency.
INTERVAL_EARLY = 0.5
INTERVAL_LATE = 2.0


# --- Component definitions ------------------------------------------------


class ComponentDefinitionError(Exception):
    pass


@dataclass(frozen=True)
class SensorTypes:
    """The sensor type strings in sensors.json, each mapped to its
    ws.sensor.Type name in sensor.proto."""
    sensors_path: Path
    proto_path: Path
    components_commit: str  # None if the checkout isn't a git repo
    protobuf_commit: str
    types: dict             # e.g. {"humidity": "T_RELATIVE_HUMIDITY", ...}
    matched: tuple          # the strings with no typeEnum, matched by name


@dataclass(frozen=True)
class ComponentDefinition:
    """A component's definition.json from a Wippersnapper_Components
    checkout, its subcomponents checked against sensors.json."""
    kind: str               # e.g. "i2c"
    name: str               # e.g. "bme680"
    path: Path
    commit: str             # None if the checkout isn't a git repo
    definition: dict
    sensor_types: dict      # SensorTypes.types


# Abbreviations in sensors.json's names, spelled out as sensor.proto does
_ABBREVIATIONS = {"temp": "temperature"}


def _commit(directory):
    try:
        return subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except OSError as e:
        raise ComponentDefinitionError(f"cannot read {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise ComponentDefinitionError(f"{path} is not valid JSON: {e}") from e


def _proto_enum(path):
    """The value names of sensor.proto's enum Type, in file order. Its
    comments hold braces ("{value}"), so the enum ends at the first "}" at
    the start of a line."""
    try:
        source = path.read_text()
    except OSError as e:
        raise ComponentDefinitionError(f"cannot read {path}: {e}") from e
    body = re.search(r"^enum Type\s*\{(.*?)^\}", source, re.S | re.M)
    if not body:
        raise ComponentDefinitionError(f"{path} has no enum Type")
    names = re.findall(r"^\s*(T_\w+)\s*=\s*\d+\s*;", body.group(1), re.M)
    if not names:
        raise ComponentDefinitionError(f"{path}: enum Type has no values")
    return names


def _match_enum(name, enum):
    """The enum value name the sensors.json name spells: the one whose
    lowercase tokens (less T_) are exactly the name's words, temp spelled
    out (pm10-std is T_PM10_STD, not T_PM100_STD); else the one holding
    all the words with the fewest others (humidity is
    T_RELATIVE_HUMIDITY). None if there's no such value, or a tie."""
    words = [_ABBREVIATIONS.get(w, w) for w in name.split("-")]
    tokens = {e: e[2:].lower().split("_") for e in enum}
    exact = [e for e, t in tokens.items() if t == words]
    if len(exact) == 1:
        return exact[0]
    holding = [e for e, t in tokens.items() if set(words) <= set(t)]
    if not holding:
        return None
    fewest = min(len(tokens[e]) for e in holding)
    best = [e for e in holding if len(tokens[e]) == fewest]
    return best[0] if len(best) == 1 else None


def load_sensor_types(components_dir, protobuf_dir):
    """Map each sensor type string in components_dir's
    components/sensors.json to its ws.sensor.Type name in protobuf_dir's
    proto/wippersnapper/sensor.proto: its entry's typeEnum, else the value
    it spells. Raises ComponentDefinitionError if a typeEnum isn't in the
    enum, a string without one matches no value or more than one, or two
    strings map to the same value."""
    components_dir = Path(components_dir).expanduser()
    protobuf_dir = Path(protobuf_dir).expanduser()
    sensors_path = components_dir / "components" / "sensors.json"
    proto_path = protobuf_dir / "proto" / "wippersnapper" / "sensor.proto"
    sensors = _read_json(sensors_path)
    if not isinstance(sensors, dict) or not sensors:
        raise ComponentDefinitionError(
            f"{sensors_path}: expected an object of sensor types")
    enum = _proto_enum(proto_path)
    types = {}
    matched = []
    for name, entry in sensors.items():
        value = entry.get("typeEnum") if isinstance(entry, dict) else None
        if value is not None:
            if value not in enum:
                raise ComponentDefinitionError(
                    f"{sensors_path}: sensor type {name!r} has typeEnum "
                    f"{value!r}, not a ws.sensor.Type in {proto_path}")
        else:
            value = _match_enum(name, enum)
            if value is None:
                raise ComponentDefinitionError(
                    f"{sensors_path}: sensor type {name!r} has no typeEnum "
                    f"and matches no single ws.sensor.Type in {proto_path}")
            matched.append(name)
        if value in types.values():
            other = next(n for n, v in types.items() if v == value)
            raise ComponentDefinitionError(
                f"{sensors_path}: sensor types {other!r} and {name!r} both "
                f"match {value} in {proto_path}")
        types[name] = value
    return SensorTypes(sensors_path, proto_path, _commit(components_dir),
                       _commit(protobuf_dir), types, tuple(matched))


def _sensor_type(subcomponent):
    """A subcomponent's sensor type string: the string itself, or an
    object's sensorType."""
    if isinstance(subcomponent, dict):
        return subcomponent.get("sensorType")
    return subcomponent


def load_component_definition(components_dir, kind, name, sensor_types):
    """Read components/<kind>/<name>/definition.json from the
    Wippersnapper_Components checkout in components_dir, checking each of
    its subcomponents is a sensor type in sensor_types (a SensorTypes)."""
    components_dir = Path(components_dir).expanduser()
    path = components_dir / "components" / kind / name / "definition.json"
    definition = _read_json(path)
    if not isinstance(definition, dict):
        raise ComponentDefinitionError(f"{path}: expected an object")
    subcomponents = definition.get("subcomponents")
    if not isinstance(subcomponents, list) or not subcomponents:
        raise ComponentDefinitionError(f"{path} has no subcomponents")
    for index, subcomponent in enumerate(subcomponents):
        kind_name = _sensor_type(subcomponent)
        if kind_name not in sensor_types.types:
            raise ComponentDefinitionError(
                f"{path}: subcomponent {index} ({subcomponent!r}) isn't a "
                f"sensor type in {sensor_types.sensors_path}")
    return ComponentDefinition(kind, name, path, _commit(components_dir),
                               definition, sensor_types.types)


def subcomponent_names(component):
    """The sensor type strings of component's subcomponents, in order."""
    return [_sensor_type(s) for s in component.definition["subcomponents"]]


def subcomponent_types(component, names=None):
    """The types map of an Add for component reading the subcomponents
    named (all of them if None): {str(index): ws.sensor.Type name}, index
    each one's 0-based position in the subcomponents array, in array order
    whatever the order of names. A name listed more than once in the array
    takes its first position."""
    all_names = subcomponent_names(component)
    wanted = all_names if names is None else list(names)
    missing = [n for n in wanted if n not in all_names]
    if missing:
        raise ComponentDefinitionError(
            f"{component.path}: no subcomponents {missing}, only {all_names}")
    indexes = sorted({all_names.index(n) for n in wanted})
    return {str(i): component.sensor_types[all_names[i]] for i in indexes}


# --- Devices and messages -------------------------------------------------


@dataclass(frozen=True)
class I2cDevice:
    """An I2C sensor wired to the i2c_bus probe's port."""
    name: str           # its driver's name in CreateI2cSensorDrv()
    address: int
    component: ComponentDefinition

    @property
    def subcomponents(self):
        """Its definition's subcomponents' sensor type strings, in order."""
        return subcomponent_names(self.component)

    def types(self, names=None):
        """The Add's types map for the subcomponents named (all if None),
        {str(index): ws.sensor.Type name}."""
        try:
            return subcomponent_types(self.component, names)
        except ComponentDefinitionError as e:
            pytest.fail(str(e), pytrace=False)

    def types_at(self, indexes):
        """The Add's types map for the subcomponents at indexes in the
        definition's subcomponents array. Skips the test if the definition
        has too few."""
        names = self.subcomponents
        if max(indexes) >= len(names):
            pytest.skip(f"{self.name} has {len(names)} subcomponents, the "
                        f"test needs index {max(indexes)}")
        return self.types([names[i] for i in indexes])


def device(i2c_devices, component_definition, name, address, kind="i2c"):
    """The I2cDevice for the sensor whose driver and component are name, at
    address. Fails unless the i2c_bus probe lists address, and the
    definition lists it among its i2cAddresses."""
    if address not in i2c_devices.addresses:
        pytest.fail(f"i2c_bus probe doesn't list 0x{address:02x}, the {name} "
                    "these tests add", pytrace=False)
    component = component_definition(kind, name)
    addresses = [int(a, 16)
                 for a in component.definition.get("i2cAddresses", [])]
    if address not in addresses:
        pytest.fail(f"{component.path} doesn't list 0x{address:02x} in "
                    "i2cAddresses", pytrace=False)
    return I2cDevice(name, address, component)


def descriptor(dev, address_space, address=None):
    """dev's i2c.Descriptor on address_space (at address, if given)."""
    return {"addressSpace": address_space,
            "address": dev.address if address is None else address}


def add_msg(dev, address_space, types, period=PERIOD, settings=None):
    """An i2c.Add for dev on address_space, reading every period the types
    map ({key: ws.sensor.Type}), with settings ({key: ws.config.Value}) if
    given."""
    add = {
        "descriptor": descriptor(dev, address_space),
        "name": dev.name,
        "period": period,
        "types": dict(types),
    }
    if settings:
        add["settings"] = {"settings": dict(settings)}
    return add


def expect_driver_initialized(serial, dev):
    """Expect Handle_Add() to create and initialize dev's driver."""
    serial.expect(r"^\[i2c\] Creating sensor driver\.\.\.$",
                  timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^\[i2c\] Driver initialized: ?$", timeout=5)
    serial.expect(rf"^{dev.name}$", timeout=5)


def add(serial, protomq, uid, dev, address_space, types, period=PERIOD,
        settings=None):
    """Send an i2c.Add for dev reading types every period, with settings,
    and expect the DUT to decode it and initialize the driver."""
    msg = add_msg(dev, address_space, types, period, settings)
    log.info("adding %s at 0x%02x with %s every %ss%s", dev.name, dev.address,
             types, period, f", settings {settings}" if settings else "")
    protomq.publish(uid, {"i2c": {"add": msg}})
    protomq.expect_b2d_message(uid, "i2c.add", match=msg, timeout=5)
    expect_driver_initialized(serial, dev)


def remove(protomq, uid, dev, address_space):
    """Send an i2c.Remove for dev. Handle_Remove() logs nothing when it
    succeeds; the caller checks for a component error."""
    msg = {"descriptor": descriptor(dev, address_space)}
    log.info("removing %s at 0x%02x", dev.name, dev.address)
    protomq.publish(uid, {"i2c": {"remove": msg}})
    protomq.expect_b2d_message(uid, "i2c.remove", match=msg, timeout=5)


def modify(serial, protomq, uid, dev, address_space, types, period=PERIOD):
    """Send an i2c.Modify that replaces dev with one reading types every
    period, and expect the DUT to remove it, then initialize the new
    driver."""
    msg = {"remove": {"descriptor": descriptor(dev, address_space)},
           "add": add_msg(dev, address_space, types, period)}
    log.info("modifying %s at 0x%02x to %s every %ss", dev.name, dev.address,
             types, period)
    protomq.publish(uid, {"i2c": {"modify": msg}})
    protomq.expect_b2d_message(uid, "i2c.modify", match=msg, timeout=5)
    expect_driver_initialized(serial, dev)
    # Handle_Modify() logs this before the Add if Handle_Remove() failed
    serial.assert_none(r"^\[i2c\] WARNING: Remove failed in modify")


def _check_event(dev, address_space, event, types, value_range):
    """Check an i2c.Event for dev (see expect_event()); returns its
    {type: value}."""
    got_descriptor = event.get("descriptor", {})
    # Exact, not ProtoMQ's partial match: the DUT sends the bus's pins, and
    # no mux fields for a device on the bus
    assert got_descriptor == descriptor(dev, address_space), (
        f"expected descriptor {address_space} at 0x{dev.address:02x}, got "
        f"{got_descriptor}: {event}")
    events = event.get("events", {})
    expected = dict(sorted(types.items(), key=lambda item: int(item[0])))
    got = {key: e.get("type") for key, e in
           sorted(events.items(), key=lambda item: int(item[0]))}
    assert list(got.items()) == list(expected.items()), (
        f"expected sensor events {expected}, got {got}: {event}")
    values = {}
    for sensor_event in events.values():
        kind = sensor_event["type"]
        value = sensor_event.get("floatValue")
        assert value is not None, f"expected a floatValue for {kind}: {event}"
        assert math.isfinite(value), f"{kind} is {value}: {event}"
        if value_range and kind in value_range:
            low, high = value_range[kind]
            assert low <= value <= high, (
                f"expected {kind} {low}-{high}, got {value}: {event}")
        values[kind] = value
    return values


def expect_event(protomq, uid, dev, address_space, timeout, types,
                 value_range=None):
    """Expect the broker to get an i2c.Event for dev on address_space,
    holding a sensor event per entry in the Add's types map
    ({key: ws.sensor.Type}), under the same key, each a finite floatValue
    (and within value_range[type], (low, high), where given). Returns when
    the broker received it, in seconds since the epoch, and {type: value}.

    ProtoMQ decodes the events map into a JSON object, whose integer keys
    come out in ascending order whatever their order on the wire, so the
    order checked is that of the keys."""
    event = protomq.expect_d2b_message(
        uid, "i2c.event", match={"descriptor": {"address": dev.address}},
        timeout=timeout)
    values = _check_event(dev, address_space, event, types, value_range)
    received = protomq.last_d2b_time(uid)
    log.info("0x%02x: %s at %s", dev.address, values, received)
    return received, values


def _event_types(event):
    return {key: e.get("type") for key, e in event.get("events", {}).items()}


def collect_full_events(protomq, uid, sensors, address_space, mark, timeout,
                        interval=0.5):
    """Wait up to timeout for an i2c.Event since mark (from d2b_mark()) from
    each of several devices added at once, holding every type in its Add,
    and check it as expect_event() does. sensors is a list of (I2cDevice,
    types, value_range). Returns ({address: {type: value}} for the devices
    whose event passed, {address: why} for the rest), so one bad device
    doesn't hide the others' results.

    Reads every event since mark, not expect_d2b_message()'s cursor, which
    is shared by all i2c.events: waiting on one device would skip past the
    others'. Events holding only some of a device's types are skipped: a
    driver whose metrics warm up at different rates publishes the ones that
    read (update()'s "Failed to read sensor key=" for the rest)."""
    by_address = {dev.address: (dev, types, value_range)
                  for dev, types, value_range in sensors}
    found, failed, partial = {}, {}, {}
    deadline = time.monotonic() + timeout
    while True:
        for event in protomq.d2b_since(uid, "i2c.event", mark):
            address = event.get("descriptor", {}).get("address")
            if (address in found or address in failed
                    or address not in by_address):
                continue
            dev, types, value_range = by_address[address]
            got = _event_types(event)
            if got != dict(types) and got.items() <= dict(types).items():
                partial[address] = got  # still warming up
                continue
            try:
                found[address] = _check_event(dev, address_space, event,
                                              types, value_range)
                log.info("%s at 0x%02x: %s", dev.name, address,
                         found[address])
            except AssertionError as e:
                failed[address] = str(e)
        if len(found) + len(failed) == len(by_address):
            return found, failed
        if time.monotonic() >= deadline:
            for address in by_address.keys() - found.keys() - failed.keys():
                failed[address] = (
                    f"no event with every type within {timeout}s"
                    + (f" (last partial: {partial[address]})"
                       if address in partial else " (no event at all)"))
            return found, failed
        time.sleep(interval)


def assert_period(times, period=PERIOD):
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


def expect_component_error(protomq, uid, message, address=None):
    """Expect the DUT to publish an error.component with message, for the
    i2c device at address if given (the empty descriptor's address is
    left out of the JSON, so None doesn't check it)."""
    match = {"message": message}
    if address is not None:
        match["i2c"] = {"address": address}
    return protomq.expect_d2b_message(uid, "error.component", match=match,
                                      timeout=CHECKIN_TIMEOUT)


def assert_no_events(protomq, uid, within=PERIOD + REMOVED_BUFFER):
    """Expect no i2c.event for within seconds."""
    protomq.assert_no_d2b_message(uid, "i2c.event", within=within)


def assert_no_component_error(protomq, uid, mark):
    """Expect no error.component from the DUT since mark."""
    errors = protomq.d2b_since(uid, "error.component", mark)
    assert not errors, f"expected no error.component, got {errors}"
