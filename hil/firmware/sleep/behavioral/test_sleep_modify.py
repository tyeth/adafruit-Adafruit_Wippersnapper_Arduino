"""
HIL tests for modifying a sleeping device's sleep settings: like "stop
sleeping", IO sends no message; the device's next checkin response carries
the new checkin.Response.sleep_config (and any component changes it needs),
which the device applies when it wakes and checks in.

- The timer test starts in light sleep and is switched to deep sleep: the
  light wake's checkin applies it, and the next sleep is a deep one, which
  the same 30 s timer wakes with a reset.
- The ext0 test (deep and light) starts waking on the button pin going LOW
  (pulled up) and is switched to waking on it going HIGH (pulled down): the
  old stimulus wakes it once more, its checkin applies the new pull and
  level, the old stimulus no longer wakes it, and the new one does.
"""

import time

import pytest

import sleep_helpers as sh
from sleep_helpers import (CHECKIN_TIMEOUT, DISCONNECT_TIMEOUT, EXT0_PIN,
                           HOLD_S, TIMER_S, WAKE_TIMEOUT, log)

# A deep wake's checkin: a boot, WiFi and the broker
DEEP_WAKE_CHECKIN_TIMEOUT = 150
# A light wake's checkin: WiFi and the broker again, no boot
LIGHT_WAKE_CHECKIN_TIMEOUT = 120
# The sleep watch's end, per mode: a deep wake's boot, or a light wake (or
# any reset, which a light sleep mustn't do)
WAKE_RES = {"deep": sh.DEEP_WAKE_RE, "light": sh.LIGHT_WAKE_OR_RESET_RE}
MODES = {"deep": "S_DEEP", "light": "S_LIGHT"}


def expect_deep_wake(serial, slept_at, reason):
    """A deep wake for reason ("Timer", "EXT0"), a reset into a fresh boot.
    Returns (when the boot ROM's reset line arrived, seconds asleep from
    slept_at, the firmware's Total Sleep Duration)."""
    woke_at = sh.line_time(serial, serial.expect(sh.DEEP_SLEEP_RESET_RE,
                                                 timeout=WAKE_TIMEOUT))
    serial.expect(rf"^Sleep Wakeup Reason: {reason}$", timeout=10)
    serial.expect(r"^Prv\. Sleep Mode: %d$" % sh.S_DEEP, timeout=5)
    reported = int(serial.expect(r"^Total Sleep Duration \(sec\): (\d+)$",
                                 timeout=5).group(1))
    return woke_at, woke_at - slept_at, reported


def expect_light_wake(serial, slept_at, asleep_from, ext0):
    """A light wake, where the DUT slept, its wake line already expected;
    if ext0, it hands the wake pin back to its digital component. Returns
    (when the wake was timed, seconds asleep from slept_at, the firmware's
    "Slept for")."""
    if ext0:
        serial.expect(r"^\[sleep\] Released ext0 wake pin back to digital "
                      r"GPIO(, restored its pull)?$", timeout=5)
    slept_for = serial.expect(sh.WOKE_TIME_RE, timeout=5)
    woke_at = sh.line_time(serial, slept_for)
    resets = [l.text for l in serial.lines(asleep_from)
              if l.time <= woke_at and sh.RESET_RE.search(l.text)]
    assert not resets, f"DUT reset during light sleep: {resets}"
    return woke_at, woke_at - slept_at, int(slept_for.group(1))


def expect_light_checkin(serial, protomq, uid, board_definition, response):
    """After a light wake: the DUT reconnects and checks in, the broker
    answering response."""
    serial.expect(r"^\[app\] Reconnecting network after light sleep "
                  r"wake\.\.\.$", timeout=10)
    serial.expect(r"^\[app\] Checking in after light sleep wake\.\.\.$",
                  timeout=LIGHT_WAKE_CHECKIN_TIMEOUT)
    sh.expect_wake_checkin(protomq, uid, board_definition, response,
                           LIGHT_WAKE_CHECKIN_TIMEOUT)


# --- (a) Timer: light sleep, modified to deep sleep ---------------------

LIGHT_TIMER = {"mode": "S_LIGHT", "timer": {"duration": TIMER_S}}
DEEP_TIMER = {"mode": "S_DEEP", "timer": {"duration": TIMER_S}}


@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_modify_light_to_deep(board, secrets_name, flashed_board,
                                    serial, protomq, checkin,
                                    board_definition):
    """A 30 s light sleep, modified while asleep to a 30 s deep sleep: the
    light timer wakes the DUT where it slept, its checkin applies the deep
    SleepConfig, and it deep sleeps; the timer wakes it with a reset, and
    its fresh checkin keeps it deep sleeping."""
    # --- Arrange ------------------------------------------------------
    uid = checkin()
    sh.enable_sleep_on_checkin(protomq, board_definition, LIGHT_TIMER)
    protomq.publish(uid, {"sleep": {"sleepConfig": LIGHT_TIMER}})
    protomq.expect_b2d_message(uid, "sleep.sleepConfig", match=LIGHT_TIMER,
                               timeout=5)
    slept_at = sh.expect_goodnight_and_sleep(serial, protomq, uid, "light")
    asleep_from = serial.mark()

    # --- Act ----------------------------------------------------------
    # Modify it to deep sleep while it light sleeps
    deep_response = sh.enable_sleep_on_checkin(protomq, board_definition,
                                               DEEP_TIMER)

    # --- Assert -------------------------------------------------------
    rows, misses = [], []
    try:
        # 1. The light timer wakes it, where it slept
        sh.assert_dropped(uid, sh.watch_sleep(board, serial, protomq, uid,
                                              WAKE_TIMEOUT,
                                              wake_re=WAKE_RES["light"]))
        serial.expect(sh.LIGHT_WAKE_RE.pattern, timeout=WAKE_TIMEOUT)
        _, asleep, reported = expect_light_wake(serial, slept_at,
                                                asleep_from, ext0=False)
        rows.append((1, asleep, reported, "light"))
        sh.check_wake_timing(1, asleep, reported, misses)

        # 2. Its checkin applies the deep SleepConfig
        expect_light_checkin(serial, protomq, uid, board_definition,
                             deep_response)
        serial.expect(r"^\[checkin\] Response is new or changed$",
                      timeout=CHECKIN_TIMEOUT)
        serial.expect(r"^\[app\] Processing sleep configuration from "
                      r"checkin$", timeout=CHECKIN_TIMEOUT)
        serial.expect(sh.TIMER_ARMED_RE, timeout=5)
        serial.expect(r"^Sleep Enabled: YES$", timeout=5)
        serial.expect(r"^Completed checkin process!$",
                      timeout=CHECKIN_TIMEOUT)

        # 3. ...so its next sleep is a deep one
        slept_at = sh.expect_goodnight_and_sleep(serial, protomq, uid,
                                                 "deep", configured=False,
                                                 entering_loop=False)
        sh.assert_dropped(uid, sh.watch_sleep(board, serial, protomq, uid,
                                              WAKE_TIMEOUT,
                                              wake_re=WAKE_RES["deep"]))

        # 4. The timer wakes it 30 s later, with a deep sleep's reset
        _, asleep, reported = expect_deep_wake(serial, slept_at, "Timer")
        rows.append((2, asleep, reported, "deep"))
        sh.check_wake_timing(2, asleep, reported, misses)

        # 5. Its fresh checkin keeps it deep sleeping
        sh.expect_wake_checkin(protomq, uid, board_definition, deep_response,
                               DEEP_WAKE_CHECKIN_TIMEOUT)
        serial.expect(r"^\[app\] Processing sleep configuration from "
                      r"checkin$", timeout=CHECKIN_TIMEOUT)
        sh.expect_goodnight_and_sleep(serial, protomq, uid, "deep")
        sh.assert_dropped(uid, sh.watch_sleep(board, serial, protomq, uid,
                                              DISCONNECT_TIMEOUT))
    finally:
        sh.log_wake_table("light sleep modified to deep sleep", rows,
                          extra_headers=("slept in",))
    assert not misses, "\n".join(misses)


# --- (b) ext0: button HIGH->LOW, modified to LOW->HIGH ------------------

# The button wakes the DUT going LOW (pulled up), then, modified, going
# HIGH (pulled down)
OLD, NEW = sh.EXT0_VARIANTS["low"], sh.EXT0_VARIANTS["high"]


def ext0_wake(probe, serial, mode, variant, slept_at, asleep_from):
    """Press the button: drive the wake pin to variant's wake level, expect
    the wake, then release the pin, leaving it to whichever pull the
    checkin sets up next. Returns (wake latency, seconds asleep, the
    firmware's sleep duration)."""
    wake_line = sh.DEEP_SLEEP_RESET_RE if mode == "deep" else \
        sh.LIGHT_WAKE_RE.pattern
    driven_at = time.time()
    probe.drive(EXT0_PIN, variant.level)
    try:
        reset = serial.expect(wake_line, timeout=sh.EXT0_LATENCY_S + 3)
    finally:
        probe.release(EXT0_PIN)
    if mode == "deep":
        woke_at = sh.line_time(serial, reset)
        serial.expect(r"^Sleep Wakeup Reason: EXT0$", timeout=10)
        reported = int(serial.expect(
            r"^Total Sleep Duration \(sec\): (\d+)$", timeout=10).group(1))
        asleep = woke_at - slept_at
    else:
        woke_at, asleep, reported = expect_light_wake(serial, slept_at,
                                                      asleep_from, ext0=True)
    return woke_at - driven_at, asleep, reported


@pytest.mark.parametrize("mode", list(MODES))
@pytest.mark.parametrize("secrets_name", ["good"], indirect=True)
def test_sleep_modify_ext0_level(board, secrets_name, flashed_board, serial,
                                 protomq, checkin, board_definition, probe,
                                 mode):
    """An ext0 sleep waking on the button going LOW (pulled up), modified
    while asleep to waking on it going HIGH (pulled down): the old press
    wakes the DUT once more, its checkin applies the new pull and level,
    the old level no longer wakes it, and the new press does."""
    sleep_mode = MODES[mode]
    wake_re = WAKE_RES[mode]
    # --- Arrange ------------------------------------------------------
    uid, _, old_config = sh.ext0_arrange(probe, serial, protomq, checkin,
                                         board_definition, sleep_mode, OLD)
    slept_at = sh.ext0_send_and_sleep(serial, protomq, uid, mode, OLD,
                                      old_config)
    sh.assert_stays_asleep(board, serial, protomq, uid, HOLD_S, wake_re)

    # --- Act ----------------------------------------------------------
    # Modify it while it sleeps: the pin pulled down, waking HIGH
    new_adds = sh.ext0_component_adds(NEW)
    new_response = sh.enable_sleep_on_checkin(
        protomq, board_definition, sh.ext0_sleep_config(sleep_mode, NEW),
        componentAdds=new_adds)

    # --- Assert -------------------------------------------------------
    misses = []
    # 1. The old press (LOW) still wakes it: the old config is armed
    asleep_from = serial.mark()
    latency, asleep, reported = ext0_wake(probe, serial, mode, OLD, slept_at,
                                          asleep_from)
    sh.ext0_result(mode, OLD, latency, asleep, reported, misses)

    # 2. Its checkin applies the new pull and wake level
    if mode == "deep":
        sh.expect_wake_checkin(protomq, uid, board_definition, new_response,
                               DEEP_WAKE_CHECKIN_TIMEOUT)
        sh.expect_ext0_pin_added(serial, NEW)
        serial.expect(r"^\[app\] Processing sleep configuration from "
                      r"checkin$", timeout=CHECKIN_TIMEOUT)
    else:
        expect_light_checkin(serial, protomq, uid, board_definition,
                             new_response)
        serial.expect(r"^\[checkin\] Response is new or changed$",
                      timeout=CHECKIN_TIMEOUT)
    # "Pull: DOWN": ext0 took the pin's new digital component's pull
    serial.expect(sh.ext0_armed_re(NEW), timeout=CHECKIN_TIMEOUT)
    serial.expect(r"^Sleep Enabled: YES$", timeout=5)
    # ...publishes the released pin, now pulled LOW, and sleeps again
    sh.expect_ext0_pin_published(serial, protomq, uid, NEW.idle,
                                 entering_loop=mode == "deep")
    slept_at = sh.expect_goodnight_and_sleep(serial, protomq, uid, mode,
                                             configured=False,
                                             entering_loop=False)

    # 3. The old level no longer wakes it: the pin sits LOW, pulled down
    sh.assert_stays_asleep(board, serial, protomq, uid, HOLD_S, wake_re)

    # 4. The new press (HIGH) wakes it
    asleep_from = serial.mark()
    latency, asleep, reported = ext0_wake(probe, serial, mode, NEW, slept_at,
                                          asleep_from)
    sh.ext0_result(mode, NEW, latency, asleep, reported, misses)

    # 5. ...and it goes back to sleep on the new config
    if mode == "deep":
        sh.expect_wake_checkin(protomq, uid, board_definition, new_response,
                               DEEP_WAKE_CHECKIN_TIMEOUT)
        sh.expect_ext0_pin_added(serial, NEW)
        serial.expect(sh.ext0_armed_re(NEW), timeout=CHECKIN_TIMEOUT)
    else:
        # The same response it applied last wake: skipped, still armed
        expect_light_checkin(serial, protomq, uid, board_definition,
                             new_response)
        serial.expect(r"^\[checkin\] Response unchanged since last "
                      r"checkin$", timeout=CHECKIN_TIMEOUT)
        serial.expect(r"^Completed checkin process!$",
                      timeout=CHECKIN_TIMEOUT)
    sh.expect_ext0_pin_published(serial, protomq, uid, NEW.idle,
                                 entering_loop=mode == "deep")
    sh.expect_goodnight_and_sleep(serial, protomq, uid, mode,
                                  configured=False, entering_loop=False)
    sh.assert_stays_asleep(board, serial, protomq, uid, HOLD_S, wake_re)
    log.info("ext0 modified from wake %s (pull %s) to wake %s (pull %s) in "
             "%s sleep", "HIGH" if OLD.level else "LOW", OLD.pull,
             "HIGH" if NEW.level else "LOW", NEW.pull, mode)
    assert not misses, "\n".join(misses)
