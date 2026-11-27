"""Fixtures for the I2C sensor HIL tests: component definitions from a
Wippersnapper_Components checkout, checked against Wippersnapper_Protobuf's
sensor.proto (see i2c_helpers.py).

    HIL_WS_COMPONENTS_DIR    optional, the Wippersnapper_Components
                             checkout (main), default
                             ~/Wippersnapper_Components
    HIL_WS_PROTOBUF_DIR      optional, the Wippersnapper_Protobuf checkout
                             (api-v2), default ~/Wippersnapper_Protobuf
"""

import logging
import os

import pytest

import i2c_helpers


DEFAULT_WS_COMPONENTS_DIR = "~/Wippersnapper_Components"
DEFAULT_WS_PROTOBUF_DIR = "~/Wippersnapper_Protobuf"

log = logging.getLogger("hil.firmware")


def _env_dir(name, default):
    """The directory in the environment variable name, or default if it's
    unset or empty."""
    value = os.environ.get(name)
    return value if value and value.strip() else default


def _components_dir():
    return _env_dir("HIL_WS_COMPONENTS_DIR", DEFAULT_WS_COMPONENTS_DIR)


@pytest.fixture(scope="session")
def sensor_types():
    """The sensor type strings a component's subcomponents may use, the
    keys of components/sensors.json in the Wippersnapper_Components
    checkout at $HIL_WS_COMPONENTS_DIR, each mapped to its ws.sensor.Type
    in proto/wippersnapper/sensor.proto in the Wippersnapper_Protobuf
    checkout at $HIL_WS_PROTOBUF_DIR (an i2c_helpers.SensorTypes)."""
    protobuf_dir = _env_dir("HIL_WS_PROTOBUF_DIR", DEFAULT_WS_PROTOBUF_DIR)
    try:
        types = i2c_helpers.load_sensor_types(_components_dir(),
                                                 protobuf_dir)
    except i2c_helpers.ComponentDefinitionError as e:
        pytest.fail(str(e), pytrace=False)
    log.info("sensor types: %s at Wippersnapper_Components %s, %s at "
             "Wippersnapper_Protobuf %s", types.sensors_path,
             types.components_commit, types.proto_path, types.protobuf_commit)
    if types.matched:
        log.info("sensor types without a typeEnum, matched by name: %s",
                 ", ".join(f"{n}={types.types[n]}" for n in types.matched))
    return types


@pytest.fixture(scope="session")
def component_definition(sensor_types):
    """A function (kind, name) -> the component's definition.json,
    components/<kind>/<name>/definition.json in the Wippersnapper_Components
    checkout at $HIL_WS_COMPONENTS_DIR, its subcomponents checked against
    sensor_types (an i2c_helpers.ComponentDefinition)."""
    def load(kind, name):
        try:
            component = i2c_helpers.load_component_definition(
                _components_dir(), kind, name, sensor_types)
        except i2c_helpers.ComponentDefinitionError as e:
            pytest.fail(str(e), pytrace=False)
        log.info("component definition: %s at Wippersnapper_Components %s",
                 component.path, component.commit)
        return component
    return load
