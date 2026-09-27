"""Detectors find sensitive spans in text."""

from .base import Detector
from .literal import LiteralPlaceholderDetector
from .manual import ManualDetector
from .regex import RegexDetector

__all__ = [
    "Detector",
    "LiteralPlaceholderDetector",
    "ManualDetector",
    "RegexDetector",
]
