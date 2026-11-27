"""SerialMonitor against the real board: attaching, reset through the
auto-reset circuit, matching the boot output, and losing the port on a power
cycle. The pty-only behaviour is covered by tests/test_serial_monitor.py."""

import re

import pytest

import hil_board_utils as hbu
from hil_checks import (CRASH_RE, CRASH_WINDOW, ROM_ANY_RESET_RE, ROM_RESET_RE,
                        expect_banner)

pytestmark = pytest.mark.hardware


def test_reset_captures_whole_boot(port, open_monitor):
    mon = open_monitor(port)
    boot = mon.board_reset()
    assert boot == mon.boot

    mon.expect(r"^ets \w+ +\d+ \d+ [\d:]+$", timeout=5)
    mon.expect(ROM_RESET_RE, timeout=1)
    expect_banner(mon)
    mon.assert_none(CRASH_RE, duration=CRASH_WINDOW)
    # Nothing from before the reset leaked into this boot.
    first = mon.lines(boot)[:3]
    assert any(l.text.startswith("ets ") for l in first), first


def test_second_reset_starts_a_new_boot(port, open_monitor):
    mon = open_monitor(port)
    first = mon.board_reset()
    expect_banner(mon)
    second = mon.board_reset()
    assert second > first

    expect_banner(mon)
    resets = [l for l in mon.lines(second) if re.search(ROM_ANY_RESET_RE, l.text)]
    assert len(resets) == 1, resets
    # assert_none defaults to the latest boot, so the first boot's ROM line
    # only matches when asked to look back that far.
    with pytest.raises(hbu.SerialUnexpectedError):
        mon.assert_none(ROM_ANY_RESET_RE, since=first)


def test_open_does_not_reset(port, open_monitor):
    mon = open_monitor(port)
    mon.board_reset()
    expect_banner(mon)
    mon.close()

    mon = open_monitor(port)
    mon.assert_none(ROM_ANY_RESET_RE, since=0, duration=3)


def test_timestamps_and_dump(port, open_monitor, tmp_path):
    mon = open_monitor(port)
    boot = mon.board_reset()
    expect_banner(mon)
    mon.close()

    lines = mon.lines(boot)
    times = [l.time for l in lines]
    assert times == sorted(times)
    assert [l.index for l in lines] == list(range(boot, boot + len(lines)))
    assert "Adafruit.io WipperSnapper\n" in mon.output

    dumped = mon.dump(tmp_path / "boot.serial.log").read_text().splitlines()
    assert len(dumped) == mon.mark()
    assert all(re.match(r"^\[\d\d:\d\d:\d\d\.\d{3}\] ", l) for l in dumped)


def test_power_cycle_drops_the_port(board_entry, power, port, open_monitor):
    mon = open_monitor(port)
    hbu.power_cycle_board(board_entry, **power)
    with pytest.raises(hbu.SerialError, match="lost") as exc:
        mon.expect("never printed", timeout=10)
    assert not isinstance(exc.value, AssertionError)
    mon.close()
    with pytest.raises(hbu.SerialError, match="closed"):
        mon.board_reset()

    # A new monitor on the re-enumerated port sees the fresh boot.
    mon = open_monitor(hbu.wait_for_port(board_entry))
    expect_banner(mon)
