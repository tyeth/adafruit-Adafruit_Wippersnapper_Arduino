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


def test_good_secrets_report_io_user(board, firmware, valid_secrets, broker_creds,
                                     flashed_board, serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The valid secrets for this scenario, from the $HIL_* variables.
    expected_user = broker_creds.username
    assert expected_user

    # 2. The whole chip was erased, and a LittleFS image holding those
    #    secrets was built and flashed with the firmware. (Compared outside
    #    the assert so a failure doesn't print the credentials.)
    matches = json.loads(flashed_board.secrets_path.read_text()) == valid_secrets
    assert matches
    fs_partition = hil_board_utils.esp_get_fs_partition(firmware.partitions)
    assert flashed_board.build.partition == fs_partition
    assert f"/{hil_board_utils.SECRETS_FILENAME}" in flashed_board.build.listing
    assert flashed_board.flash.output.count("Hash of data verified") == 2

    # 3. A serial monitor is attached to the board's port.
    assert serial.port == board.port

    # --- Act ----------------------------------------------------------
    # 4. Power-cycle the board's hub port and capture the boot once its
    #    serial port is back.
    serial.port_reset(board.entry, timeout=15, **power)

    # --- Assert -------------------------------------------------------
    # 5. The firmware reports the user from secrets.json.
    serial.expect(rf"^Adafruit\.io User: {re.escape(expected_user)}$",
                  timeout=30)


@pytest.mark.skip(reason="optional instrumentation: step-timed variant of "
                         "test_good_secrets_report_io_user")
def test_good_secrets_report_io_user_timed(board, firmware, valid_secrets,
                                           broker_creds, flashed_board, serial,
                                           power):
    timer = StepTimer()
    try:
        # --- Arrange ------------------------------------------------------
        # 1. The valid secrets for this scenario, from the $HIL_* variables.
        with timer.step("(1) load secrets"):
            expected_user = broker_creds.username
            assert expected_user

        # 2a. The whole chip is erased,
        # 2b. a LittleFS image holding those secrets is built, and
        # 2c. flashed with the firmware (all by flashed_board).
        timer.record("(2a) erase chip", flashed_board.erase_s)
        timer.record("(2b) build fs image", flashed_board.build_s)
        timer.record("(2c) flash firmware + fs", flashed_board.flash_s)
        # (Compared outside the assert so a failure doesn't print the
        # credentials.)
        matches = json.loads(flashed_board.secrets_path.read_text()) == valid_secrets
        assert matches
        fs_partition = hil_board_utils.esp_get_fs_partition(firmware.partitions)
        assert flashed_board.build.partition == fs_partition
        assert f"/{hil_board_utils.SECRETS_FILENAME}" in flashed_board.build.listing
        assert flashed_board.flash.output.count("Hash of data verified") == 2

        # 3. A serial monitor is attached to the board's port.
        with timer.step("(3) serial attached"):
            assert serial.port == board.port

        # --- Act ----------------------------------------------------------
        # 4. Power-cycle the board's hub port and capture the boot once its
        #    serial port is back.
        with timer.step("(4) power-cycle reset"):
            serial.port_reset(board.entry, timeout=15, **power)

        # --- Assert -------------------------------------------------------
        # 5. The firmware reports the user from secrets.json.
        with timer.step("(5) expect io user"):
            serial.expect(rf"^Adafruit\.io User: {re.escape(expected_user)}$",
                          timeout=30)
    finally:
        timer.report()


@pytest.mark.parametrize("secrets_name", ["default"], indirect=True)
def test_default_secrets(board, default_secrets, flashed_board, serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and the
    #    placeholder secrets.
    assert json.loads(flashed_board.secrets_path.read_text()) == default_secrets

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the board's hub port and capture the boot.
    serial.port_reset(board.entry, timeout=15, **power)

    # --- Assert -------------------------------------------------------
    # 3. The firmware rejects the placeholder IO credentials and halts.
    serial.expect(
        r"^ERROR: Invalid IO credentials in secrets\.json! TO FIX: Please change "
        r"io_username and io_key to match your Adafruit IO credentials!$",
        timeout=30)


@pytest.mark.parametrize("secrets_name", ["no_fs_image"], indirect=True)
def test_no_fs_image(board, flashed_board, serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware only, so the
    #    fs partition is still erased.
    assert flashed_board.build is None
    assert flashed_board.flash.output.count("Hash of data verified") == 1

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the board's hub port and capture the boot.
    serial.port_reset(board.entry, timeout=15, **power)

    # --- Assert -------------------------------------------------------
    # 3. The firmware can't mount LittleFS and halts, and is still halted
    #    on the next repeat.
    for _ in range(2):
        serial.expect(r"^Fatal Error: Halted execution!$", timeout=30)
        serial.expect(r"^ERROR: Failure initializing LittleFS!$", timeout=5)


@pytest.mark.parametrize("secrets_name", ["no_secrets_file"], indirect=True)
def test_no_secrets_file(board, flashed_board, serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and an empty
    #    LittleFS image.
    assert flashed_board.secrets_path is None
    assert (f"/{hil_board_utils.SECRETS_FILENAME}"
            not in flashed_board.build.listing)
    assert flashed_board.flash.output.count("Hash of data verified") == 2

    # --- Act ----------------------------------------------------------
    # 2. Power-cycle the board's hub port and capture the boot.
    serial.port_reset(board.entry, timeout=15, **power)

    # --- Assert -------------------------------------------------------
    # 3. The firmware mounts LittleFS, finds no secrets.json and halts,
    #    and is still halted on the next repeat.
    for _ in range(2):
        serial.expect(r"^Fatal Error: Halted execution!$", timeout=30)
        serial.expect(
            r"^ERROR: No secrets\.json found on filesystem - did you upload "
            r"credentials\?$",
            timeout=5)
    serial.assert_none(r"Failure initializing LittleFS")


@pytest.mark.parametrize("secrets_name", ["no_network_type_wifi"], indirect=True)
def test_secrets_structure_no_network_type_wifi(board, generated_secrets,
                                                secrets_name, flashed_board,
                                                serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and secrets
    #    without network_type_wifi.
    assert (json.loads(flashed_board.secrets_path.read_text())
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
                                                      secrets_name, flashed_board,
                                                      serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and secrets
    #    with an empty alternative_networks list.
    assert (json.loads(flashed_board.secrets_path.read_text())
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
                                                       secrets_name, flashed_board,
                                                       serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and secrets
    #    whose alternative_networks is a string.
    assert (json.loads(flashed_board.secrets_path.read_text())
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
                                                       secrets_name, flashed_board,
                                                       serial, power):
    # --- Arrange ------------------------------------------------------
    # 1. The chip was erased and flashed with the firmware and secrets
    #    whose alternative_networks is an object.
    assert (json.loads(flashed_board.secrets_path.read_text())
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
