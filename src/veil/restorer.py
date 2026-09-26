"""Restoring: put original values back in place of placeholders."""

from __future__ import annotations

import re

from .placeholders import (
    LOOSE_PLACEHOLDER_RE,
    PLACEHOLDER_RE,
    placeholder_candidates,
)
from .types import RepairedPlaceholder, RestoreResult
from .vault.base import Vault


class Restorer:
    r"""Replaces known placeholders in a model's reply with their original values.

    The reply is scanned once for complete bracketed tokens, so ``[PERSON_1]``
    is never confused with ``[PERSON_10]``, and a restored value that happens
    to contain placeholder-like text is never scanned again.

    Models sometimes rewrite placeholders. With ``tolerant=True`` (the
    default), a bracketed token that isn't an exact placeholder, such as
    ``[person_1]``, ``[PERSON 1]``, ``[ PERSON_1 ]``, ``[PERSON_01]``,
    ``【PERSON_1】``, or Markdown's ``\[PERSON_1\]``, is restored when its
    normalized form is in the vault, and listed in ``RestoreResult.repaired``.
    Bracketed text that merely looks similar (``[Figure 2]``) is left alone.

    Unknown placeholders are left as they are and reported; restoring never
    raises because of them.
    """

    def __init__(self, vault: Vault, *, tolerant: bool = True) -> None:
        """Create a restorer.

        Args:
            vault: Where placeholders are looked up.
            tolerant: Also restore placeholders the model rewrote (see above).
        """
        self._vault = vault
        self._tolerant = tolerant

    def restore(self, text: str) -> RestoreResult:
        """Restore every known placeholder in ``text``.

        Args:
            text: Text that may contain placeholders, usually a model reply.

        Returns:
            The restored text, the number of placeholder occurrences replaced,
            a warning for each distinct unknown placeholder, and the rewritten
            placeholders that were repaired.

        Raises:
            TypeError: If ``text`` is not a string.
        """
        if not isinstance(text, str):
            raise TypeError(f"restore() expects str, got {type(text).__name__}")

        restored_count = 0
        unknown: dict[str, None] = {}
        repaired: list[RepairedPlaceholder] = []
        known_types: set[str | None] = set()
        if self._tolerant:
            known_types = {_type_of(stored) for stored, _ in self._vault.items()}

        def replace(match: re.Match[str]) -> str:
            nonlocal restored_count
            written = match.group(0)
            value = self._vault.get_value(written)
            if value is not None:
                restored_count += 1
                return value
            candidates = placeholder_candidates(match["body"]) if self._tolerant else []
            for candidate in candidates:
                value = self._vault.get_value(candidate)
                if value is not None:
                    restored_count += 1
                    repaired.append(RepairedPlaceholder(written, candidate))
                    return value
            # Report exact placeholders, and rewritten ones of a type this
            # vault uses ("[person 3]" when only PERSON_1 and _2 exist), but
            # not ordinary bracketed text like "[Figure 2]".
            if PLACEHOLDER_RE.fullmatch(written) or any(
                _type_of(candidate) in known_types for candidate in candidates
            ):
                unknown[written] = None
            return written

        pattern = LOOSE_PLACEHOLDER_RE if self._tolerant else PLACEHOLDER_RE
        restored = pattern.sub(replace, text)
        warnings = [
            f"Unknown placeholder {placeholder} was left unchanged."
            for placeholder in unknown
        ]
        return RestoreResult(
            text=restored,
            restored_count=restored_count,
            warnings=warnings,
            repaired=repaired,
        )


def _type_of(placeholder: str) -> str | None:
    match = PLACEHOLDER_RE.fullmatch(placeholder)
    return match["type"] if match else None
