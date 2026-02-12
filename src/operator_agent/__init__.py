"""Operator Agent - Personal AI agent bridge."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("operator-agent")
except PackageNotFoundError:
    __version__ = "0.1.0"
