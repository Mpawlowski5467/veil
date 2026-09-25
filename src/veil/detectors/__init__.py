"""Detectors find sensitive spans in text."""

from .base import Detector
from .manual import ManualDetector
from .regex import RegexDetector

__all__ = ["Detector", "ManualDetector", "RegexDetector"]
