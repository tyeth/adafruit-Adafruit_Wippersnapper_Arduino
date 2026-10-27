"""Pre-power hooks run before every uhubctl and esptool run. Needs no
hardware: subprocess.run is replaced."""

import subprocess
from types import SimpleNamespace

import pytest

import hil_board_utils


@pytest.fixture
def calls(monkeypatch):
    """The order hooks and subprocess.run are called in."""
    calls = []

    def run(cmd, **kwargs):
        calls.append("run")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(hil_board_utils.subprocess, "run", run)
    monkeypatch.setattr(hil_board_utils, "find_uhubctl", lambda *a: "uhubctl")
    monkeypatch.setattr(hil_board_utils, "_pre_power_hooks", [])
    return calls


def _run_uhubctl():
    hil_board_utils.run_uhubctl(["-a", "off"], sudo=False)


def _run_esptool():
    hil_board_utils.esp_run_esptool(SimpleNamespace(path="esptool"), ["version"])


@pytest.mark.parametrize("runner", [_run_uhubctl, _run_esptool])
def test_hook_runs_before_command(calls, runner):
    hil_board_utils.add_pre_power_hook(lambda: calls.append("hook"))
    runner()
    assert calls == ["hook", "run"]


@pytest.mark.parametrize("runner", [_run_uhubctl, _run_esptool])
def test_raising_hook_stops_command(calls, runner):
    def hook():
        raise OSError("i2c gone")

    hil_board_utils.add_pre_power_hook(hook)
    with pytest.raises(OSError):
        runner()
    assert calls == []


def test_removed_hook_doesnt_run(calls):
    def hook():
        calls.append("hook")

    hil_board_utils.add_pre_power_hook(hook)
    hil_board_utils.remove_pre_power_hook(hook)
    _run_uhubctl()
    assert calls == ["run"]
