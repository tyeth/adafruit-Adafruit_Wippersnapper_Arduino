"""
HIL tests for Adafruit Wippersnapper firmware's pixels component, with a
NeoPixel strand.

How Does This Work?
A NeoPixel strand is wired to a DUT pin; the board entry's neopixel_strand
probe names the pin, pixel count and color ordering (the neopixel_strand
fixture). The broker adds, writes and removes the strand, and the tests
check the DUT handles each, by its serial log, without publishing a
component error. After each fill, the color sequence and on/off tests
photograph the strand with the hil_camera fixture and assert it shows
that color.

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
                            MAX_PIXEL_STRANDS, NOT_FOUND, add,
                            assert_no_errors, remove, write)


log = logging.getLogger("hil.pixels")

# How many add/write/remove cycles the slot-overflow test runs: past every
# slot, so a Remove that frees none has Handle_Pixels_Add() write beyond
# _pixel_strands[] by the last cycles
OVERFLOW_CYCLES = MAX_PIXEL_STRANDS + 2


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_pixels_neopixel_add_write_remove(board, secrets_name, flashed_board,
                                          serial, protomq, checkin,
                                          neopixel_strand):
    """Add a NeoPixel strand, fill it with one color, then remove it. The DUT
    should handle each message and publish no error.component."""
    # --- Arrange ------------------------------------------------------
    strand = neopixel_strand
    # 0xWWRRGGBB: white is the most significant byte (for RGBW pixels),
    # then red, green, blue. Each channel differs, so a swapped ordering
    # would show on the strand.
    color = 0x11FF8040
    uid = checkin()
    log.info("strand: %s, brightness %d, color 0x%08X", strand, BRIGHTNESS,
             color)

    # --- Act ----------------------------------------------------------
    # Each step sends one message from the broker, then waits for the DUT's
    # serial log to show it handled it; a step fails the test if not.

    # pixels.Add -> "[pixels] Added NeoPixel strand on pin <gpio>"
    add(serial, protomq, uid, strand)

    # pixels.Write of color -> "[pixels]: Filling strand!"
    write(serial, protomq, uid, strand, color)

    # pixels.Remove -> "[pixels] Removed pixel strand from pin: <gpio>"
    remove(serial, protomq, uid, strand)

    # --- Assert -------------------------------------------------------
    # No pixels failure on serial, and no error.component for the strand
    assert_no_errors(serial, protomq, uid, strand)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_pixels_neopixel_color_sequence(board, secrets_name, flashed_board,
                                        serial, protomq, checkin,
                                        neopixel_strand, hil_camera):
    """Fill a NeoPixel strand red, blue, then green. After each fill, the
    camera should see the strand lit in that color."""
    # --- Arrange ------------------------------------------------------
    strand = neopixel_strand
    # (name, 0xWWRRGGBB), white off
    colors = (("red", 0x00FF0000),
              ("blue", 0x000000FF),
              ("green", 0x0000FF00))
    uid = checkin()

    # pixels.Add -> "[pixels] Added NeoPixel strand on pin <gpio>"
    add(serial, protomq, uid, strand)

    for name, color in colors:
        # --- Act ------------------------------------------------------
        # pixels.Write of color -> "[pixels]: Filling strand!"
        write(serial, protomq, uid, strand, color)
        # Blocks until the photo is taken and the model has answered
        capture = hil_camera()
        log.info("wrote %s to strand, %s saw %s (%s)", name, capture["model"],
                 capture["result"]["neopixel"], capture["image"])

        # --- Assert ---------------------------------------------------
        assert capture["result"]["neopixel"] == {
            "pixels_power_status": "on", "pixels_color": name}, capture

    # pixels.Remove -> "[pixels] Removed pixel strand from pin: <gpio>"
    remove(serial, protomq, uid, strand)
    # RemoveStrand() clears the strand before releasing it
    capture = hil_camera()
    log.info("removed, %s saw %s (%s)", capture["model"],
             capture["result"]["neopixel"], capture["image"])
    assert capture["result"]["neopixel"] == {
        "pixels_power_status": "off", "pixels_color": "none"}, capture

    # No pixels failure on serial, and no error.component for the strand
    assert_no_errors(serial, protomq, uid, strand)


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_pixels_neopixel_on_off(board, secrets_name, flashed_board, serial,
                                protomq, checkin, neopixel_strand, hil_camera):
    """Turn a NeoPixel strand on (white), then off (black). After each
    fill, the camera should see the strand lit white, then unlit."""
    # --- Arrange ------------------------------------------------------
    strand = neopixel_strand
    # 0xWWRRGGBB: RGB white (RGBW pixels' white stays off)
    white = 0x00FFFFFF
    # Every channel 0, which stays 0 through gamma and brightness: off
    black = 0x00000000
    uid = checkin()

    # pixels.Add -> "[pixels] Added NeoPixel strand on pin <gpio>"
    add(serial, protomq, uid, strand)

    # --- Act: on ------------------------------------------------------
    # pixels.Write of white -> "[pixels]: Filling strand!"
    write(serial, protomq, uid, strand, white)
    # Blocks until the photo is taken and the model has answered
    capture = hil_camera()
    log.info("wrote white, %s saw %s (%s)", capture["model"],
             capture["result"]["neopixel"], capture["image"])

    # --- Assert: on ---------------------------------------------------
    assert capture["result"]["neopixel"] == {
        "pixels_power_status": "on", "pixels_color": "white"}, capture

    # --- Act: off -----------------------------------------------------
    # pixels.Write of black -> "[pixels]: Filling strand!"
    write(serial, protomq, uid, strand, black)
    capture = hil_camera()
    log.info("wrote black, %s saw %s (%s)", capture["model"],
             capture["result"]["neopixel"], capture["image"])

    # --- Assert: off --------------------------------------------------
    assert capture["result"]["neopixel"] == {
        "pixels_power_status": "off", "pixels_color": "none"}, capture

    # pixels.Remove -> "[pixels] Removed pixel strand from pin: <gpio>"
    remove(serial, protomq, uid, strand)
    # RemoveStrand() clears the strand before releasing it
    capture = hil_camera()
    log.info("removed, %s saw %s (%s)", capture["model"],
             capture["result"]["neopixel"], capture["image"])
    assert capture["result"]["neopixel"] == {
        "pixels_power_status": "off", "pixels_color": "none"}, capture

    # No pixels failure on serial, and no error.component for the strand
    assert_no_errors(serial, protomq, uid, strand)


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
    color = 0x11FF8040  # any color; what's shown isn't checked here

    # --- Act ----------------------------------------------------------
    # Only one strand is ever added at a time, so a controller that frees
    # slots on Remove never holds more than one
    for cycle in range(1, OVERFLOW_CYCLES + 1):
        log.info("cycle %d/%d (MAX_PIXEL_STRANDS %d)", cycle,
                 OVERFLOW_CYCLES, MAX_PIXEL_STRANDS)
        add(serial, protomq, uid, neopixel_strand)
        write(serial, protomq, uid, neopixel_strand, color)
        remove(serial, protomq, uid, neopixel_strand)

    # --- Assert -------------------------------------------------------
    # Every cycle was handled above, in order; the DUT didn't crash or
    # reboot, and nothing failed
    serial.assert_none(CRASH_RE)
    assert_no_errors(serial, protomq, uid, neopixel_strand)
