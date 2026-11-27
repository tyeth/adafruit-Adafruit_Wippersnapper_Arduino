"""Resolving the probe's expander pin names. Needs no hardware."""

import pytest

import hil_probe


@pytest.mark.parametrize("name, pin", [
    ("GPA0", 0), ("GPA1", 1), ("GPA7", 7), ("GPB0", 8), ("GPB7", 15),
])
def test_resolve_expander_pin(name, pin):
    assert hil_probe.resolve_expander_pin(name) == pin


@pytest.mark.parametrize("name", ["GPA8", "GPC0", "gpa0", "A0", "GPA", "", 0, None])
def test_resolve_expander_pin_rejects(name):
    with pytest.raises(ValueError):
        hil_probe.resolve_expander_pin(name)
