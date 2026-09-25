"""The high-level API: `Shield` ties detection, masking, and restoring together."""

from __future__ import annotations

import functools
import warnings
from collections.abc import Callable, Mapping, Sequence

from .detectors.base import Detector
from .detectors.manual import ManualDetector
from .detectors.regex import PatternLike, RegexDetector
from .masker import Masker
from .restorer import Restorer
from .types import MaskResult, RestoreResult, ShieldWarning
from .vault.base import Vault
from .vault.memory import MemoryVault


class Shield:
    r"""Masks PII before text goes to a model and restores it in the reply.

    One `Shield` holds one vault, so the same value gets the same placeholder
    across every `mask` call. Use one instance per conversation.

    Example:
        >>> shield = Shield(custom_patterns={"ORDER": r"#\d{5}"})
        >>> shield.add_entity("Jan Nowak", "PERSON")
        >>> text = "Email Jan Nowak at jan.n@example.com about order #12345."
        >>> shield.mask(text).text
        'Email [PERSON_1] at [EMAIL_1] about order [ORDER_1].'
        >>> shield.restore("Hi [PERSON_1], order [ORDER_1] has shipped.").text
        'Hi Jan Nowak, order #12345 has shipped.'
    """

    def __init__(
        self,
        *,
        custom_patterns: Mapping[str, PatternLike] | None = None,
        detectors: Sequence[Detector] | None = None,
        vault: Vault | None = None,
    ) -> None:
        r"""Create a shield.

        Args:
            custom_patterns: Extra regex patterns for the default
                `RegexDetector`, mapping an entity type to a pattern, e.g.
                ``{"ORDER": r"#\d{5}"}``. Reusing a built-in name (``"EMAIL"``,
                ``"PHONE"``, ``"IPV4"``) replaces that pattern.
            detectors: Detectors to use instead of the default
                `RegexDetector`. Manually registered entities are always
                detected in addition to these.
            vault: Where mappings are stored. Defaults to a new `MemoryVault`.

        Raises:
            ValueError: If both ``custom_patterns`` and ``detectors`` are given,
                or a custom pattern is invalid.
            TypeError: If a detector or the vault does not implement its
                protocol.
        """
        if detectors is not None and custom_patterns is not None:
            raise ValueError(
                "custom_patterns configures the default RegexDetector; when "
                "passing detectors, include RegexDetector(custom_patterns=...) "
                "among them instead."
            )
        if detectors is None:
            detectors = [RegexDetector(custom_patterns)]
        elif isinstance(detectors, Detector):
            raise TypeError("detectors must be a sequence of detectors, not one")
        for detector in detectors:
            if not isinstance(detector, Detector):
                raise TypeError(
                    f"{type(detector).__name__} does not implement "
                    "detect(text) -> list[Span]"
                )
        if vault is None:
            vault = MemoryVault()
        elif not isinstance(vault, Vault):
            raise TypeError(f"{type(vault).__name__} does not implement Vault")

        self._vault = vault
        self._manual = ManualDetector()
        self._masker = Masker([self._manual, *detectors], vault)
        self._restorer = Restorer(vault)

    @property
    def vault(self) -> Vault:
        """The vault holding this shield's value <-> placeholder mappings."""
        return self._vault

    def add_entity(self, value: str, entity_type: str) -> None:
        """Register a value that detectors can't find, such as a name.

        From now on every `mask` call replaces ``value`` wherever it appears as
        an exact, case-sensitive match that is not part of a longer word.
        Registering the same value again replaces its entity type for future
        detection; a placeholder already assigned to it is kept.

        Args:
            value: The literal text to mask, e.g. ``"Jan Nowak"``.
            entity_type: Its type, e.g. ``"PERSON"``: upper-case letters,
                digits, and underscores, starting with a letter.

        Raises:
            ValueError: If ``value`` is blank or ``entity_type`` is invalid.
        """
        self._manual.add(value, entity_type)

    def mask(self, text: str) -> MaskResult:
        """Replace PII in ``text`` with placeholders.

        Args:
            text: Text about to be sent to a model.

        Returns:
            A `MaskResult` with the masked text, the replacements made, and any
            warnings (for example a known value that still appears in the
            masked text).
        """
        return self._masker.mask(text)

    def restore(self, text: str) -> RestoreResult:
        """Replace placeholders in ``text`` with the original values.

        Never raises on unknown placeholders: they are left as they are and
        listed in the result's ``warnings``.

        Args:
            text: Text from a model, usually its reply to masked input.

        Returns:
            A `RestoreResult` with the restored text, how many placeholders
            were replaced, and any warnings.
        """
        return self._restorer.restore(text)

    def wrap(self, llm: Callable[[str], str]) -> Callable[[str], str]:
        """Wrap a text-in, text-out model call so it only ever sees masked text.

        The returned function masks its input, calls ``llm`` with the masked
        text, restores the reply, and returns it. Mask and restore warnings are
        emitted with `warnings.warn` as `ShieldWarning`; turn them into errors
        with ``warnings.simplefilter("error", ShieldWarning)``.

        Args:
            llm: Any callable taking a prompt string and returning a string.

        Returns:
            A callable with the same signature.
        """

        def safe_llm(text: str) -> str:
            masked = self.mask(text)
            _emit(masked.warnings)
            reply = llm(masked.text)
            if not isinstance(reply, str):
                raise TypeError(
                    f"Wrapped model returned {type(reply).__name__}, expected str"
                )
            restored = self.restore(reply)
            _emit(restored.warnings)
            return restored.text

        functools.update_wrapper(safe_llm, llm, updated=())
        return safe_llm

    def reset(self) -> None:
        """Clear the vault, e.g. to start a new conversation.

        Placeholder numbering restarts at 1. Registered entities and custom
        patterns are configuration, not conversation state, so they are kept.
        """
        self._vault.clear()


def _emit(messages: list[str]) -> None:
    # stacklevel=3 attributes the warning to the code that called safe_llm().
    for message in messages:
        warnings.warn(message, ShieldWarning, stacklevel=3)
