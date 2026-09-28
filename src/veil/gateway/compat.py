"""Which Claude Code release the gateway was checked against, and what to say.

When the gateway refuses a request, or fails on one, the user sees its message
in Claude Code as ``API Error: ...``. The messages built here say what
happened, whether anything was sent, and what to do: update this package,
update Claude Code, or ``/rewind`` past content that is part of the
conversation. They name paths and problems, never a value from a request.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .. import __version__
from .config import APP

#: The newest Claude Code release the request rules were checked against.
#: ``tests/live/census.py`` moves it forward after a clean run.
TESTED_CLAUDE_CODE = "2.1.283"

_VERSION = r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}"
_USER_AGENT = re.compile(
    rf"(?<![A-Za-z0-9_./-])claude-cli/({_VERSION})(?![0-9A-Za-z.])", re.ASCII
)
_CLI_OUTPUT = re.compile(rf"[ \t]*({_VERSION})(?![0-9A-Za-z.])", re.ASCII)

#: At most this many distinct problems are listed in one message.
MAX_LISTED = 50


def version_tuple(version: str) -> tuple[int, ...]:
    """``"2.1.283"`` as ``(2, 1, 283)``, for comparing."""
    return tuple(int(part) for part in version.split("."))


def from_user_agent(value: str | None) -> str | None:
    """The Claude Code version in a User-Agent (``claude-cli/X.Y.Z (...)``)."""
    found = _USER_AGENT.search(value or "")
    return found[1] if found else None


def from_cli_output(text: str) -> str | None:
    """The version ``claude --version`` printed (``2.1.283 (Claude Code)``)."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    found = _CLI_OUTPUT.match(first)
    return found[1] if found else None


def compare(version: str | None, tested: str = TESTED_CLAUDE_CODE) -> int | None:
    """1 if ``version`` is newer than ``tested``, -1 if older, 0 if the same.

    None if the version isn't known.
    """
    if version is None:
        return None
    mine, theirs = version_tuple(version), version_tuple(tested)
    return (mine > theirs) - (mine < theirs)


def version_advice(client: str | None) -> str:
    """What to update, given the Claude Code version that sent the request."""
    own = f"{APP} {__version__}"
    tested = TESTED_CLAUDE_CODE
    order = compare(client)
    if order is None:
        return f"{own} was tested with Claude Code {tested}: update {APP}."
    if order > 0:
        return (
            f"This is Claude Code {client}, and {own} was tested with {tested}: "
            f"update {APP}."
        )
    if order < 0:
        return (
            f"This is Claude Code {client}, and {own} was tested with {tested}: "
            f"update Claude Code, or {APP}."
        )
    return (
        f"{own} was tested with this Claude Code ({client}): update {APP}, or "
        "report this if it is up to date."
    )


def _listed(problems: Sequence[tuple[str, str, int]]) -> str:
    items = [
        f"{path} ({problem})" + (f" {count} times" if count > 1 else "")
        for path, problem, count in problems[:MAX_LISTED]
    ]
    if len(problems) > MAX_LISTED:
        items.append(f"and {len(problems) - MAX_LISTED} more")
    return "; ".join(items)


def in_conversation(problems: Sequence[tuple[str, str, int]]) -> bool:
    """Whether every problem is in the conversation's messages.

    Only then can ``/rewind`` help: the rest (tools, system prompt, settings)
    is sent with every request.
    """
    return all(path.startswith("messages[") for path, _, _ in problems)


def refusal_message(
    problems: Sequence[tuple[str, str, int]], client: str | None
) -> str:
    """The one-line message for a request that couldn't be masked.

    The advice comes first: Claude Code shows the start of a long message.
    """
    parts = [
        f"{APP}: can't mask this request, so nothing was sent.",
        version_advice(client),
    ]
    if in_conversation(problems):
        parts.append(
            "If it happens on every prompt, it is in the conversation: /rewind "
            "to before the prompt that brought it in, or start a new one."
        )
    else:
        parts.append("It is part of every request, so /rewind won't help.")
    parts.append(f"Not handled: {_listed(problems)}")
    return " ".join(" ".join(parts).split())


def failure_message(what: str, client: str | None, *, sent: bool = False) -> str:
    """The one-line message for a failure of the gateway's own (a bug)."""
    where = "the masked request reached the API" if sent else "so nothing was sent"
    text = f"{APP}: {what} (a bug), {where}. {version_advice(client)}"
    return " ".join(text.split())


def summary(
    requests: int, problems: Sequence[tuple[str, str, int]], client: str | None
) -> list[str]:
    """Lines to print when the client exits, if requests were refused."""
    counted = (
        "1 request couldn't be masked, so it wasn't sent"
        if requests == 1
        else f"{requests} requests couldn't be masked, so they weren't sent"
    )
    lines = [f"{APP}: {counted}. Not handled:"]
    lines += [
        f"  {path} ({problem})" + (f", {count} times" if count > 1 else "")
        for path, problem, count in problems[:MAX_LISTED]
    ]
    if len(problems) > MAX_LISTED:
        lines.append(f"  and {len(problems) - MAX_LISTED} more")
    lines.append(f"{APP}: {version_advice(client)}")
    return lines
