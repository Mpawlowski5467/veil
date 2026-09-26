"""Normalizers: decide when two differently written values are the same value.

A normalizer takes a value and returns a *key*, or ``None``. Two values of the
same entity type with the same key get the same placeholder when a `Shield` is
created with ``normalize=True``. ``None`` means "match this value exactly": a
normalizer only returns a key when the value is clearly nothing but one value
of its type, as the built-in detector for that type would match it in full.
So a custom ``PHONE`` pattern that captures ``"Tel: 555-555-0123"`` is never
merged with ``"555-555-0123"``.

Keys are internal. They are never stored, logged, or put in warnings.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TypeAlias

from .detectors.regex import (
    _CARD_SEPARATORS,
    _EXTENSION,
    _GROUP_SPACES,
    EMAIL_PATTERN,
    INTL_PHONE_PATTERN,
    US_PHONE_PATTERN,
    _intl_phone_end,
)
from .placeholders import placeholder_type
from .types import Span
from .vault.base import Vault

#: A normalizer maps a value to its key, or to ``None`` to match it exactly.
Normalizer: TypeAlias = Callable[[str], str | None]

_GLUE = frozenset("=&/!#$^~")
_TRAILING_EXTENSION = re.compile(_EXTENSION + r"\Z")
_NON_DIGITS = re.compile(r"[^0-9]+")


def _decimal_digits(text: str) -> str:
    """Return the digits in ``text`` as ASCII.

    The phone patterns match any decimal digit, fullwidth and Arabic-Indic
    included, so those must count by their value rather than be dropped.
    """
    return "".join(str(int(ch)) for ch in text if ch.isdecimal())


def normalize_email(value: str) -> str | None:
    """Key an email address by its lower-cased, NFC-normalized form.

    ``Jan.N@Example.com`` and ``jan.n@example.com`` share a key. Plus tags
    (``jan+news@``) and dots in the local part are kept: those are different
    mailboxes on most servers. ``str.lower`` is used, not ``casefold``, so
    ``straße@`` and ``strasse@`` stay distinct.
    """
    if EMAIL_PATTERN.fullmatch(value) is None:
        return None
    # The detector glues "key=", "a&b=" and "path/" onto an address; a key
    # would let restore() rewrite that text's case. Match those exactly.
    if any(ch in _GLUE for ch in value[: value.rindex("@")]):
        return None
    # Letters whose case doesn't round-trip (the Kelvin sign, dotted capital
    # I, titlecase digraphs) look like other letters: match them exactly.
    if any(ch.lower() != ch and ch.lower().upper() != ch for ch in value):
        return None
    return unicodedata.normalize("NFC", value.lower())


def normalize_phone(value: str) -> str | None:
    """Key a phone number by its E.164 digits and extension.

    ``(555) 555-0123``, ``555.555.0123``, ``1-555-555-0123`` and
    ``+1 555 555 0123`` share a key, and so do ``+44 20 7946 0958``,
    ``+44 (0)20 7946 0958`` and ``+442079460958``. A number without a ``+`` is
    read as North American, as the built-in detector does. An extension is
    part of the key (``ext. 12`` and ``x12`` match; no extension doesn't).
    """
    extension = ""
    base = value
    tail = _TRAILING_EXTENSION.search(value)
    if tail is not None:
        extension = ";ext=" + _decimal_digits(tail.group(0))
        base = value[: tail.start()]
    if US_PHONE_PATTERN.fullmatch(value) is not None:
        digits = _decimal_digits(base)
        if base.startswith("+") or len(digits) == 11:
            return "+" + digits + extension
        return "+1" + digits + extension
    match = INTL_PHONE_PATTERN.match(value)
    if match is None or _intl_phone_end(match) != len(value):
        return None
    # A "(0)" trunk prefix is not dialled from abroad: "+44 (0)20" is "+44 20".
    digits = _decimal_digits(base.replace("(0)", ""))
    return "+" + digits + extension


_IPV6_CHARS = re.compile(r"[0-9A-Fa-f:.]+")


def normalize_ipv6(value: str) -> str | None:
    """Key an IPv6 address by its 128-bit value.

    ``2001:DB8::1`` and ``2001:0db8:0:0:0:0:0:1`` share a key.
    """
    if _IPV6_CHARS.fullmatch(value) is None:
        return None
    try:
        address = ipaddress.IPv6Address(value)
    except ValueError:
        return None
    return f"{int(address):032x}"


_CARD_SEP = "[" + re.escape(_CARD_SEPARATORS) + "]"
_CARD_CHARS = re.compile(f"[0-9]+(?:{_CARD_SEP}[0-9]+)*")


def normalize_card(value: str) -> str | None:
    """Key a card number by its digits, ignoring spaces and dashes."""
    if _CARD_CHARS.fullmatch(value) is None:
        return None
    digits = _NON_DIGITS.sub("", value)
    return digits if 12 <= len(digits) <= 19 else None


_IBAN_SPACE = "[" + re.escape(_GROUP_SPACES) + "]"
_IBAN_CHARS = re.compile(
    f"[A-Za-z]{{2}}[0-9]{{2}}[A-Za-z0-9]*(?:{_IBAN_SPACE}[A-Za-z0-9]+)*"
)
_IBAN_SPACES = re.compile(_IBAN_SPACE)


def normalize_iban(value: str) -> str | None:
    """Key an IBAN by its compact upper-case form, ignoring group spaces."""
    if _IBAN_CHARS.fullmatch(value) is None:
        return None
    compact = _IBAN_SPACES.sub("", value).upper()
    return compact if 15 <= len(compact) <= 34 else None


#: The normalizers `Shield(normalize=True)` uses, by entity type. ``IPV4`` has
#: none: the built-in detector only accepts the one canonical spelling.
BUILTIN_NORMALIZERS: Mapping[str, Normalizer] = MappingProxyType(
    {
        "EMAIL": normalize_email,
        "PHONE": normalize_phone,
        "IPV6": normalize_ipv6,
        "CREDIT_CARD": normalize_card,
        "IBAN": normalize_iban,
    }
)


#: An entity type and a key: values with the same one share a placeholder.
TypeKey: TypeAlias = tuple[str, str]


class KeyIndex:
    """Finds the stored value a new spelling should share a placeholder with.

    Maps ``(entity type, key)`` to the placeholder of the first stored value
    with that key. The index is derived from the vault and kept between calls
    while ``len(vault)`` is what it was when the index was last in sync; every
    hit is checked against the vault, so a stale index can only miss a merge
    (giving a new placeholder), never make a wrong one.
    """

    def __init__(self, normalizers: Mapping[str, Normalizer]) -> None:
        """Create an index for the given normalizers, by entity type."""
        self._normalizers = dict(normalizers)
        self.clear()

    def clear(self) -> None:
        """Forget everything cached."""
        # placeholder -> (value stored under it, its type key or None)
        self._keys: dict[str, tuple[str, TypeKey | None]] = {}
        self._index: dict[TypeKey, str] | None = None
        self._synced = -1  # len(vault) when the index was last complete

    def type_key(self, entity_type: str, value: str) -> TypeKey | None:
        """Return ``value``'s key for ``entity_type``, or None to match exactly."""
        normalizer = self._normalizers.get(entity_type)
        key = normalizer(value) if normalizer is not None else None
        return None if key is None else (entity_type, key)

    def placeholder_for(self, vault: Vault, span: Span) -> str:
        """Return the placeholder for ``span``'s value.

        In order: the value's own placeholder; the placeholder of a stored
        value of the same type with the same key; a new placeholder.
        """
        placeholder = vault.get_placeholder(span.value)
        if placeholder is not None:
            return placeholder
        type_key = self.type_key(span.entity_type, span.value)
        if type_key is not None:
            shared = self._lookup(vault, type_key)
            if shared is not None:
                return shared
        return self.create(vault, span, type_key)

    def create(self, vault: Vault, span: Span, type_key: TypeKey | None) -> str:
        """Store ``span``'s value; index it under ``type_key`` (None: never)."""
        before = len(vault)
        placeholder = vault.get_or_create(span.value, span.entity_type)
        # Record the key this masker chose, even before the index exists: a
        # rebuild must not compute one for a merged value it stored as None.
        self._keys[placeholder] = (span.value, type_key)
        if self._index is not None:
            if self._synced == before and len(vault) == before + 1:
                self._synced += 1
                if type_key is not None:
                    self._index.setdefault(type_key, placeholder)
            elif type_key is None:
                self._index = None  # it may list this value under a key
        return placeholder

    def _lookup(self, vault: Vault, type_key: TypeKey) -> str | None:
        index = self._index
        if index is None or len(vault) != self._synced:
            index = self._rebuild(vault)
        placeholder = index.get(type_key)
        if placeholder is None:
            return None
        entry = self._keys.get(placeholder)
        if (
            entry is not None
            and entry[1] == type_key
            and vault.get_value(placeholder) == entry[0]
        ):
            return placeholder
        # The vault changed without changing size (cleared and refilled).
        return self._rebuild(vault).get(type_key)

    def _rebuild(self, vault: Vault) -> dict[TypeKey, str]:
        keys = self._keys
        index: dict[TypeKey, str] = {}
        items = vault.items()
        for placeholder, value in items:
            entry = keys.get(placeholder)
            if entry is None or entry[0] != value:
                entity_type = placeholder_type(placeholder)
                type_key = self.type_key(entity_type, value) if entity_type else None
                entry = keys[placeholder] = (value, type_key)
            if entry[1] is not None:
                index.setdefault(entry[1], placeholder)
        self._index = index
        self._synced = len(items)
        return index
