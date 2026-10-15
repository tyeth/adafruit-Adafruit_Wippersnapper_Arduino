"""Provisioning: the firmware reads secrets.json from LittleFS at boot."""

import re
import time
from contextlib import contextmanager

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
