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
