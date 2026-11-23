"""
HIL tests for Adafruit Wippersnapper firmware's pixels component, with a
NeoPixel strand.

How Does This Work?
A NeoPixel strand is wired to a DUT pin; the board entry's neopixel_strand
probe names the pin, pixel count and color ordering (the neopixel_strand
fixture). The broker adds, writes and removes the strand, and the tests
check the DUT handles each, by its serial log, without publishing a
component error. The sequence test holds each color for a while, for
camera instrumentation to check (pixels_helpers.hold()); nothing on this
host reads the pixels' color back yet.

Exercises:
- `src/components/pixels/controller.cpp::Router()`
- `src/components/pixels/controller.cpp::Handle_Pixels_Add()`
- `src/components/pixels/controller.cpp::Handle_Pixels_Write()`
- `src/components/pixels/controller.cpp::Handle_Pixels_Remove()`
- `src/components/pixels/controller.cpp::GetStrandIndex()` (and with no strand)
- `src/components/pixels/hardware.cpp::AddStrand()`
- `src/components/pixels/hardware.cpp::AddNeoPixel()`
- `src/components/pixels/hardware.cpp::GetStrandOrderNeoPixel()`
- `src/components/pixels/hardware.cpp::FillStrand()`
- `src/components/pixels/hardware.cpp::ApplyGammaCorrection()`
- `src/components/pixels/hardware.cpp::RemoveStrand()`
"""

import logging

import pytest

from pixels_helpers import (BRIGHTNESS, CHECKIN_TIMEOUT, CRASH_RE,
                            MAX_PIXEL_STRANDS, NOT_FOUND, ON_OFF, add,
                            assert_no_errors, remove, write, write_sequence)


log = logging.getLogger("hil.pixels")

# The Write's color, 0xWWRRGGBB: white is the most significant byte (for
# RGBW pixels), then red, green, blue. Each channel differs, so a swapped
# ordering would show on the strand.
COLOR = 0x11FF8040
# How many add/write/remove cycles the slot-overflow test runs: past every
# slot, so a Remove that frees none has Handle_Pixels_Add() write beyond
# _pixel_strands[] by the last cycles
OVERFLOW_CYCLES = MAX_PIXEL_STRANDS + 2


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_pixels_neopixel_add_write_remove(board, secrets_name, flashed_board,
                                          serial, protomq, checkin,
                                          neopixel_strand):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    log.info("strand: %s, brightness %d, color 0x%08X", neopixel_strand,
             BRIGHTNESS, COLOR)

    # --- Act ----------------------------------------------------------
    add(serial, protomq, uid, neopixel_strand)
    write(serial, protomq, uid, neopixel_strand, COLOR)
    remove(serial, protomq, uid, neopixel_strand)

    # --- Assert -------------------------------------------------------
    # Each step was logged above, in order; none failed
    assert_no_errors(serial, protomq, uid, neopixel_strand)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_pixels_neopixel_color_sequence(board, secrets_name, flashed_board,
                                        serial, protomq, checkin,
                                        neopixel_strand):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    add(serial, protomq, uid, neopixel_strand)

    # --- Act ----------------------------------------------------------
    # Red, blue, then green, each held for HOLD_SECONDS
    write_sequence(serial, protomq, uid, neopixel_strand)
    remove(serial, protomq, uid, neopixel_strand)

    # --- Assert -------------------------------------------------------
    # One Filling strand! per color above, in order; none failed
    assert_no_errors(serial, protomq, uid, neopixel_strand)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_pixels_neopixel_on_off(board, secrets_name, flashed_board, serial,
                                protomq, checkin, neopixel_strand):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    add(serial, protomq, uid, neopixel_strand)

    # --- Act ----------------------------------------------------------
    # White, then black (off), each held for HOLD_SECONDS
    write_sequence(serial, protomq, uid, neopixel_strand, ON_OFF)
    remove(serial, protomq, uid, neopixel_strand)

    # --- Assert -------------------------------------------------------
    # One Filling strand! per color above, in order; none failed
    assert_no_errors(serial, protomq, uid, neopixel_strand)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_pixels_remove_without_add(board, secrets_name, flashed_board,
                                   serial, protomq, checkin,
                                   neopixel_strand):
    # --- Arrange ------------------------------------------------------
    # Nothing added on the strand's pin, so there's no strand to remove
    uid = checkin()
    msg = {"pinData": neopixel_strand.pin}

    # --- Act ----------------------------------------------------------
    protomq.publish(uid, {"pixels": {"remove": msg}})

    # --- Assert -------------------------------------------------------
    # GetStrandIndex() finds no strand, so Handle_Pixels_Remove() rejects
    # it with an error.component (it logs nothing)...
    protomq.expect_b2d_message(uid, "pixels.remove", match=msg, timeout=5)
    protomq.expect_d2b_message(uid, "error.component",
                               match={"pin": neopixel_strand.pin,
                                      "message": NOT_FOUND},
                               timeout=CHECKIN_TIMEOUT)
    # ...without removing anything, crashing or rebooting
    serial.assert_none(r"\[pixels\] Removed pixel strand")
    serial.assert_none(CRASH_RE)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_bug_remove_overflows_strand_slots(board, secrets_name,
                                           flashed_board, serial, protomq,
                                           checkin, neopixel_strand):
    """Handle_Pixels_Remove() releases the strand but never frees its
    slot: it neither deletes the PixelsHardware nor decrements
    _num_strands. Handle_Pixels_Add() always takes slot _num_strands
    without checking it against MAX_PIXEL_STRANDS, so the add after
    MAX_PIXEL_STRANDS add/remove cycles on one strand writes past
    _pixel_strands[], over _num_strands. Fails until Remove frees the slot
    (or Add rejects a full controller with an error.component)."""
    # --- Arrange ------------------------------------------------------
    uid = checkin()

    # --- Act ----------------------------------------------------------
    # Only one strand is ever added at a time, so a controller that frees
    # slots on Remove never holds more than one
    for cycle in range(1, OVERFLOW_CYCLES + 1):
        log.info("cycle %d/%d (MAX_PIXEL_STRANDS %d)", cycle,
                 OVERFLOW_CYCLES, MAX_PIXEL_STRANDS)
        add(serial, protomq, uid, neopixel_strand)
        write(serial, protomq, uid, neopixel_strand, COLOR)
        remove(serial, protomq, uid, neopixel_strand)

    # --- Assert -------------------------------------------------------
    # Every cycle was handled above, in order; the DUT didn't crash or
    # reboot, and nothing failed
    serial.assert_none(CRASH_RE)
    assert_no_errors(serial, protomq, uid, neopixel_strand)
