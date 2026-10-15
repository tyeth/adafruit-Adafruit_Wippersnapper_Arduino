"""Provisioning: the firmware reads secrets.json from LittleFS at boot."""

import json
import re
import time
from contextlib import contextmanager

import pytest

import hil_board_utils


class StepTimer:
    """Times numbered test steps and prints them as a table. Steps done by
    fixtures before the test body are added with record()."""

    def __init__(self):
        self.steps = []

    def record(self, label, seconds):
        self.steps.append((label, seconds))

    @contextmanager
    def step(self, label):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(label, time.perf_counter() - start)

    def report(self):
        width = max(len(label) for label, _ in self.steps)
        lines = [f"  {label:<{width}}  {seconds:8.3f}s" for label, seconds in self.steps]
        total = sum(seconds for _, seconds in self.steps)
        lines.append(f"  {'total':<{width}}  {total:8.3f}s")
        print("\nstep timings:\n" + "\n".join(lines))


def test_good_secrets_report_io_user(board, firmware, secrets_file, broker_creds,
                                     flashed_firmware, flashed_fs, serial, power):
    timer = StepTimer()
    try:
        # --- Arrange ------------------------------------------------------
        # 1. Load the known-good secrets for this scenario.
        with timer.step("(1) load secrets"):
            secrets_path = secrets_file("good")
            expected_user = broker_creds.username
            assert expected_user

        # 2a. A LittleFS image holding those secrets is built, and
        # 2b. flashed with the firmware (once per session, by flashed_firmware).
        timer.record("(2a) build fs image", flashed_firmware.build_s)
        timer.record("(2b) flash firmware + fs", flashed_firmware.flash_s)
        fs_partition = hil_board_utils.esp_get_fs_partition(firmware.partitions)
        assert flashed_firmware.build.partition == fs_partition
        assert f"/{hil_board_utils.SECRETS_FILENAME}" in flashed_firmware.build.listing
        assert flashed_firmware.flash.output.count("Hash of data verified") == 2

        # 3. The filesystem on the board holds this module's secrets
        #    (flashed_fs reflashes it only if a previous module changed it).
        if flashed_fs.flashed:
            timer.record("(3a) rebuild fs image", flashed_fs.build_s)
            timer.record("(3b) reflash fs", flashed_fs.flash_s)
        assert flashed_fs.secrets_path == secrets_path
        assert flashed_firmware.fs_digest == flashed_fs.digest

        # 4. A serial monitor is attached to the board's port.
        with timer.step("(4) serial attached"):
            assert serial.port == board.port

        # --- Act ----------------------------------------------------------
        # 5. Power-cycle the board's hub port and capture the boot once its
        #    serial port is back.
        with timer.step("(5) power-cycle reset"):
            serial.port_reset(board.entry, timeout=15, **power)

        # --- Assert -------------------------------------------------------
        # 6. The firmware reports the user from secrets.json.
        with timer.step("(6) expect io user"):
            serial.expect(rf"^Adafruit\.io User: {re.escape(expected_user)}$",
                          timeout=30)
    finally:
        timer.report()


@pytest.mark.parametrize("secrets_name", ["default"], indirect=True)
def test_default_secrets(board, default_secrets, flashed_fs, serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The board's filesystem holds the placeholder secrets.
    assert json.loads(flashed_fs.secrets_path.read_text()) == default_secrets

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the board's hub port and capture the boot.
    serial.port_reset(board.entry, timeout=15, **power)

    # --- Assert -------------------------------------------------------
    # 3. The firmware rejects the placeholder IO credentials and halts.
    serial.expect(
        r"^ERROR: Invalid IO credentials in secrets\.json! TO FIX: Please change "
        r"io_username and io_key to match your Adafruit IO credentials!$",
        timeout=30)


@pytest.mark.parametrize("secrets_name", ["no_network_type_wifi"], indirect=True)
def test_secrets_structure_no_network_type_wifi(board, generated_secrets,
                                                secrets_name, flashed_fs, serial,
                                                power):
    # --- Arrange ------------------------------------------------------
    # 1. The board's filesystem holds secrets without network_type_wifi.
    assert (json.loads(flashed_fs.secrets_path.read_text())
            == generated_secrets[secrets_name])

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the board's hub port and capture the boot.
    serial.port_reset(board.entry, timeout=15, **power)

    # --- Assert -------------------------------------------------------
    # 3. The firmware halts with the structure error, and is still halted
    #    on the next repeat.
    for _ in range(2):
        serial.expect(r"^Fatal Error: Halted execution!$", timeout=30)
        serial.expect(
            r"^ERROR: Could not find network_type_wifi in secrets\.json!$",
            timeout=5)
    # 4. It halted before checking the (placeholder) credentials.
    serial.assert_none(r"Invalid (IO|network) credentials")


@pytest.mark.parametrize("secrets_name", ["empty_alternative_networks"],
                         indirect=True)
def test_secrets_structure_empty_alternative_networks(board, generated_secrets,
                                                      secrets_name, flashed_fs,
                                                      serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The board's filesystem holds secrets with an empty
    #    alternative_networks list.
    assert (json.loads(flashed_fs.secrets_path.read_text())
            == generated_secrets[secrets_name])

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the board's hub port and capture the boot.
    serial.port_reset(board.entry, timeout=15, **power)

    # --- Assert -------------------------------------------------------
    # 3. The firmware halts with the structure error, and is still halted
    #    on the next repeat.
    for _ in range(2):
        serial.expect(r"^Fatal Error: Halted execution!$", timeout=30)
        serial.expect(
            r"^ERROR: No alternative network entries found under "
            r"network_type_wifi\.alternative_networks in secrets\.json!$",
            timeout=5)
    # 4. It halted before checking the (placeholder) credentials.
    serial.assert_none(r"Invalid (IO|network) credentials")


@pytest.mark.parametrize("secrets_name", ["string_alternative_networks"],
                         indirect=True)
def test_secrets_structure_string_alternative_networks(board, generated_secrets,
                                                       secrets_name, flashed_fs,
                                                       serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The board's filesystem holds secrets whose alternative_networks
    #    is a string.
    assert (json.loads(flashed_fs.secrets_path.read_text())
            == generated_secrets[secrets_name])

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the board's hub port and capture the boot.
    serial.port_reset(board.entry, timeout=15, **power)

    # --- Assert -------------------------------------------------------
    # 3. The firmware halts with the structure error, and is still halted
    #    on the next repeat.
    for _ in range(2):
        serial.expect(r"^Fatal Error: Halted execution!$", timeout=30)
        serial.expect(
            r"^ERROR: Unrecognised value type for "
            r"network_type_wifi\.alternative_networks in secrets\.json!$",
            timeout=5)
    # 4. It halted before checking the (placeholder) credentials.
    serial.assert_none(r"Invalid (IO|network) credentials")


@pytest.mark.parametrize("secrets_name", ["object_alternative_networks"],
                         indirect=True)
def test_secrets_structure_object_alternative_networks(board, generated_secrets,
                                                       secrets_name, flashed_fs,
                                                       serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The board's filesystem holds secrets whose alternative_networks
    #    is an object.
    assert (json.loads(flashed_fs.secrets_path.read_text())
            == generated_secrets[secrets_name])

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the board's hub port and capture the boot.
    serial.port_reset(board.entry, timeout=15, **power)

    # --- Assert -------------------------------------------------------
    # 3. The firmware halts with the structure error, and is still halted
    #    on the next repeat.
    for _ in range(2):
        serial.expect(r"^Fatal Error: Halted execution!$", timeout=30)
        serial.expect(
            r"^ERROR: Unrecognised value type for "
            r"network_type_wifi\.alternative_networks in secrets\.json!$",
            timeout=5)
    # 4. It halted before checking the (placeholder) credentials.
    serial.assert_none(r"Invalid (IO|network) credentials")
