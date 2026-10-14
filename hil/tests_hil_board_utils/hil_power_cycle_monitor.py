#!/usr/bin/env python3
"""Power-cycle a HIL board's hub port, attach to its serial port when it
re-enumerates, and check that it prints the expected boot banner.

    python tests_hil_board_utils/hil_power_cycle_monitor.py
    python tests_hil_board_utils/hil_power_cycle_monitor.py -timeout 30 -o logs/boot.serial.log
    python tests_hil_board_utils/hil_power_cycle_monitor.py -board feather_esp32_v2 -reset

Each BANNER line must appear, in order, as a whole line. Exits 0 if all
do, 1 otherwise; the capture is written to -o either way.

The board starts booting as soon as power returns, before its serial port
can be opened, so the first lines of the boot can be missed. The banner
comes several seconds in, so that's fine; pass -reset to reset it again once
attached and capture the whole boot.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

HIL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HIL_DIR))
import hil_board_utils as hbu  # noqa: E402

DEFAULT_BOARDS = HIL_DIR / "hil-boards.json"

BANNER = [
    "Adafruit.io WipperSnapper",
    "Firmware Version: 2.0.0-alpha.1",
    "API: Version 2",
    "Board ID: feather-esp32-v2",
]


def load_board_entry(path, board=None):
    """Read a board entry from path, which holds one entry or a list of them."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise hbu.PortError(f"cannot read board file {path}: {e}") from e
    entries = data if isinstance(data, list) else [data]
    if board is None:
        if len(entries) != 1:
            raise hbu.PortError(f"{path} has {len(entries)} boards; pick one with -board")
        entry = entries[0]
    else:
        matches = [e for e in entries if isinstance(e, dict) and e.get("board") == board]
        if not matches:
            raise hbu.PortError(f"board '{board}' not found in {path}")
        entry = matches[0]
    hbu.validate_board_entry(entry)
    return entry


def check_banner(mon, timeout):
    """Expect each BANNER line in order, all within timeout seconds."""
    deadline = time.monotonic() + timeout
    for text in BANNER:
        remaining = max(deadline - time.monotonic(), 0)
        mon.expect(rf"^{re.escape(text)}$", timeout=remaining)
        print(f"ok: {text}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Power-cycle a HIL board and print its serial output"
    )
    parser.add_argument("-boards", type=Path, default=DEFAULT_BOARDS,
                        help=f"board file (default: {DEFAULT_BOARDS.name})")
    parser.add_argument("-board", help="board name, if the board file lists several")
    parser.add_argument("-timeout", type=float, default=20,
                        help="seconds after attaching for the whole banner to appear "
                             "(default: 20)")
    parser.add_argument("-delay", type=float, default=2,
                        help="seconds the port stays off (default: 2)")
    parser.add_argument("-port-timeout", type=float, default=15,
                        help="seconds to wait for the port to come back (default: 15)")
    parser.add_argument("-baud", type=int, default=hbu.SERIAL_BAUD,
                        help=f"baud rate (default: {hbu.SERIAL_BAUD})")
    parser.add_argument("-reset", action="store_true",
                        help="reset the board after attaching to capture the full boot")
    parser.add_argument("-o", type=Path, help="write the timestamped capture here")
    parser.add_argument("-sudo", action="store_true",
                        help="run uhubctl under 'sudo -n' (not needed if your user can "
                             "write the hub's sysfs port files, e.g. via the dialout group)")
    parser.add_argument("-uhubctl", help="path to uhubctl, used if not found on PATH")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        entry = load_board_entry(args.boards, args.board)
        loc = entry["location"]
        print(f"power-cycling '{entry['board']}' at hub {loc['hub']} port "
              f"{loc['port']} ({args.delay}s off)", file=sys.stderr)
        hbu.power_cycle_board(entry, args.delay, args.uhubctl, args.sudo)

        port = hbu.wait_for_port(entry, timeout=args.port_timeout)
        print(f"attached to {port}; checking the boot banner", file=sys.stderr)

        mon = hbu.SerialMonitor(port, args.baud)
        try:
            if args.reset:
                mon.reset()
            check_banner(mon, args.timeout)
        finally:
            mon.close()
            if args.o:
                print(f"wrote {mon.dump(args.o)}", file=sys.stderr)
        print("PASS: boot banner matched")
    except hbu.HilBoardUtilsError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
