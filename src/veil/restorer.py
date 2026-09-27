"""Restoring: put original values back in place of placeholders."""

from __future__ import annotations

import re

from .placeholders import (
    LOOSE_PLACEHOLDER_RE,
    MAX_PLACEHOLDER_LENGTH,
    PLACEHOLDER_OPENERS,
    PLACEHOLDER_RE,
    loose_match_body,
    placeholder_candidates,
    placeholder_type,
)
from .types import RepairedPlaceholder, RestoreResult
from .vault.base import Vault, _batch


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
    Bracketed text that merely looks similar (``[Figure 2]``) is left alone,
    and so is a code subscript such as ``scores[email1]``.

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

    @property
    def _pattern(self) -> re.Pattern[str]:
        return LOOSE_PLACEHOLDER_RE if self._tolerant else PLACEHOLDER_RE

    @property
    def _openers(self) -> re.Pattern[str]:
        return _LOOSE_OPENERS_RE if self._tolerant else _EXACT_OPENERS_RE

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
        replacer = _Replacer(self._vault, tolerant=self._tolerant)
        with _batch(self._vault):  # one check of a shared vault for all lookups
            restored = self._pattern.sub(replacer, text)
        return replacer.result(restored)

    def stream(self) -> StreamRestorer:
        """Start restoring text that arrives in pieces; see `StreamRestorer`."""
        return StreamRestorer(self)


class StreamRestorer:
    """Restores placeholders in text that arrives in pieces, such as a stream.

    Feed each piece to `feed`, which returns the restored text that is final
    so far, then call `finish` for the rest. Joined, the pieces returned are
    exactly what `Restorer.restore` gives for the whole text, however the
    text was split. Only text that could still turn into a placeholder is held
    back: at most `MAX_PLACEHOLDER_LENGTH` characters, from a bracket onward.
    """

    def __init__(self, restorer: Restorer) -> None:
        """Start a stream; usually made by `Shield.stream_restorer`."""
        self._vault = restorer._vault
        self._pattern = restorer._pattern
        self._openers = restorer._openers
        self._replacer = _Replacer(restorer._vault, tolerant=restorer._tolerant)
        self._pending = ""  # text received but not yet restored and returned
        self._before = ""  # the character just before it, for the lookbehind
        self._emitted: list[str] = []
        self._finished = False

    def feed(self, text: str) -> str:
        """Add the next piece; return the restored text that is now final.

        Raises:
            TypeError: If ``text`` is not a string.
            ValueError: If the stream was already finished.
        """
        if not isinstance(text, str):
            raise TypeError(f"feed() expects str, got {type(text).__name__}")
        if self._finished:
            raise ValueError("feed() called after finish()")
        self._pending += text
        return self._advance(final=False)

    def finish(self) -> str:
        """End the stream; return the rest of the restored text.

        Raises:
            ValueError: If the stream was already finished.
        """
        if self._finished:
            raise ValueError("finish() called twice")
        out = self._advance(final=True)
        self._finished = True
        return out

    def result(self) -> RestoreResult:
        """Summarize the whole stream, like `Restorer.restore` would.

        Raises:
            ValueError: If the stream isn't finished yet.
        """
        if not self._finished:
            raise ValueError("result() called before finish()")
        return self._replacer.result("".join(self._emitted))

    def _advance(self, *, final: bool) -> str:
        # Search a copy that starts one character early, so the lookbehind
        # sees the character before the pending text.
        text = self._before + self._pending
        start = pos = len(self._before)
        out: list[str] = []
        with _batch(self._vault):
            while True:
                found = self._openers.search(text, pos)
                if found is None:
                    out.append(text[pos:])
                    pos = len(text)
                    break
                opener = found.start()
                match = self._pattern.match(text, opener)
                if match is not None:
                    # Every alternative ends with a closing bracket, so a
                    # match found now can't change as more text arrives.
                    out.append(text[pos:opener])
                    out.append(self._replacer(match))
                    pos = match.end()
                elif final or len(text) - opener >= MAX_PLACEHOLDER_LENGTH:
                    # No match starts here, and none ever will.
                    out.append(text[pos : opener + 1])
                    pos = opener + 1
                else:
                    # A placeholder may still be arriving: hold back from here.
                    out.append(text[pos:opener])
                    pos = opener
                    break
        if pos > start:
            self._before = text[pos - 1]
        self._pending = text[pos:]
        emitted = "".join(out)
        self._emitted.append(emitted)
        return emitted


# The characters a match can start with.
_LOOSE_OPENERS_RE = re.compile(f"[{re.escape(PLACEHOLDER_OPENERS)}]")
_EXACT_OPENERS_RE = re.compile(r"\[")


class _Replacer:
    """The replacement function for `re.sub`, collecting what it did."""

    def __init__(self, vault: Vault, *, tolerant: bool) -> None:
        self._vault = vault
        self._tolerant = tolerant
        self.restored_count = 0
        self._unknown: dict[str, None] = {}
        self._repaired: list[RepairedPlaceholder] = []
        self._known_types: set[str | None] | None = None  # built only if needed

    def __call__(self, match: re.Match[str]) -> str:
        written = match.group(0)
        value = self._vault.get_value(written)
        if value is not None:
            self.restored_count += 1
            return value
        candidates = (
            placeholder_candidates(loose_match_body(match)) if self._tolerant else []
        )
        for candidate in candidates:
            value = self._vault.get_value(candidate)
            if value is not None:
                self.restored_count += 1
                self._repaired.append(RepairedPlaceholder(written, candidate))
                return value
        # Report exact placeholders, and rewritten ones of a type this vault
        # uses ("[person 3]" when only PERSON_1 and _2 exist), but not
        # ordinary bracketed text like "[Figure 2]".
        if PLACEHOLDER_RE.fullmatch(written):
            self._unknown[written] = None
        elif candidates:
            if self._known_types is None:
                self._known_types = {
                    placeholder_type(stored) for stored, _ in self._vault.items()
                }
            if any(placeholder_type(c) in self._known_types for c in candidates):
                self._unknown[written] = None
        return written

    def result(self, text: str) -> RestoreResult:
        return RestoreResult(
            text=text,
            restored_count=self.restored_count,
            warnings=[
                f"Unknown placeholder {placeholder} was left unchanged."
                for placeholder in self._unknown
            ],
            repaired=list(self._repaired),
        )
