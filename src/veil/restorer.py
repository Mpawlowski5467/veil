"""Restoring: put original values back in place of placeholders."""

from __future__ import annotations

import re

from .placeholders import PLACEHOLDER_RE
from .types import RestoreResult
from .vault.base import Vault


class Restorer:
    """Replaces known placeholders in a model's reply with their original values.

    The reply is scanned once for complete ``[TYPE_N]`` tokens, so
    ``[PERSON_1]`` is never confused with ``[PERSON_10]``, and a restored value
    that happens to contain placeholder-like text is never scanned again.
    Unknown placeholders are left as they are and reported; restoring never
    raises because of them.
    """

    def __init__(self, vault: Vault) -> None:
        """Create a restorer that looks placeholders up in ``vault``."""
        self._vault = vault

    def restore(self, text: str) -> RestoreResult:
        """Restore every known placeholder in ``text``.

        Args:
            text: Text that may contain placeholders, usually a model reply.

        Returns:
            The restored text, the number of placeholder occurrences replaced,
            and a warning for each distinct unknown placeholder.

        Raises:
            TypeError: If ``text`` is not a string.
        """
        if not isinstance(text, str):
            raise TypeError(f"restore() expects str, got {type(text).__name__}")

        restored_count = 0
        unknown: dict[str, None] = {}

        def replace(match: re.Match[str]) -> str:
            nonlocal restored_count
            placeholder = match.group(0)
            value = self._vault.get_value(placeholder)
            if value is None:
                unknown[placeholder] = None
                return placeholder
            restored_count += 1
            return value

        restored = PLACEHOLDER_RE.sub(replace, text)
        warnings = [
            f"Unknown placeholder {placeholder} was left unchanged."
            for placeholder in unknown
        ]
        return RestoreResult(
            text=restored, restored_count=restored_count, warnings=warnings
        )
