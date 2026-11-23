"""
HIL tests for Adafruit Wippersnapper firmware's pixels component, with a
DotStar strand.

How Does This Work?
A DotStar strand is wired to two DUT pins, data and clock; the board
entry's dotstar_strand probe names them, the pixel count and color
ordering (the dotstar_strand fixture). The broker adds, writes and
removes the strand, and the tests check the DUT handles each, by its
serial log, without publishing a component error. The sequence test
holds each color for a while, for camera instrumentation to check
(pixels_helpers.hold()); nothing on this host reads the pixels' color
back yet.

Exercises:
- `src/components/pixels/controller.cpp::Router()`
- `src/components/pixels/controller.cpp::Handle_Pixels_Add()`
- `src/components/pixels/controller.cpp::Handle_Pixels_Write()`
- `src/components/pixels/controller.cpp::Handle_Pixels_Remove()`
- `src/components/pixels/controller.cpp::GetStrandIndex()`
- `src/components/pixels/hardware.cpp::AddStrand()`
- `src/components/pixels/hardware.cpp::AddDotStar()`
- `src/components/pixels/hardware.cpp::GetStrandOrderDotStar()`
- `src/components/pixels/hardware.cpp::FillStrand()`
- `src/components/pixels/hardware.cpp::ApplyGammaCorrection()`
- `src/components/pixels/hardware.cpp::RemoveStrand()`
"""

import logging

import pytest

from pixels_helpers import (BRIGHTNESS, ON_OFF, add, assert_no_errors,
                            remove, write, write_sequence)


log = logging.getLogger("hil.pixels")

# The Write's color, 0x00RRGGBB: DotStars have no white, so the most
# significant byte is ignored. Each channel differs, so a swapped ordering
# would show on the strand.
COLOR = 0x00FF8040


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_pixels_dotstar_add_write_remove(board, secrets_name, flashed_board,
                                         serial, protomq, checkin,
                                         dotstar_strand):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    log.info("strand: %s, brightness %d, color 0x%06X", dotstar_strand,
             BRIGHTNESS, COLOR)

    # --- Act ----------------------------------------------------------
    add(serial, protomq, uid, dotstar_strand)
    write(serial, protomq, uid, dotstar_strand, COLOR)
    remove(serial, protomq, uid, dotstar_strand)

    # --- Assert -------------------------------------------------------
    # Each step was logged above, in order; none failed
    assert_no_errors(serial, protomq, uid, dotstar_strand)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_pixels_dotstar_color_sequence(board, secrets_name, flashed_board,
                                       serial, protomq, checkin,
                                       dotstar_strand):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    add(serial, protomq, uid, dotstar_strand)

    # --- Act ----------------------------------------------------------
    # Red, blue, then green, each held for HOLD_SECONDS
    write_sequence(serial, protomq, uid, dotstar_strand)
    remove(serial, protomq, uid, dotstar_strand)

    # --- Assert -------------------------------------------------------
    # One Filling strand! per color above, in order; none failed
    assert_no_errors(serial, protomq, uid, dotstar_strand)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_pixels_dotstar_on_off(board, secrets_name, flashed_board, serial,
                               protomq, checkin, dotstar_strand):
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    add(serial, protomq, uid, dotstar_strand)

    # --- Act ----------------------------------------------------------
    # White, then black (off), each held for HOLD_SECONDS
    write_sequence(serial, protomq, uid, dotstar_strand, ON_OFF)
    remove(serial, protomq, uid, dotstar_strand)

    # --- Assert -------------------------------------------------------
    # One Filling strand! per color above, in order; none failed
    assert_no_errors(serial, protomq, uid, dotstar_strand)
