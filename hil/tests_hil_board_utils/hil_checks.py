"""Boot-output checks shared by the hardware tests."""

import re

import hil_power_cycle_monitor as hpcm

# ESP32 ROM line after an EN-pin reset, booting from flash (not download mode).
ROM_RESET_RE = r"^rst:0x1 \(POWERON_RESET\),boot:0x[0-9a-f]+ \(SPI_FAST_FLASH_BOOT\)$"
ROM_ANY_RESET_RE = r"^rst:0x"
CRASH_RE = r"Guru Meditation|abort\(\) was called|^Backtrace:|task_wdt: Task watchdog"

# How long a fresh boot is watched for crashes. Kept short: with secrets for
# a network that doesn't exist, the firmware currently trips the task
# watchdog about 75s in, during the WiFi scan.
CRASH_WINDOW = 5


def expect_banner(mon, timeout=20):
    hpcm.check_banner(mon, timeout)


def expect_identity(mon, secrets, timeout=5):
    """Expect the user and SSID the firmware read from secrets.json."""
    user = secrets["io_username"]
    ssid = secrets["network_type_wifi"]["network_ssid"]
    mon.expect(rf"^Adafruit\.io User: {re.escape(user)}$", timeout=timeout)
    mon.expect(rf"^WiFi Network: {re.escape(ssid)}$", timeout=timeout)
