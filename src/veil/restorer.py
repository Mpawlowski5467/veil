"""Restoring: put original values back in place of placeholders."""

from __future__ import annotations

import re

from .placeholders import (
    LOOSE_PLACEHOLDER_RE,
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

    def restore(self, text: str, *, tolerant: bool | None = None) -> RestoreResult:
        """Restore every known placeholder in ``text``.

        Args:
            text: Text that may contain placeholders, usually a model reply.
            tolerant: Override the restorer's ``tolerant`` setting for this
                call, e.g. ``False`` to restore only exact placeholders in text
                that will be written somewhere, not just read.

        Returns:
            The restored text, the number of placeholder occurrences replaced,
            a warning for each distinct unknown placeholder, and the rewritten
            placeholders that were repaired.

        Raises:
            TypeError: If ``text`` is not a string.
        """
        if not isinstance(text, str):
            raise TypeError(f"restore() expects str, got {type(text).__name__}")
        tolerant = self._resolve(tolerant)
        replacer = _Replacer(self._vault, tolerant=tolerant)
        with _batch(self._vault):  # one check of a shared vault for all lookups
            restored = _pattern(tolerant).sub(replacer, text)
        return replacer.result(restored)

    def stream(self, *, tolerant: bool | None = None) -> StreamRestorer:
        """Start restoring text that arrives in pieces; see `StreamRestorer`.

        Args:
            tolerant: Override the restorer's ``tolerant`` setting.
        """
        return StreamRestorer(self._vault, tolerant=self._resolve(tolerant))

    def _resolve(self, tolerant: bool | None) -> bool:
        if tolerant is None:
            return self._tolerant
        if not isinstance(tolerant, bool):
            raise TypeError(f"tolerant must be a bool, got {type(tolerant).__name__}")
        return tolerant


def _pattern(tolerant: bool) -> re.Pattern[str]:
    return LOOSE_PLACEHOLDER_RE if tolerant else PLACEHOLDER_RE


class StreamRestorer:
    """Restores placeholders in text that arrives in pieces, such as a stream.

    Feed each piece to `feed`, which returns the restored text that is final
    so far, then call `finish` for the rest. Joined, the pieces returned are
    exactly what `Restorer.restore` gives for the whole text, however the
    text was split. Only text that could still turn into a placeholder is held
    back: text from a bracket onward that could still become a placeholder,
    always shorter than `MAX_PLACEHOLDER_LENGTH`.
    """

    def __init__(self, vault: Vault, *, tolerant: bool = True) -> None:
        """Start a stream; usually made by `Shield.stream_restorer`.

        Args:
            vault: Where placeholders are looked up.
            tolerant: Also restore placeholders the model rewrote, as
                `Restorer` does.
        """
        self._vault = vault
        self._pattern = _pattern(tolerant)
        self._openers = _LOOSE_OPENERS_RE if tolerant else _EXACT_OPENERS_RE
        self._prefix = _LOOSE_PREFIX_RE if tolerant else _EXACT_PREFIX_RE
        self._replacer = _Replacer(vault, tolerant=tolerant)
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

    @property
    def held(self) -> int:
        """How many characters received so far are held back, not yet returned."""
        return len(self._pending)

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
                elif final or not self._prefix.fullmatch(text, opener):
                    # No match starts here, and no text still to come can
                    # make one: what follows can't begin a placeholder.
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

# Text that may be the start of a match, if more arrives: a superset of the
# prefixes of every match (an exact placeholder's "[", type, "_", and digits;
# a rewritten form's opening bracket and allowed body characters), and never
# longer than MAX_PLACEHOLDER_LENGTH - 1, so a stream holds back less than that.
_EXACT_PREFIX_RE = re.compile(r"\[(?:[A-Z][A-Z0-9_]{0,73})?")
_LOOSE_PREFIX_RE = re.compile(
    r"\[(?:[A-Z][A-Z0-9_]{0,73})?"
    r"|\x5c(?:\[[A-Za-z0-9 \t_-]{0,63}\x5c?)?"
    r"|[\[\N{FULLWIDTH LEFT SQUARE BRACKET}\N{LEFT BLACK LENTICULAR BRACKET}]"
    r"[A-Za-z0-9 \t_-]{0,63}"
)


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
