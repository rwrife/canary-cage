"""Beacon adapters — sinks that announce a canary fired."""

from .base import Beacon, BeaconRecord
from .file import FileBeacon
from .log import LogBeacon
from .webhook import WebhookBeacon

__all__ = ["Beacon", "BeaconRecord", "FileBeacon", "LogBeacon", "WebhookBeacon"]
