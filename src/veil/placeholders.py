"""The placeholder format: ``[TYPE_N]``.

Everything that creates or recognizes placeholders goes through this module, so
the format is defined in exactly one place.
"""

from __future__ import annotations

import re

#: Entity types are upper-case ASCII letters, digits, and underscores, and start
#: with a letter: ``EMAIL``, ``IPV4``, ``ORDER_ID``.
ENTITY_TYPE_RE = re.compile(r"[A-Z][A-Z0-9_]*")

#: Matches anything shaped like a placeholder. The type group is greedy and the
#: number is the digits after the *last* underscore, so ``[ORDER_ID_12]`` parses
#: as type ``ORDER_ID``, number ``12``. The closing bracket is part of the match,
#: which is what keeps ``[PERSON_1]`` from matching inside ``[PERSON_10]``.
PLACEHOLDER_RE = re.compile(r"\[(?P<type>[A-Z][A-Z0-9_]*)_(?P<number>\d+)\]")

#: Bracketed text that may be a placeholder the model rewrote: any case,
#: spaces or dashes in place of the underscore, padding inside the brackets,
#: fullwidth or lenticular brackets (common in CJK output), and brackets
#: escaped for Markdown. The body can't contain brackets, so an exact
#: placeholder is never hidden inside a longer match.
LOOSE_PLACEHOLDER_RE = re.compile(
    r"\x5c?[\[\N{FULLWIDTH LEFT SQUARE BRACKET}\N{LEFT BLACK LENTICULAR BRACKET}]"
    r"[ \t]*(?P<body>[A-Za-z][A-Za-z0-9 \t_-]{0,40}?[0-9]{1,6})[ \t]*"
    r"\x5c?[\]\N{FULLWIDTH RIGHT SQUARE BRACKET}\N{RIGHT BLACK LENTICULAR BRACKET}]"
)

_LOOSE_SEPARATORS_RE = re.compile(r"[ \t_-]+")


def validate_entity_type(entity_type: str) -> str:
    """Return ``entity_type`` unchanged if it is a valid type name.

    Args:
        entity_type: Candidate type name, e.g. ``"PERSON"``.

    Returns:
        The same string, for convenient inline use.

    Raises:
        ValueError: If the name is not upper-case letters, digits, and
            underscores starting with a letter.
    """
    if not isinstance(entity_type, str) or not ENTITY_TYPE_RE.fullmatch(entity_type):
        raise ValueError(
            f"Invalid entity type {entity_type!r}: use upper-case letters, digits, "
            "and underscores, starting with a letter (e.g. 'PERSON', 'ORDER_ID')."
        )
    return entity_type


def format_placeholder(entity_type: str, number: int) -> str:
    """Build the placeholder for the ``number``-th value of ``entity_type``.

    Args:
        entity_type: A valid entity type name.
        number: Per-type sequence number, starting at 1.

    Returns:
        A placeholder such as ``"[EMAIL_2]"``.

    Raises:
        ValueError: If the type is invalid or ``number`` is less than 1.
    """
    validate_entity_type(entity_type)
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError(f"Placeholder number must be an int >= 1, got {number!r}")
    return f"[{entity_type}_{number}]"


def placeholder_candidates(body: str) -> list[str]:
    """List the exact placeholders a loosely written one could stand for.

    ``body`` is what `LOOSE_PLACEHOLDER_RE` found between the brackets. Case,
    spacing, dashes, and leading zeros are normalized: ``"person 01"`` gives
    ``["[PERSON_1]"]``. If nothing separates the type from the number, every
    split is a candidate, longest type first: ``"ipv41"`` gives
    ``["[IPV4_1]", "[IPV_41]"]``.

    Args:
        body: The text inside the brackets.

    Returns:
        Candidate placeholders, most likely first. May be empty.
    """
    normalized = _LOOSE_SEPARATORS_RE.sub("_", body.strip()).upper()
    digits_start = len(normalized)
    while digits_start > 0 and normalized[digits_start - 1].isdigit():
        digits_start -= 1
    if digits_start > 0 and normalized[digits_start - 1] == "_":
        splits: range | list[int] = [digits_start]
    else:
        splits = range(len(normalized) - 1, digits_start - 1, -1)
    candidates: list[str] = []
    for split in splits:
        entity_type = normalized[:split].rstrip("_")
        number = int(normalized[split:])
        if number >= 1 and ENTITY_TYPE_RE.fullmatch(entity_type):
            candidates.append(format_placeholder(entity_type, number))
    return candidates
