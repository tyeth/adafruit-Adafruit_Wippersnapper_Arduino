"""The whole flow, like hil_power_cycle_monitor.py plus provisioning: build a
LittleFS image with secrets.json, flash it with the firmware, power-cycle the
board, find its port again, and check it boots with those secrets."""

import pytest

import hil_board_utils as hbu
from hil_checks import (CRASH_RE, CRASH_WINDOW, ROM_RESET_RE, expect_banner,
                        expect_identity)


@pytest.mark.hardware
def test_provision_flash_power_cycle_boot(pytestconfig, board_entry, firmware, chip,
                                          power, make_secrets, open_monitor, tmp_path):
    # 1. secrets.json: a real one if given, else generated with unique values
    #    so the boot output proves this image was the one flashed.
    secrets_path = pytestconfig.getoption("secrets")
    if secrets_path:
        secrets = hbu.load_secrets(secrets_path)
    else:
        secrets_path, secrets = make_secrets()
        hbu.load_secrets(secrets_path)

    # 2. LittleFS image sized to the firmware's fs partition.
    image = tmp_path / "littlefs.bin"
    build = hbu.esp_make_littlefs(firmware.partitions, image, secrets_path)
    print(f"built {image}: partition '{build.partition.label}' at "
          f"{build.partition.offset:#x}, {build.partition.size:#x} bytes")

    # 3. Flash firmware at 0x0 and the image at the partition offset.
    port = hbu.find_port(board_entry, refresh=True)
    flash = hbu.esp_flash(port, chip, firmware.partitions, firmware.combined, image)
    assert flash.partition == build.partition
    print(f"flashed {port}: {' '.join(flash.command)}")

    # 4. Power-cycle and wait for the port to come back.
    hbu.power_cycle_board(board_entry, **power)
    port = hbu.wait_for_port(board_entry)
    print(f"power-cycled; board back at {port}")

    # 5. Reset once attached to capture the whole boot, then check it.
    mon = open_monitor(port)
    mon.reset()
    mon.expect(ROM_RESET_RE, timeout=5)
    expect_banner(mon)
    expect_identity(mon, secrets)
    mon.assert_none(CRASH_RE, duration=CRASH_WINDOW)
