"""
The I2C sensors wired to the HIL DUT, shared by the i2c tests
(test_i2c_sensors.py, test_i2c_settings.py): each one's component name
(driver and Wippersnapper_Components definition), address, plausible
bench readings, how long its first full event may take after the Add,
and whether it reads at once or warms up for minutes after power-on.
"""

from i2c_helpers import FIRST_EVENT_TIMEOUT, PERIOD


# Whether a sensor reads at once, or warms up for minutes after power-on
FAST, SLOW = "fast", "slow"

# {driver/component name: (address, plausible bench readings per
# ws.sensor.Type, how long its first full event may take, warm-up)}
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
    }, 60 + 2 * PERIOD + 15, SLOW),
}
FAST_SENSORS = [n for n, spec in SENSORS.items() if spec[3] == FAST]

# The I2C sensors wired to a mux channel, not the bus, in the same form as
# SENSORS. Their channel comes from the i2c_bus probe's mux in
# hil-boards.json. Kept apart from SENSORS, whose tests add every sensor on
# the bus.
MUX_SENSORS = {
    "aht20": (0x38, {
        "T_AMBIENT_TEMPERATURE": (5.0, 50.0),
        "T_AMBIENT_TEMPERATURE_FAHRENHEIT": (41.0, 122.0),
        "T_RELATIVE_HUMIDITY": (1.0, 99.0),
    }, FIRST_EVENT_TIMEOUT, FAST),
}
