from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from operator_ai.config import Config, DefaultsConfig


def test_timezone_defaults_to_utc() -> None:
    d = DefaultsConfig(models=["test/model"])
    assert d.timezone == "UTC"


def test_timezone_override() -> None:
    d = DefaultsConfig(models=["test/model"], timezone="America/Vancouver")
    assert d.timezone == "America/Vancouver"


def test_config_tz_returns_zoneinfo() -> None:
    c = Config(defaults={"models": ["test/m"], "timezone": "Europe/London"})
    assert c.tz == ZoneInfo("Europe/London")


def test_config_tz_defaults_to_utc() -> None:
    c = Config(defaults={"models": ["test/m"]})
    assert c.tz == ZoneInfo("UTC")


def test_invalid_timezone_raises() -> None:
    with pytest.raises(ValueError, match="Unknown timezone"):
        DefaultsConfig(models=["test/model"], timezone="Mars/Olympus")
