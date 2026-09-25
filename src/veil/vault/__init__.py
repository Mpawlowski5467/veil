"""Vaults store the mapping between original values and placeholders."""

from .base import Vault
from .memory import MemoryVault

__all__ = ["MemoryVault", "Vault"]
