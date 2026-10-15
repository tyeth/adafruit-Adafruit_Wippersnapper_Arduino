"""Filesystem image generation (partitions.bin -> LittleFS with secrets.json)
and flashing it, with and without the firmware, using esptool."""

import json
import subprocess

import pytest

import hil_board_utils as hbu
from hil_checks import ROM_RESET_RE, expect_banner, expect_identity

HASH_VERIFIED = "Hash of data verified"


# --- Generation (no board needed) -----------------------------------------


def test_fs_partition_from_firmware(firmware):
    part = hbu.esp_get_fs_partition(firmware.partitions)
    assert part.type == hbu.ESP_TYPE_DATA
    assert part.subtype in hbu.ESP_FS_SUBTYPES
    assert part.offset % hbu.ESP_BLOCK_SIZE == 0
    assert part.size % hbu.ESP_BLOCK_SIZE == 0
    assert hbu.esp_get_fs_offset(firmware.partitions) == part.offset


def test_combined_image_matches_chip(firmware, chip):
    hbu.esp_check_combined_image(firmware.combined, chip)
    other = "esp32s3" if chip != "esp32s3" else "esp32"
    with pytest.raises(hbu.EspFlashError):
        hbu.esp_check_combined_image(firmware.combined, other)


def test_make_littlefs_packs_secrets(firmware, make_secrets, tmp_path):
    secrets_path, secrets = make_secrets()
    hbu.load_secrets(secrets_path)
    image = tmp_path / "littlefs.bin"

    result = hbu.esp_make_littlefs(firmware.partitions, image, secrets_path)

    assert result.output == image
    assert result.partition == hbu.esp_get_fs_partition(firmware.partitions)
    assert image.stat().st_size == result.partition.size
    assert f"/{hbu.SECRETS_FILENAME}" in result.listing
    hbu.esp_check_flash_layout(firmware.combined, image, result.partition)

    # Unpack the image and compare secrets.json byte for byte.
    unpacked = tmp_path / "unpacked"
    subprocess.run(
        [hbu.find_mklittlefs(), "-u", str(unpacked),
         *hbu.esp_geometry_args(result.partition.size), str(image)],
        check=True, capture_output=True,
    )
    packed = unpacked / hbu.SECRETS_FILENAME
    assert packed.read_bytes() == secrets_path.read_bytes()
    assert json.loads(packed.read_text()) == secrets


def test_invalid_secrets_are_rejected(make_secrets):
    path, _ = make_secrets(status_pixel_brightness=2)
    with pytest.raises(hbu.SecretsError, match="status_pixel_brightness"):
        hbu.load_secrets(path)


def test_esptool_found():
    tool = hbu.esp_find_esptool()
    assert tool.version
    assert tool.write_flash_command in ("write-flash", "write_flash")
    assert tool.erase_flash_command in ("erase-flash", "erase_flash")


# --- Flashing (board needed) ----------------------------------------------


def boot_and_check(open_monitor, port, secrets, banner=False):
    mon = open_monitor(port)
    mon.board_reset()
    mon.expect(ROM_RESET_RE, timeout=5)
    if banner:
        expect_banner(mon)
    expect_identity(mon, secrets, timeout=20)


@pytest.mark.hardware
def test_flash_fs_only(firmware, chip, port, make_secrets, open_monitor, tmp_path):
    secrets_path, secrets = make_secrets()
    image = tmp_path / "littlefs.bin"
    build = hbu.esp_make_littlefs(firmware.partitions, image, secrets_path)

    result = hbu.esp_flash_fs(port, chip, firmware.partitions, image)

    assert result.partition == build.partition
    assert result.command[-2:] == [f"{build.partition.offset:#x}", str(image)]
    assert result.output.count(HASH_VERIFIED) == 1
    # The firmware is untouched and now reads the new secrets.
    boot_and_check(open_monitor, port, secrets)


@pytest.mark.hardware
def test_flash_firmware_and_fs(firmware, chip, port, make_secrets, open_monitor,
                               tmp_path):
    secrets_path, secrets = make_secrets()
    image = tmp_path / "littlefs.bin"
    build = hbu.esp_make_littlefs(firmware.partitions, image, secrets_path)

    result = hbu.esp_flash(port, chip, firmware.partitions, firmware.combined, image)

    assert result.command[-4:] == [
        f"{hbu.ESP_COMBINED_OFFSET:#x}", str(firmware.combined),
        f"{build.partition.offset:#x}", str(image),
    ]
    assert result.output.count(HASH_VERIFIED) == 2
    boot_and_check(open_monitor, port, secrets, banner=True)


@pytest.mark.hardware
def test_erase_then_flash(firmware, chip, port, make_secrets, open_monitor,
                          tmp_path):
    erase = hbu.esp_erase_flash(port, chip)

    assert erase.command[-1] == hbu.esp_find_esptool().erase_flash_command
    # With the bootloader gone, the ROM finds no image to boot.
    mon = open_monitor(port)
    mon.board_reset()
    mon.expect(r"invalid header: 0xffffffff", timeout=5)
    mon.close()

    # Flashing the firmware and fs brings it back.
    secrets_path, secrets = make_secrets()
    image = tmp_path / "littlefs.bin"
    hbu.esp_make_littlefs(firmware.partitions, image, secrets_path)
    result = hbu.esp_flash(port, chip, firmware.partitions, firmware.combined, image)
    assert result.output.count(HASH_VERIFIED) == 2
    boot_and_check(open_monitor, port, secrets, banner=True)


@pytest.mark.hardware
def test_flash_refuses_open_port(firmware, chip, port, make_secrets, open_monitor,
                                 tmp_path):
    """The SerialMonitor holds the port exclusively, so esptool fails cleanly."""
    secrets_path, _ = make_secrets()
    image = tmp_path / "littlefs.bin"
    hbu.esp_make_littlefs(firmware.partitions, image, secrets_path)
    open_monitor(port)
    with pytest.raises(hbu.EsptoolError):
        hbu.esp_flash_fs(port, chip, firmware.partitions, image)
