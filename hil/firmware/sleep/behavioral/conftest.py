"""Fixtures for the sleep behavioral tests. The I2C sleep tests add an I2C
sensor at checkin, so they reuse the i2c tests' helpers (i2c/i2c_helpers.py,
i2c/hil_sensors.py) and fixtures (sensor_types, component_definition, from
i2c/conftest.py), which pytest only gives to tests under i2c/. As in
checkin/conftest.py.
"""

import importlib.util
import sys
from pathlib import Path

I2C_DIR = Path(__file__).resolve().parents[2] / "i2c"
if str(I2C_DIR) not in sys.path:
    sys.path.insert(0, str(I2C_DIR))

# i2c/conftest.py under its own name, so it doesn't clash with the
# conftest modules pytest loads
_spec = importlib.util.spec_from_file_location("i2c_conftest",
                                               I2C_DIR / "conftest.py")
_i2c_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_i2c_conftest)

sensor_types = _i2c_conftest.sensor_types
component_definition = _i2c_conftest.component_definition
