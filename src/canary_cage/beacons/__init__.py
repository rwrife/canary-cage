"""Beacon adapters — sinks that announce a canary fired."""

from .base import Beacon, BeaconRecord
from .file import FileBeacon
from .log import LogBeacon

__all__ = ["Beacon", "BeaconRecord", "FileBeacon", "LogBeacon"]
