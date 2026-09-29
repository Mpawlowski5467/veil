"""Check restored OpenAI tool inputs before any executable input is delivered."""

from __future__ import annotations

from typing import Any

from ..placeholders import placeholder_type
from ..shield import Shield
from .hooks import count_values
from .response import StreamError

# These are Codex's direct local patch tools, not arbitrary MCP names. Code
# wrappers and shell commands are deliberately not exempt: their destinations
# cannot be established by inspecting a string.
_LOCAL_PATCH_TOOLS = frozenset({"apply_patch", "functions.apply_patch"})


class PrivateToolError(StreamError):
    """A returned tool input contains private values outside the allowed tools."""


def check_tool(shield: Shield, name: str | None, restored: Any) -> None:
    """Refuse private values in tools other than direct local patch calls.

    This checks the model's returned arguments, not local execution or network
    access. A command that reads a file can still transmit its contents without
    naming any private value in its arguments.
    """
    if name in _LOCAL_PATCH_TOOLS:
        return
    values = (
        value
        for placeholder, value in shield.vault.items()
        if placeholder_type(placeholder) != "LITERAL"
    )
    if count_values(restored, values):
        raise PrivateToolError(
            "Veil blocked a tool input containing private values; "
            "only direct local apply_patch calls can receive them"
        )
