"""What Claude Code sends through the masking gateway, checked after an update.

Run from the repository root; it makes real model calls (about 80-90 on four
models, one scenario at a time):

    uv run python -m tests.live.census [--models haiku] [--update] ...

Each scenario runs Claude Code through a chain: a front `Recorder` (what
Claude Code sent, as it sent it), the gateway as ``veil claude`` runs it, and
an upstream `Recorder` (what left the machine). The report lists what is new
against the census in ``tests/gateway_payloads/`` (endpoints, headers, betas,
tool names, body paths and kinds) and every failure: a request the gateway
refused or would refuse, a real value in what left, a reply key the gateway
dropped, a count that doesn't add up. Failures of the harness itself (a
timeout, a dialog) are reported apart.

With ``--update``, a run without failures refreshes the census files and the
protocol words (``vocab.py``); new paths go in only with ``--accept-new``.
Once the run covers ``-p`` and an interactive session, clean, on all four
models, the tested Claude Code version moves forward too (never back).

The run folder holds full bodies, the account email Claude Code adds, the
gateway's secret and screen captures: it is private (0700), deleted after a
clean run unless ``--keep``, and kept for ``--from`` / ``--resume`` otherwise.

Exit codes: 0 clean, 1 something new, 2 a failure, 3 a setup problem, 130
interrupted.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import harness as h
from .recorder import Recorder, census
from .terminal import Terminal, end_processes, fold

ROOT = Path(__file__).resolve().parents[2]
PAYLOADS = ROOT / "tests" / "gateway_payloads"
COMPAT = ROOT / "src" / "veil" / "gateway" / "compat.py"
VOCAB = ROOT / "src" / "veil" / "gateway" / "vocab.py"
README = ROOT / "README.md"

#: Labels and what ``--model`` gets. Fable 5.1 is pinned: its alias may move.
MODELS = {
    "haiku": "haiku",
    "sonnet": "sonnet",
    "opus": "opus",
    "fable": "claude-fable-5-1",
}
CORE = ("print", "compact", "interactive")
EXTRAS = ("schema", "mcp", "media", "web", "auto", "refusal")
# Scenarios that need auto mode, which Claude Code doesn't use on Haiku.
AUTO_SCENARIOS = ("auto", "refusal")
MESSAGES = "POST /v1/messages"
SECRET = "x-gateway-secret"
# What the front records may answer besides a success.
EXPECTED_STATUSES = {"HEAD /api/hello": {200}}

# --- Fictional data ----------------------------------------------------------

NAME = "Jan Nowak"
OTHER_NAME = "Ada Quill"
EMAIL = "jane.doe@example.com"
OTHER_EMAIL = "ada.q@example.com"
PHONE = "(555) 555-0100"
REAL_VALUES = (NAME, OTHER_NAME, EMAIL, OTHER_EMAIL, PHONE)
# Parts of the fictional values: never a protocol word.
_FICTIONAL_PARTS = ("jan", "nowak", "ada", "quill", "jane", "doe", "555")
NOTES = f"Name: {NAME}\nEmail: {EMAIL}\nPhone: {PHONE}\n"
CARD = f"Contact {OTHER_NAME} at {OTHER_EMAIL} about the invoice.\n"
CONFIG = {"entities": {"PERSON": [NAME, OTHER_NAME]}, "identity": False}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# --- Prompts -----------------------------------------------------------------

_SUBAGENT = (
    "Use the Agent tool with the general-purpose subagent, run_in_background "
    'false, description "fg read", prompt "Read notes.txt and reply with its '
    'Email line only".'
)
PRINT_PROMPT = (
    f"My name is {NAME}. Also see @card.txt.\n"
    "Do these steps in order, one tool call per step:\n"
    "1. Use the Read tool to read notes.txt.\n"
    "2. Use the Bash tool to run exactly: cat notes.txt; exit 1\n"
    f"3. {_SUBAGENT}\n"
    "4. Reply with the single word DONE."
)
# One line, and no @ (it opens the file picker).
TURN_PROMPT = (
    f"My name is {NAME}. Do these steps in order, one tool call per step: "
    "1. Use the Read tool to read notes.txt. "
    f"2. {_SUBAGENT} "
    "3. Reply with the single word DONE."
)
OK_PROMPT = "Reply with the single word OK."
SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "email": {
                "type": "string",
                "description": f"The contact email address, for example {OTHER_EMAIL}",
            }
        },
        "required": ["email"],
    }
)
EXTRA_PROMPTS = {
    "schema": "Use the Read tool to read notes.txt, then give the email address in it.",
    "mcp": (
        "Use the ToolSearch tool to load the find_contact tool, then call "
        'find_contact with the name "Quill", then reply with the email address '
        "it returned."
    ),
    "media": (
        "Use the Read tool to read pixel.png, then the Read tool to read "
        "doc.pdf, then reply with the single word DONE."
    ),
    "web": (
        'Use the WebSearch tool once to search for "IANA example domain", then '
        "use the WebFetch tool on https://example.com and ask for the page "
        "title, then reply with the single word DONE."
    ),
    "auto": (
        "Use the Bash tool to run exactly: echo hello. Then reply with the "
        "single word DONE."
    ),
    "refusal": (
        "Use the Bash tool to run exactly: echo hello. Then reply with the "
        "single word DONE."
    ),
}
EXTRA_TOOLS = {
    "schema": ["Read"],
    "mcp": ["ToolSearch", "mcp__contacts__find_contact"],
    "media": ["Read"],
    "web": ["WebSearch", "WebFetch", "ToolSearch"],
    "auto": ["Bash"],
    "refusal": ["Bash"],
}
# Where the refusal check plants content the gateway must refuse for good: an
# opaque value under a field it has no rule for.
PLANTED = "census_planted"
PLANTED_VALUE = "Q2Vuc3VzIHBsYW50ZWQgdmFsdWU" * 3

# --- Screen markers (Claude Code 2.1.283; update after UI changes) -----------

TRUST = ("Quick safety check", "Yes, I trust this folder")
TRUST_YES = "Yes, I trust this folder"
DIALOG = ("Do you want to proceed", "Press Enter to continue")
ERROR = "API Error"
READY = "? for shortcuts"
#: Seconds without a change before a turn counts as done.
QUIET = 3.0


class HarnessError(RuntimeError):
    """The harness couldn't drive Claude Code (a timeout, a dialog): no finding."""


# --- Shapes and what is new --------------------------------------------------


_SEGMENT = re.compile(r"[a-z_][a-z0-9_]{0,39}")


def own_names() -> set[str]:
    """This machine's user name and home folder name, in lower case.

    They are never written to a committed file (four characters or more:
    shorter ones would be found inside ordinary words).
    """
    names = {
        os.environ.get("USER", ""),
        os.environ.get("LOGNAME", ""),
        Path.home().name,
    }
    return {name.lower() for name in names if len(name) >= 4}


def _owned(text: str) -> bool:
    return any(name in text.lower() for name in own_names())


def endpoint(record: Mapping[str, Any]) -> str:
    """``"POST /v1/messages"``: a record's method and path's shape.

    The query is dropped, and a path segment that isn't a plain name (an
    id, say) becomes ``<id>``.
    """
    path = str(record["path"]).split("?")[0]
    shape = "/".join(
        part if not part or (_SEGMENT.fullmatch(part) and not _owned(part)) else "<id>"
        for part in path.split("/")
    )
    return f"{record['method']} {shape}"


def collapse(shape: Mapping[str, list[str]]) -> dict[str, list[str]]:
    """A request shape as the census keeps it: tool schemas are not listed.

    Tool definitions go out as they are, so what is inside their schemas
    doesn't matter to the gateway (StructuredOutput's is masked whole).
    """
    return {
        path: list(kinds)
        for path, kinds in shape.items()
        if not path.startswith("$.tools[].input_schema.") and not _owned(path)
    }


def _others_tool(name: Any) -> bool:
    """A tool whose schema someone else wrote: MCP's, or the user's own."""
    return isinstance(name, str) and (
        name.startswith("mcp__") or name == "StructuredOutput"
    )


def own_inputs(body: Any) -> Any:
    """A body with the input of each call to `_others_tool` emptied.

    Its keys are that schema's, not Claude Code's, so the census doesn't
    list them (a --json-schema property, say).
    """
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return body

    def block(item: Any) -> Any:
        if (
            isinstance(item, dict)
            and item.get("type") == "tool_use"
            and _others_tool(item.get("name"))
        ):
            return {**item, "input": {}}
        return item

    messages = [
        {**m, "content": [block(b) for b in m["content"]]}
        if isinstance(m, dict) and isinstance(m.get("content"), list)
        else m
        for m in body["messages"]
    ]
    return {**body, "messages": messages}


def _ignored(path: str, *, reply: bool) -> bool:
    if reply:
        return path.startswith("$.content_block.input.")
    return path.startswith("$.messages[]") and ".input." in path


def new_paths(
    shape: Mapping[str, Iterable[str]],
    known: Mapping[str, Mapping[str, Any]],
    *,
    reply: bool = False,
) -> dict[str, list[str]]:
    """Paths or kinds in ``shape`` that the census ``known`` doesn't list.

    Tool inputs vary by tool, so paths inside them don't count.
    """
    found = {}
    for path, kinds in shape.items():
        if _ignored(path, reply=reply):
            continue
        new = set(kinds) - set(known.get(path, {}).get("kinds", []))
        if new:
            found[path] = sorted(new)
    return found


Observed = dict[str, dict[str, dict[str, set[str]]]]


def observe(into: Observed, key: str, shape: Mapping[str, Iterable[str]], label: str):
    """Add one body's shape, seen in scenario ``label``, to ``into[key]``."""
    paths = into.setdefault(key, {})
    for path, kinds in shape.items():
        entry = paths.setdefault(path, {"kinds": set(), "scenarios": set()})
        entry["kinds"].update(kinds)
        entry["scenarios"].add(label)


def merge_census(
    old: Mapping[str, Mapping[str, Any]], observed: Observed, *, accept_new: bool
) -> dict[str, dict[str, Any]]:
    """The census with what was observed added; nothing is ever removed.

    A known path gets the new scenario labels. A new path, or a new kind at
    a known one, goes in only with ``accept_new``.
    """
    merged = {
        ep: {path: dict(entry) for path, entry in paths.items()}
        for ep, paths in old.items()
    }
    for ep, paths in observed.items():
        if ep not in merged and not accept_new:
            continue
        target = merged.setdefault(ep, {})
        for path, entry in paths.items():
            known = target.get(path)
            if known is None and not accept_new:
                continue
            known = known or {"kinds": [], "scenarios": []}
            kinds = set(known["kinds"])
            if accept_new:
                kinds |= entry["kinds"]
            target[path] = {
                "kinds": sorted(kinds),
                "scenarios": sorted(set(known["scenarios"]) | entry["scenarios"]),
            }
    return merged


@dataclass
class Endpoints:
    """Endpoints, request headers, betas and tool names seen in a run."""

    endpoints: dict[str, dict[str, Any]] = field(default_factory=dict)
    headers: dict[str, set[str]] = field(default_factory=dict)
    betas: set[str] = field(default_factory=set)
    tools: set[str] = field(default_factory=set)

    def add(self, record: Mapping[str, Any], body: Any, label: str) -> None:
        """Add one front record (what Claude Code sent).

        The hooks' requests to the gateway itself (``/_gateway/...``) never
        go further, so they aren't listed.
        """
        ep = endpoint(record)
        if str(record["path"]).startswith("/_gateway/"):
            return
        entry = self.endpoints.setdefault(
            ep,
            {
                "count": 0,
                "response_types": set(),
                "scenarios": set(),
                "statuses": set(),
            },
        )
        entry["count"] += 1
        entry["scenarios"].add(label)
        if record.get("response_type"):
            entry["response_types"].add(str(record["response_type"]).split(";")[0])
        if isinstance(record.get("status"), int):
            entry["statuses"].add(record["status"])
        for name in record.get("header_names", []):
            if name != SECRET and _HEADER.fullmatch(name) and not _owned(name):
                self.headers.setdefault(name, set()).add(label)
        self.betas |= betas(record.get("headers", {}).get("anthropic-beta", ""))
        self.tools |= tool_names(body)


_BETA = re.compile(r"[a-z0-9][a-z0-9.-]{0,79}")
_HEADER = re.compile(r"[a-z0-9-]{1,64}")
_TOOL = re.compile(r"[A-Za-z0-9_-]{1,128}")


def betas(value: str) -> set[str]:
    """The beta names in an ``anthropic-beta`` header."""
    return {
        b for b in (part.strip() for part in value.split(",")) if _BETA.fullmatch(b)
    }


def tool_names(body: Any) -> set[str]:
    """The names of the tools a body defines, but MCP tools and StructuredOutput."""
    tools = body.get("tools") if isinstance(body, dict) else None
    if not isinstance(tools, list):
        return set()
    return {
        str(tool["name"])
        for tool in tools
        if isinstance(tool, dict)
        and isinstance(tool.get("name"), str)
        and _TOOL.fullmatch(tool["name"])
        and not tool["name"].startswith("mcp__")
        and tool["name"] != "StructuredOutput"
    }


def new_endpoint_things(seen: Endpoints, known: Mapping[str, Any]) -> list[str]:
    """What ``seen`` has that ``endpoints.json`` doesn't: one line each."""
    lines = [
        f"endpoint {ep}" for ep in sorted(set(seen.endpoints) - set(known["endpoints"]))
    ]
    lines += [
        f"request header {name}"
        for name in sorted(set(seen.headers) - set(known["request_headers"]))
    ]
    lines += [f"beta {b}" for b in sorted(seen.betas - set(known["anthropic_beta"]))]
    lines += [f"tool {t}" for t in sorted(seen.tools - set(known.get("tools", [])))]
    return lines


def merge_endpoints(
    old: Mapping[str, Any], seen: Endpoints, *, accept_new: bool
) -> dict[str, Any]:
    """``endpoints.json`` with ``seen`` added (new entries only with ``accept_new``)."""
    endpoints = {ep: dict(entry) for ep, entry in old["endpoints"].items()}
    for ep, entry in seen.endpoints.items():
        known = endpoints.get(ep)
        if known is None and not accept_new:
            continue
        known = known or {
            "count": 0,
            "response_types": [],
            "scenarios": [],
            "statuses": [],
        }
        endpoints[ep] = {
            "count": max(known["count"], entry["count"]),
            "response_types": sorted(
                set(known["response_types"]) | entry["response_types"]
            ),
            "scenarios": sorted(set(known["scenarios"]) | entry["scenarios"]),
            "statuses": sorted(set(known["statuses"]) | entry["statuses"]),
        }
    headers = {name: list(labels) for name, labels in old["request_headers"].items()}
    for name, labels in seen.headers.items():
        if name in headers or accept_new:
            headers[name] = sorted(set(headers.get(name, [])) | labels)
    merged: dict[str, Any] = {
        "anthropic_beta": sorted(
            set(old["anthropic_beta"]) | (seen.betas if accept_new else set())
        ),
        "endpoints": endpoints,
        "request_headers": headers,
    }
    tools = set(old.get("tools", [])) | (seen.tools if accept_new else set())
    if tools:
        merged["tools"] = sorted(tools)
    return merged


def dump(value: Any) -> str:
    """The census files' format."""
    return json.dumps(value, indent=1, sort_keys=True) + "\n"


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step."""
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


# --- Protocol words ----------------------------------------------------------

_WORD = re.compile(r"[A-Za-z_$][A-Za-z0-9_$-]{0,63}")


def words(body: Any) -> set[str]:
    """Field and type names in a request body: candidates for ``vocab.WORDS``.

    Tool inputs and the schemas of MCP tools and StructuredOutput hold what
    others wrote, so they are left out; so is anything that looks like data.
    """
    found: set[str] = set()

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                found.add(key)
                if key == "type" and isinstance(item, str):
                    found.add(item)
                if key == "input" and path.startswith("$.messages"):
                    continue
                if key.startswith("by_") and isinstance(item, dict):
                    continue  # keyed by data (a path, a slug): not field names
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for item in value:
                walk(item, f"{path}[]")

    if isinstance(body, dict):
        for key, item in body.items():
            if key == "tools" and isinstance(item, list):
                found.add(key)
                for tool in item:
                    name = tool.get("name") if isinstance(tool, dict) else None
                    if _others_tool(name):
                        continue
                    walk(tool, "$.tools[]")
            else:
                found.add(key)
                walk(item, f"$.{key}")
    own = own_names()
    return {
        word
        for word in found
        if _WORD.fullmatch(word)
        and not any(part in word.lower() for part in (*_FICTIONAL_PARTS, *own))
    }


def vocab_source(old: str, new_words: Iterable[str]) -> str:
    """``vocab.py`` with ``new_words`` added to ``WORDS`` (sorted, none removed)."""
    head, rest = old.split("WORDS = frozenset(\n    {\n", 1)
    body, tail = rest.split("    }\n)", 1)
    current = {
        line.strip().rstrip(",").strip('"')
        for line in body.splitlines()
        if line.strip()
    }
    merged = sorted(current | set(new_words))
    lines = "".join(f"        {json.dumps(word)},\n" for word in merged)
    return f"{head}WORDS = frozenset(\n    {{\n{lines}    }}\n){tail}"


# --- Versions ----------------------------------------------------------------

_VERSION = r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}"
_TESTED = re.compile(rf'^TESTED_CLAUDE_CODE = "({_VERSION})"$', re.MULTILINE)
_README_TESTED = re.compile(rf"It was tested with Claude Code ({_VERSION})\.")
_BILLING = re.compile(rf"\bcc_version=({_VERSION})")


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def billing_versions(body: Any) -> set[str]:
    """The versions in a body's billing lines (``x-anthropic-billing-header``)."""
    system = body.get("system") if isinstance(body, dict) else None
    texts = (
        [system]
        if isinstance(system, str)
        else [b.get("text") for b in system if isinstance(b, dict)]
        if isinstance(system, list)
        else []
    )
    return {
        found[1]
        for text in texts
        if isinstance(text, str) and text.startswith("x-anthropic-billing-header:")
        for found in [_BILLING.search(text)]
        if found
    }


def move_version(text: str, pattern: re.Pattern[str], new: str) -> tuple[str, str]:
    """``text`` with the one version ``pattern`` finds moved up to ``new``.

    Returns the text and why it wasn't changed ("" if it was). Raises
    ValueError unless the pattern matches exactly once.
    """
    found = list(pattern.finditer(text))
    if len(found) != 1:
        raise ValueError(f"expected one tested version, found {len(found)}")
    old = found[0][1]
    if version_key(new) <= version_key(old):
        return text, f"{new} isn't newer than {old}"
    start, end = found[0].span(1)
    return text[:start] + new + text[end:], ""


# --- Checks over bodies ------------------------------------------------------


def leaks(value: Any, path: str = "$") -> Iterator[str]:
    """The JSON path of every string that holds a real value (never the text).

    A real value is one of the fictional values this census plants, or any
    email address not at example.com (such as the account's).
    """
    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaks(key, f"{path}.<key>")
            yield from leaks(
                item, f"{path}.{key}" if _WORD.fullmatch(key) else f"{path}.<key>"
            )
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from leaks(item, f"{path}[{i}]")
    elif isinstance(value, str) and holds_real_value(value):
        yield path


def holds_real_value(text: str) -> bool:
    return any(v in text for v in REAL_VALUES) or any(
        not email.lower().endswith("@example.com") for email in _EMAIL.findall(text)
    )


def shown(line: str) -> str:
    """A report line, or a stand-in if it holds a real value."""
    return "<line withheld>" if holds_real_value(line) else line


def normalized(path: str) -> str:
    """A path with its list indexes dropped: ``messages[].content[]``."""
    return re.sub(r"\[\d+\]", "[]", path)


def settled(
    now: float, *, posts: int, before: int, open_requests: int, changed: float
) -> bool:
    """Whether a turn is done.

    It made a request, none is open, and neither the records nor the screen
    changed for `QUIET` seconds.
    """
    return posts > before and open_requests == 0 and now - changed >= QUIET


def planter() -> Callable[[Any], Any]:
    """Plants, in the first auto-mode request only, content the gateway must
    refuse for good: an opaque value under a field it has no rule for.
    """
    planted = False

    def plant(body: Any) -> Any:
        nonlocal planted
        if planted or not (isinstance(body, dict) and body.get("safeguards")):
            return None
        planted = True
        return {**body, PLANTED: {"signature": PLANTED_VALUE}}

    return plant


# --- Fictional files ---------------------------------------------------------


def png() -> bytes:
    """A one-pixel PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\xff\xff")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


def pdf(text: str = "Invoice 1001") -> bytes:
    """A one-page PDF showing ``text``."""
    content = f"BT /F1 18 Tf 20 40 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, obj)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return out


# --- Running Claude Code through the chain -----------------------------------


@dataclass
class Chain:
    front: Recorder
    gateway: Any
    upstream: Recorder


@contextlib.contextmanager
def chain(
    folder: Path,
    data_dir: Path,
    rewrite: Callable[[Any], Any] | None = None,
    *,
    api: str | None = None,
) -> Iterator[Chain]:
    """Claude Code → front Recorder → the gateway → upstream Recorder → API.

    ``api`` is a plain-HTTP ``host:port`` to use in place of the API (tests).
    """
    from veil.gateway import Gateway, load_settings, open_sessions

    settings = load_settings(data_dir / "config.json")
    recorder = (
        Recorder(folder / "upstream", upstream=api, secure=False)
        if api
        else Recorder(folder / "upstream")
    )
    with recorder as upstream:
        sessions = open_sessions(data_dir, settings, {})
        with (
            Gateway(
                sessions, upstream=upstream.url.removeprefix("http://"), secure=False
            ) as gateway,
            Recorder(
                folder / "front",
                upstream=gateway.url.removeprefix("http://"),
                secure=False,
                rewrite=rewrite,
            ) as front,
        ):
            yield Chain(front, gateway, upstream)


def claude_config(link: Chain, data_dir: Path) -> dict[str, Any]:
    """The settings ``veil claude`` gives Claude Code, pointed at the front Recorder."""
    from veil.cli import claude_settings, hook_command
    from veil.gateway.hooks import hook_settings

    config = claude_settings(link.gateway, data_dir=data_dir)
    config["env"]["ANTHROPIC_BASE_URL"] = link.front.url
    config["env"]["DISABLE_AUTOUPDATER"] = "1"  # the same binary for the whole run
    config["hooks"] = hook_settings(hook_command(data_dir), link.front.url)
    return config


def posts(recorder: Recorder) -> list[dict[str, Any]]:
    """The model requests a Recorder has seen so far."""
    return [
        r
        for r in recorder.records
        if r["method"] == "POST" and "/v1/messages" in r["path"]
    ]


def open_count(recorder: Recorder) -> int:
    return sum(
        1
        for r in recorder.records
        if "response_shape" not in r and "response_kind" not in r
    )


@dataclass
class Outcome:
    """How one scenario went, written to its folder as ``status.json``."""

    label: str
    ok: bool = True
    harness: str = ""
    api_requests: int = 0
    notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def save(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        write_atomic(folder / "status.json", json.dumps(self.__dict__, indent=1))


def run_print(
    ws: h.Workspace,
    link: Chain,
    data_dir: Path,
    model: str,
    prompt: str,
    tools: list[str],
    out: Outcome,
    *,
    max_turns: int = 10,
    extra_args: Iterable[str] = (),
    mcp: bool = False,
    permission_mode: str | None = None,
    resume: str | None = None,
    timeout: float = 300,
    expect_error: bool = False,
) -> h.Run:
    """One ``claude -p`` run through ``link``.

    One that ends in an error is a failure, unless ``expect_error`` (the
    refusal check's) or it only ran out of turns.
    """
    try:
        run = ws.run(
            prompt,
            allowed_tools=tools,
            max_turns=max_turns,
            extra_settings=claude_config(link, data_dir),
            extra_args=list(extra_args),
            mcp=mcp,
            permission_mode=permission_mode,
            resume=resume,
            timeout=timeout,
            model=MODELS.get(model, model),
        )
    except subprocess.TimeoutExpired:
        raise HarnessError(f"claude -p didn't finish in {timeout:.0f} s") from None
    out.api_requests += len(run.api_requests())
    if not run.result:
        out.failures.append(f"claude -p ended without a result (exit {run.returncode})")
    elif run.result.get("is_error") and run.result.get("subtype") != "error_max_turns":
        # The text is Claude Code's or the gateway's, not a value.
        text = str(run.result.get("result", ""))[:300]
        line = shown(f"claude -p reported an error: {text}")
        (out.notes if expect_error else out.failures).append(line)
    return run


class Driver:
    """Drives an interactive Claude Code in a `Terminal` through a turn at a time."""

    def __init__(self, term: Terminal, front: Recorder, turn_timeout: float) -> None:
        self.term = term
        self.front = front
        self.turn_timeout = turn_timeout
        self.last = ""
        self.errors: list[str] = []

    def screen(self) -> str:
        self.last = self.term.snapshot()
        return self.last

    def _state(self) -> tuple[int, int]:
        return len(self.front.records), open_count(self.front)

    def _check_exited(self) -> None:
        if self.term.exited() is not None:
            raise HarnessError(f"Claude Code exited (status {self.term.exited()})")

    def wait_ready(self, timeout: float = 90) -> None:
        """Wait for the trust dialog, and accept it, or for the prompt."""
        deadline = time.monotonic() + timeout
        stable, since = "", time.monotonic()
        while time.monotonic() < deadline:
            self._check_exited()
            screen = self.screen()
            if any(fold(marker) in screen for marker in TRUST):
                self._trust()
                return
            if screen != stable:
                stable, since = screen, time.monotonic()
            elif READY in screen and time.monotonic() - since >= QUIET:
                return
            time.sleep(0.5)
        raise HarnessError("Claude Code didn't get to its prompt")

    def _trust(self) -> None:
        time.sleep(1.5)  # it ignores keys just after it opens
        line = self._line(TRUST_YES)
        self.term.key("down")
        time.sleep(0.5)
        if self._line(TRUST_YES) == line:
            self.term.key("tab")
            time.sleep(0.5)
        self.term.key("enter")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self._check_exited()
            if not any(fold(marker) in self.screen() for marker in TRUST):
                self.wait_ready_after_trust()
                return
            time.sleep(0.5)
        raise HarnessError("the trust dialog stayed open")

    def wait_ready_after_trust(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if READY in self.screen():
                return
            time.sleep(0.5)
        raise HarnessError(
            "Claude Code didn't get to its prompt after the trust dialog"
        )

    def _line(self, marker: str) -> str:
        for line in self.screen().splitlines():
            if fold(marker) in line:
                return line
        return ""

    def turn(self, text: str) -> int:
        """Type ``text``, press Enter, and wait until the turn is done.

        Returns how many requests it made.
        """
        before = len(posts(self.front))
        self.term.type(text)
        time.sleep(0.5)
        if fold(text[:30]) not in self.screen():
            raise HarnessError(f"the text didn't show on screen: {text[:30]!r}")
        time.sleep(0.3)
        self.term.key("enter")
        deadline = time.monotonic() + self.turn_timeout
        started = time.monotonic()
        enter_again = True
        state, screen, changed = self._state(), self.screen(), time.monotonic()
        while True:
            time.sleep(0.5)
            now = time.monotonic()
            self._check_exited()
            current, now_screen = self._state(), self.screen()
            if current != state or now_screen != screen:
                state, screen, changed = current, now_screen, now
            count = len(posts(self.front))
            if (
                enter_again
                and count == before
                and now - started > 30
                and fold(text[:30]) in "\n".join(screen.splitlines()[-8:])
            ):
                self.term.key("enter")  # the first Enter was taken as a paste
                enter_again = False
            if settled(
                now,
                posts=count,
                before=before,
                open_requests=current[1],
                changed=changed,
            ):
                break
            if now > deadline:
                raise HarnessError(f"the turn {text[:30]!r} didn't finish")
        if any(marker in screen for marker in DIALOG):
            tail = "\n".join(screen.rstrip().splitlines()[-15:])
            raise HarnessError(shown(f"a dialog is open after {text[:30]!r}:\n{tail}"))
        for line in screen.splitlines():
            if ERROR in line and line not in self.errors:
                self.errors.append(line.strip())
        return len(posts(self.front)) - before

    def exit(self) -> None:
        self.term.type("/exit")
        time.sleep(0.5)
        self.term.key("enter")
        for _ in range(40):
            if self.term.exited() is not None:
                return
            time.sleep(0.5)
        self.term.key("ctrl-c")
        time.sleep(0.5)
        self.term.key("ctrl-c")
        for _ in range(10):
            if self.term.exited() is not None:
                return
            time.sleep(0.5)


def run_interactive(
    ws: h.Workspace,
    link: Chain,
    data_dir: Path,
    model: str,
    folder: Path,
    out: Outcome,
    turn_timeout: float,
) -> None:
    """An interactive session: a turn with a subagent, /compact, one more turn."""
    settings_file = folder / "settings.json"
    fd = os.open(settings_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(claude_config(link, data_dir), f)
    claude = h.claude_path()
    assert claude is not None
    argv = [
        claude,
        "--model",
        MODELS.get(model, model),
        "--settings",
        str(settings_file),
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--debug-file",
        str(folder / "debug.log"),
        "--allowedTools",
        "Read,Agent",
    ]
    term = Terminal(
        folder / "term", argv, h.environment(), ws.work, name=f"census-{model}"
    )
    driver = Driver(term, link.front, turn_timeout)
    try:
        term.start()
        driver.wait_ready()
        out.counts["turn"] = driver.turn(TURN_PROMPT)
        out.counts["compact"] = driver.turn("/compact")
        out.counts["after_compact"] = driver.turn(OK_PROMPT)
        driver.exit()
    finally:
        term.close()
        (folder / "screen.txt").write_text(driver.last, encoding="utf-8")
    deadline = time.monotonic() + 30
    while open_count(link.front) and time.monotonic() < deadline:
        time.sleep(0.5)
    if open_count(link.front):
        out.failures.append(
            f"{open_count(link.front)} request(s) still open after /exit"
        )
    debug = folder / "debug.log"
    if debug.exists():
        out.api_requests = sum(
            "[API REQUEST]" in line
            for line in debug.read_text(encoding="utf-8").splitlines()
        )
    out.failures += [shown(f"on screen: {line}") for line in driver.errors]
    if out.counts["compact"] == 0:
        out.failures.append("/compact made no request")


def run_scenario(
    run_root: Path, data_dir: Path, model: str, scenario: str, turn_timeout: float
) -> Outcome:
    """Run one scenario in ``run_root / model / scenario``, never raising."""
    label = f"{model}/{scenario}"
    folder = run_root / model / scenario
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    ws = workspace(run_root, model)
    out = Outcome(label)
    rewrite = planter() if scenario == "refusal" else None
    try:
        with chain(folder, data_dir, rewrite) as link:
            if scenario == "print":
                run = run_print(
                    ws,
                    link,
                    data_dir,
                    model,
                    PRINT_PROMPT,
                    ["Read", "Bash", "Agent"],
                    out,
                )
                session = run.session_id or ""
                (run_root / model / "session.txt").write_text(session, encoding="utf-8")
            elif scenario == "compact":
                session_file = run_root / model / "session.txt"
                session = (
                    session_file.read_text(encoding="utf-8")
                    if session_file.exists()
                    else ""
                )
                if not session:
                    raise HarnessError("no print session to resume")
                before = len(posts(link.front))
                run = run_print(
                    ws,
                    link,
                    data_dir,
                    model,
                    "/compact",
                    [],
                    out,
                    max_turns=2,
                    resume=session,
                )
                out.counts["compact"] = len(posts(link.front)) - before
                before = len(posts(link.front))
                run_print(
                    ws,
                    link,
                    data_dir,
                    model,
                    OK_PROMPT,
                    [],
                    out,
                    max_turns=2,
                    resume=run.session_id or session,
                )
                out.counts["after_compact"] = len(posts(link.front)) - before
            elif scenario == "interactive":
                run_interactive(ws, link, data_dir, model, folder, out, turn_timeout)
            elif scenario == "refusal":
                run = run_print(
                    ws,
                    link,
                    data_dir,
                    model,
                    EXTRA_PROMPTS["auto"],
                    EXTRA_TOOLS["auto"],
                    out,
                    permission_mode="auto",
                    expect_error=True,
                )
                (folder / "result.txt").write_text(
                    str(run.result.get("result", "")), encoding="utf-8"
                )
                out.counts["first_run"] = len(posts(link.front))
                run_print(
                    ws,
                    link,
                    data_dir,
                    model,
                    OK_PROMPT,
                    [],
                    out,
                    max_turns=2,
                    permission_mode="auto",
                    resume=run.session_id or None,
                )
            else:
                extra_args: list[str] = []
                if scenario == "schema":
                    extra_args = ["--json-schema", SCHEMA]
                if scenario == "media":
                    (ws.work / "pixel.png").write_bytes(png())
                    (ws.work / "doc.pdf").write_bytes(pdf())
                run_print(
                    ws,
                    link,
                    data_dir,
                    model,
                    EXTRA_PROMPTS[scenario],
                    EXTRA_TOOLS[scenario],
                    out,
                    extra_args=extra_args,
                    mcp=scenario == "mcp",
                    permission_mode="auto" if scenario == "auto" else None,
                    timeout=420 if scenario == "web" else 300,
                )
    except HarnessError as error:
        out.ok = False
        out.harness = shown(str(error))
    except Exception as error:  # a bug in the harness, not a finding
        frame = traceback.extract_tb(error.__traceback__)[-1]
        out.ok = False
        out.harness = (
            f"{type(error).__name__} at {Path(frame.filename).name}:{frame.lineno}"
        )
    out.save(folder)
    return out


def workspace(run_root: Path, model: str) -> h.Workspace:
    """The model's working folder, shared by its scenarios (``--resume`` needs it)."""
    ws = h.Workspace(run_root / model / "ws")
    ws.write("notes.txt", NOTES)
    ws.write("card.txt", CARD)
    return ws


# --- Analysis ----------------------------------------------------------------


@dataclass
class Side:
    """One Recorder's records and bodies."""

    folder: Path
    records: list[dict[str, Any]]

    @classmethod
    def load(cls, folder: Path) -> Side:
        path = folder / "requests.jsonl"
        records = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                with contextlib.suppress(ValueError):
                    records.append(json.loads(line))
        return cls(folder, records)

    def body(self, record: Mapping[str, Any]) -> Any:
        path = self.folder / f"request-{record['id']}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def reply(self, record: Mapping[str, Any]) -> Any:
        path = self.folder / f"response-{record['id']}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def stream(self, record: Mapping[str, Any]) -> str:
        path = self.folder / f"response-{record['id']}.sse"
        return (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )

    def models(self) -> list[dict[str, Any]]:
        return [
            r
            for r in self.records
            if r["method"] == "POST" and "/v1/messages" in r["path"]
        ]


@dataclass
class Capture:
    label: str
    folder: Path
    status: dict[str, Any]
    front: Side
    upstream: Side


def captures(run_root: Path) -> list[Capture]:
    found = []
    for status_file in sorted(run_root.glob("*/*/status.json")):
        folder = status_file.parent
        found.append(
            Capture(
                label=f"{folder.parent.name}/{folder.name}",
                folder=folder,
                status=json.loads(status_file.read_text(encoding="utf-8")),
                front=Side.load(folder / "front"),
                upstream=Side.load(folder / "upstream"),
            )
        )
    return found


@dataclass
class Report:
    new: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    harness: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: set[str] = field(default_factory=set)
    requests: Observed = field(default_factory=dict)
    replies: Observed = field(default_factory=dict)
    endpoints: Endpoints = field(default_factory=Endpoints)
    words: set[str] = field(default_factory=set)
    versions: set[str] = field(default_factory=set)
    clean: set[str] = field(default_factory=set)  # labels that ran clean

    def exit_code(self) -> int:
        if self.failures or self.harness:
            return 2
        return 1 if self.new else 0


def _gateway_refusal(side: Side, record: Mapping[str, Any]) -> str | None:
    """The gateway's message, if it refused this front request."""
    from veil.gateway.config import APP

    status = record.get("status")
    if not isinstance(status, int) or status < 400:
        return None
    reply = side.reply(record)
    error = reply.get("error") if isinstance(reply, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if isinstance(message, str) and message.startswith(f"{APP}:"):
        return message
    return None


def replay(capture: Capture, config: Path, report: Report) -> None:
    """Mask every raw body again, fresh, and report refusals and generic paths."""
    from veil.gateway import UnsupportedRequestError, load_settings, open_sessions
    from veil.gateway.compat import from_user_agent

    refusal_scenario = capture.label.endswith("/refusal")
    with tempfile.TemporaryDirectory(prefix="census-replay-") as temp:
        data = Path(temp)
        data.chmod(0o700)
        shutil.copy(config, data / "config.json")
        with open_sessions(data, load_settings(data / "config.json"), {}) as sessions:
            for record in capture.front.models():
                body = capture.front.body(record)
                if not isinstance(body, dict):
                    continue
                session = record.get("headers", {}).get(
                    "x-claude-code-session-id", "none"
                )
                version = from_user_agent(record.get("headers", {}).get("user-agent"))
                masker = sessions.get(session).masker
                try:
                    masker.mask(body, client_version=version)
                except UnsupportedRequestError as error:
                    for path, problem, _ in error.problems:
                        line = f"{capture.label}: {normalized(path)}: {problem}"
                        if refusal_scenario and path.startswith(PLANTED):
                            continue
                        report.failures.append(shown(f"refused on replay: {line}"))
                    continue
                except Exception as error:  # reported by type and place only
                    frame = traceback.extract_tb(error.__traceback__)[-1]
                    where = f"{Path(frame.filename).name}:{frame.lineno}"
                    report.failures.append(
                        f"failed on replay: {capture.label}: "
                        f"{type(error).__name__} at {where}"
                    )
                    continue
                for path in getattr(masker, "last_generic", ()):
                    report.new.append(
                        shown(
                            f"handled generically: {capture.label}: {normalized(path)}"
                        )
                    )


def analyze(run_root: Path, known: Mapping[str, Any]) -> Report:
    """Everything the run found, against the census ``known``."""
    report = Report()
    config = run_root / "data" / "config.json"
    for capture in captures(run_root):
        check_capture(capture, config, known, report)
    report.new = sorted(set(report.new))
    report.new += new_endpoint_things(report.endpoints, known["endpoints"])
    for ep, paths in report.requests.items():
        shape = {path: entry["kinds"] for path, entry in paths.items()}
        for path, kinds in new_paths(shape, known["requests"].get(ep, {})).items():
            report.new.append(f"request {ep} {path}: {', '.join(kinds)}")
    for ep, paths in report.replies.items():
        shape = {path: entry["kinds"] for path, entry in paths.items()}
        for path, kinds in new_paths(
            shape, known["replies"].get(ep, {}), reply=True
        ).items():
            report.new.append(f"reply {ep} {path}: {', '.join(kinds)}")
    return report


def check_capture(
    capture: Capture, config: Path, known: Mapping[str, Any], report: Report
) -> None:
    from veil.gateway.compat import from_user_agent

    label = capture.label
    status = capture.status
    if status.get("harness"):
        report.harness.append(shown(f"{label}: {status['harness']}"))
        return
    failures_before = len(report.failures)
    report.failures += [f"{label}: {f}" for f in status.get("failures", [])]
    report.warnings += [
        f"{label}: {n}"
        for n in status.get("notes", [])
        if PLANTED not in n  # the refusal check's own, expected
    ]
    front, upstream = capture.front, capture.upstream
    # What Claude Code sent.
    refused = 0
    for record in front.records:
        body = front.body(record)
        if record.get("rewritten") and isinstance(body, dict):
            # The refusal check's planted field isn't Claude Code's.
            body = {key: value for key, value in body.items() if key != PLANTED}
        report.endpoints.add(record, body, label)
        ep = endpoint(record)
        if (
            isinstance(body, dict)
            and record["method"] == "POST"
            and "/v1/messages" in ep
        ):
            observe(report.requests, ep, collapse(census([own_inputs(body)])), label)
            report.words |= words(body)
            report.versions |= billing_versions(body)
            model = body.get("model")
            if isinstance(model, str):
                report.info.add(f"{label} ran on {model}")
        status_code = record.get("status")
        message = _gateway_refusal(front, record)
        if message is not None:
            refused += 1
            if not label.endswith("/refusal"):
                report.failures.append(shown(f"refused live: {label}: {message[:300]}"))
        elif not finished(front, record):
            report.failures.append(
                f"{label}: request {record['id']} ({ep}) never completed"
            )
        elif status_code >= 300 and status_code not in EXPECTED_STATUSES.get(ep, set()):
            report.failures.append(f"{label}: {ep} answered {status_code}")
        if "event: error" in front.stream(record):
            report.failures.append(
                f"{label}: the reply to request {record['id']} ended with an error"
            )
        ua = from_user_agent(record.get("headers", {}).get("user-agent"))
        if ua:
            report.versions.add(ua)
    # What left the machine.
    for record in upstream.models():
        body = upstream.body(record)
        for path in leaks(body):
            report.failures.append(f"leak: {label}: {normalized(path)}")
        status_code = record.get("status")
        if not finished(upstream, record):
            report.failures.append(
                f"{label}: the API's reply to request {record['id']} never completed"
            )
        elif status_code == 200:
            observe(
                report.replies,
                endpoint(record),
                record.get("response_shape", {}),
                label,
            )
        elif status_code in (429, 500, 502, 503, 529):
            report.warnings.append(
                f"{label}: the API answered {status_code} (transient)"
            )
        else:
            reply = upstream.reply(record)
            error = reply.get("error", {}) if isinstance(reply, dict) else {}
            kind = error.get("type", "") if isinstance(error, dict) else ""
            text = error.get("message", "") if isinstance(error, dict) else ""
            report.failures.append(
                shown(
                    f"{label}: the API answered {status_code} {kind}: {str(text)[:200]}"
                )
            )
    # Each request as Claude Code sent it, against what reached the API.
    for sent, reached in pairs(front, upstream):
        front_shape = sent.get("response_shape", {})
        for path, kinds in reached.get("response_shape", {}).items():
            lost = sorted(set(kinds) - set(front_shape.get(path, [])))
            if lost:
                report.failures.append(
                    f"{label}: request {sent['id']}: the gateway dropped reply "
                    f"{path} {', '.join(lost)}"
                )
        if billing_versions(front.body(sent)) != billing_versions(
            upstream.body(reached)
        ):
            report.failures.append(
                f"{label}: request {sent['id']}'s billing line didn't reach the "
                "API as it was"
            )
    # Counts.
    sent_count, reached_count = len(front.models()), len(upstream.models())
    logged = status.get("api_requests", 0)
    if sent_count and not logged:
        report.failures.append(
            f"{label}: Claude Code logged no [API REQUEST] lines, the gateway saw "
            f"{sent_count} (its debug log is missing or its format changed)"
        )
    elif logged != sent_count:
        report.failures.append(
            f"{label}: Claude Code logged {logged} model requests, the gateway "
            f"saw {sent_count}"
        )
    if reached_count != sent_count - refused:
        report.failures.append(
            f"{label}: {sent_count} requests, {refused} refused, "
            f"but {reached_count} reached the API"
        )
    missing = coverage(capture)
    scenario = label.split("/", 1)[1]
    for what in missing:
        (
            report.failures
            if scenario in CORE or scenario == "refusal"
            else report.warnings
        ).append(f"{label}: didn't see {what}")
    replay(capture, config, report)
    if len(report.failures) == failures_before and not refused_unexpected(
        label, refused
    ):
        report.clean.add(label)


def finished(side: Side, record: Mapping[str, Any]) -> bool:
    """Whether a request got a whole reply.

    A streamed reply that never reached ``message_stop`` was cut off.
    """
    if not isinstance(record.get("status"), int):
        return False
    if "response_shape" not in record and "response_kind" not in record:
        return False
    streamed = "text/event-stream" in str(record.get("response_type", ""))
    if streamed and record["status"] == 200 and "/v1/messages" in record["path"]:
        return "event: message_stop" in side.stream(record)
    return True


def pairs(front: Side, upstream: Side) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Each request the gateway passed on, with the one that reached the API.

    They are paired in order within the same session, agent, model and
    number of messages: concurrent requests (a subagent's, the title's) can
    reach the API in another order than they left Claude Code.
    """

    def key(side: Side, record: Mapping[str, Any]) -> tuple[Any, ...]:
        body = side.body(record)
        headers = record.get("headers", {})
        messages = body.get("messages") if isinstance(body, dict) else None
        return (
            headers.get("x-claude-code-session-id"),
            headers.get("x-claude-code-agent-id"),
            body.get("model") if isinstance(body, dict) else None,
            len(messages) if isinstance(messages, list) else None,
        )

    queues: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in upstream.models():
        queues.setdefault(key(upstream, record), []).append(record)
    found = []
    for record in front.models():
        if _gateway_refusal(front, record) is not None:
            continue
        queue = queues.get(key(front, record))
        if queue:
            found.append((record, queue.pop(0)))
    return found


def refused_unexpected(label: str, refused: int) -> bool:
    return refused > 0 and not label.endswith("/refusal")


def _has(body: Any, test: Callable[[Any], bool]) -> bool:
    if test(body):
        return True
    if isinstance(body, dict):
        return any(_has(v, test) for v in body.values())
    if isinstance(body, list):
        return any(_has(v, test) for v in body)
    return False


def _block(kind: str) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, dict) and v.get("type") == kind


def coverage(capture: Capture) -> list[str]:
    """What the scenario should have made Claude Code send, and didn't."""
    scenario = capture.label.split("/", 1)[1]
    records = capture.front.models()
    bodies = [capture.front.body(r) for r in records]
    agent = any(r.get("headers", {}).get("x-claude-code-agent-id") for r in records)
    counts = capture.status.get("counts", {})
    missing = []
    if not records:
        return ["any model request"]
    if scenario in ("print", "interactive") and not agent:
        missing.append("a subagent's request")
    if scenario in ("compact", "interactive") and not counts.get("compact"):
        missing.append("a request for /compact")
    if scenario == "compact" and not counts.get("after_compact"):
        missing.append("a request after /compact")
    if scenario == "interactive" and not any(
        isinstance(b, dict)
        and isinstance(b.get("output_config"), dict)
        and "format" in b["output_config"]
        for b in bodies
    ):
        missing.append("title generation (output_config.format)")
    if scenario == "schema" and "StructuredOutput" not in {
        t.get("name")
        for b in bodies
        if isinstance(b, dict)
        for t in b.get("tools", [])
        if isinstance(t, dict)
    }:
        missing.append("the StructuredOutput tool")
    if scenario == "mcp" and not any(
        _has(b, _block("tool_reference"))
        or _has(
            b,
            lambda v: (
                isinstance(v, dict)
                and str(v.get("name", "")).endswith("find_contact")
                and v.get("type") == "tool_use"
            ),
        )
        for b in bodies
    ):
        missing.append("a tool_reference or a find_contact call")
    if scenario == "media" and not any(
        _has(b, _block("image")) or _has(b, _block("document")) for b in bodies
    ):
        missing.append("an image or a document block")
    if scenario == "web" and not (
        # WebSearch runs as a request of its own with the web_search server
        # tool; the API's blocks come back in that request's reply.
        any(
            _has(
                b,
                lambda v: (
                    isinstance(v, dict)
                    and str(v.get("type", "")).startswith("web_search_")
                ),
            )
            for b in bodies
        )
        and any(
            "type=server_tool_use"
            in r.get("response_shape", {}).get("$.content_block.type", [])
            for r in records
        )
    ):
        missing.append(
            "a web search: the web_search tool, and server_tool_use in its reply"
        )
    if scenario == "auto" and not any(
        isinstance(b, dict) and b.get("safeguards") for b in bodies
    ):
        missing.append("safeguards")
    if scenario == "refusal":
        missing += refusal_contract(capture, records, bodies)
    return missing


def refusal_contract(
    capture: Capture, records: list[dict[str, Any]], bodies: list[Any]
) -> list[str]:
    """Claude Code took the planted refusal as final, and kept the feature."""
    planted = [i for i, r in enumerate(records) if r.get("rewritten")]
    if len(planted) != 1:
        return [f"exactly one planted request (saw {len(planted)})"]
    at = planted[0]
    missing = []
    if _gateway_refusal(capture.front, records[at]) is None:
        missing.append("the gateway refusing the planted request")
    first_run = capture.status.get("counts", {}).get("first_run", len(records))
    later = [b for b in bodies[at + 1 : first_run] if isinstance(b, dict)]
    planted_body = bodies[at] if isinstance(bodies[at], dict) else {}
    if any(b.get("messages") == planted_body.get("messages") for b in later):
        missing.append("no resend of the refused request")
    if any(b.get("model") != planted_body.get("model") for b in later):
        missing.append("no fallback model")
    result = capture.folder / "result.txt"
    text = result.read_text(encoding="utf-8") if result.exists() else ""
    from veil.gateway.config import APP

    if f"{APP}:" not in text:
        missing.append("the gateway's message shown to the user")
    after = [
        (r, b)
        for r, b in zip(records[first_run:], bodies[first_run:], strict=True)
        if isinstance(b, dict)
    ]
    if not after:
        missing.append("a next turn")
    elif not any(b.get("safeguards") for _, b in after):
        missing.append("safeguards kept on the next turn")
    else:
        before_betas = betas(records[at].get("headers", {}).get("anthropic-beta", ""))
        after_betas = set().union(
            *(betas(r.get("headers", {}).get("anthropic-beta", "")) for r, _ in after)
        )
        if not before_betas <= after_betas:
            missing.append("the betas kept on the next turn")
    return missing


# --- The run -----------------------------------------------------------------


def known_census() -> dict[str, Any]:
    return {
        "requests": json.loads(
            (PAYLOADS / "request_census.json").read_text(encoding="utf-8")
        ),
        "replies": json.loads(
            (PAYLOADS / "response_census.json").read_text(encoding="utf-8")
        ),
        "endpoints": json.loads(
            (PAYLOADS / "endpoints.json").read_text(encoding="utf-8")
        ),
    }


def claude_version(claude: str) -> str | None:
    from veil.gateway.compat import from_cli_output

    try:
        out = subprocess.run(
            [claude, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            env=h.environment(),
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return from_cli_output(out)


def complete(report: Report) -> bool:
    """Whether the run covers every core scenario, clean, on all four models."""
    return all(f"{m}/{s}" in report.clean for m in MODELS for s in CORE)


def update(
    report: Report, known: Mapping[str, Any], *, accept_new: bool, version: str | None
) -> list[str]:
    """Write the refreshed census, words and version; returns what was done."""
    done = []
    files = {
        "request_census.json": merge_census(
            known["requests"], report.requests, accept_new=accept_new
        ),
        "response_census.json": merge_census(
            known["replies"], report.replies, accept_new=accept_new
        ),
        "endpoints.json": merge_endpoints(
            known["endpoints"], report.endpoints, accept_new=accept_new
        ),
    }
    for name, value in files.items():
        text = dump(value)
        _check_committable(text, name)
        if text != (PAYLOADS / name).read_text(encoding="utf-8"):
            write_atomic(PAYLOADS / name, text)
            done.append(f"updated {name}")
    if report.new and not accept_new:
        # Words from paths not reviewed yet would loosen the masker.
        done.append("vocab.py stays: the run saw something new (see --accept-new)")
    else:
        old = VOCAB.read_text(encoding="utf-8")
        new = vocab_source(old, report.words)
        _check_committable(new, VOCAB.name)
        if new != old:
            write_atomic(VOCAB, new)
            done.append("updated vocab.py")
    if version is None or not complete(report) or (report.new and not accept_new):
        done.append("the tested version stays: the run isn't complete and clean")
        return done
    for path, pattern in ((COMPAT, _TESTED), (README, _README_TESTED)):
        text, why = move_version(path.read_text(encoding="utf-8"), pattern, version)
        if why:
            done.append(f"{path.name}: {why}")
        else:
            write_atomic(path, text)
            done.append(f"{path.name}: tested version is now {version}")
    return done


def _check_committable(text: str, name: str) -> None:
    """Refuse to write a committed file holding anything real or this machine's."""
    h.assert_fictional(text)
    if _owned(text):
        raise AssertionError(f"{name} would hold this machine's user name")


def sweep(folder: Path) -> None:
    """End every process whose command line names the run folder.

    Claude Code, its hooks and MCP servers are; this process and those that
    started it (a shell given the folder, say) are spared. Only the whole
    folder counts (``/x/run1`` doesn't name ``/x/run10``).
    """
    # As given and as resolved: on macOS a temporary folder is also
    # /private/var/..., and commands were given the first.
    spellings = {
        str(p) for p in (folder.absolute(), folder.resolve()) if len(p.parts) >= 3
    }
    if not spellings:
        return  # never a run folder: "/" or "/Users"
    named = re.compile(
        "(?:" + "|".join(map(re.escape, sorted(spellings))) + r")(?=/|\s|$)"
    )
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    parents: dict[int, int] = {}
    commands: dict[int, str] = {}
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            parents[int(parts[0])] = int(parts[1])
            commands[int(parts[0])] = parts[2]
    spared, pid = set(), os.getpid()
    while pid > 1 and pid not in spared:
        spared.add(pid)
        pid = parents.get(pid, 0)
    end_processes(
        [
            pid
            for pid, command in commands.items()
            if pid not in spared and named.search(command)
        ]
    )


def print_report(report: Report, run_root: Path, verbose: bool) -> None:
    sections = [
        ("New", report.new),
        ("Failures", report.failures),
        ("Harness failures", report.harness),
        ("Warnings", report.warnings),
    ]
    for title, lines in sections:
        if lines:
            print(f"\n{title} ({len(lines)}):")
            for line in lines:
                print(f"  {shown(line)}")
    models = sorted(set(report.info))
    if verbose or models:
        print("\nModels:")
        for line in models:
            print(f"  {shown(line)}")
    print(
        f"\nClaude Code versions seen: {', '.join(sorted(report.versions)) or 'none'}"
    )
    print(f"Clean scenarios: {', '.join(sorted(report.clean)) or 'none'}")


def parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tests.live.census",
        description="Check what Claude Code sends through the gateway.",
    )
    parser.add_argument("--models", default=",".join(MODELS))
    parser.add_argument("--scenarios", default=",".join(CORE + EXTRAS))
    parser.add_argument(
        "--extras-on", default="haiku", help="models that run the extra scenarios"
    )
    parser.add_argument(
        "--auto-on",
        default="sonnet",
        help="models that run auto mode's scenarios (not Haiku: it has no auto mode)",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="refresh the census on a run without failures",
    )
    parser.add_argument(
        "--accept-new", action="store_true", help="with --update, also add what is new"
    )
    parser.add_argument(
        "--from", dest="source", type=Path, help="analyze a kept run folder again"
    )
    parser.add_argument(
        "--resume",
        type=Path,
        help="rerun what is missing or failed in a kept run folder",
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the run folder after a clean run"
    )
    parser.add_argument("--turn-timeout", type=float, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.accept_new:
        args.update = True
    return args


def plan(args: argparse.Namespace) -> list[tuple[str, str]]:
    """The (model, scenario) pairs to run, in order.

    Core scenarios run on every model; the extras on ``--extras-on``, but
    auto mode's on ``--auto-on``, since Claude Code (2.1.283) uses auto mode
    only on some models (not Haiku).
    """
    models = [m for m in args.models.split(",") if m]
    scenarios = [s for s in args.scenarios.split(",") if s]
    todo = [(m, s) for m in models for s in scenarios if s in CORE]
    for s in scenarios:
        if s in EXTRAS:
            on = args.auto_on if s in AUTO_SCENARIOS else args.extras_on
            todo += [(m, s) for m in on.split(",") if m]
    return todo


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    claude = h.claude_path()
    if args.source is None and (claude is None or not Path("/usr/bin/screen").exists()):
        print("needs the claude CLI and /usr/bin/screen", file=sys.stderr)
        return 3
    todo = plan(args)
    if args.dry_run:
        version = claude_version(claude) if claude else None
        print(
            f"Claude Code {version}; would run: "
            + ", ".join(f"{m}/{s}" for m, s in todo)
        )
        return 0
    kept = args.source or args.resume
    if kept is not None:
        kept = kept.resolve()
        if not (kept / "data" / "config.json").is_file():
            print(f"not a census run folder: {kept}", file=sys.stderr)
            return 3
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.default_int_handler)
    run_root = kept or Path(tempfile.mkdtemp(prefix="census-"))
    run_root.chmod(0o700)
    data_dir = run_root / "data"
    data_dir.mkdir(mode=0o700, exist_ok=True)
    (data_dir / "config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    code = 2
    try:
        start = claude_version(claude) if claude and args.source is None else None
        if args.source is None:
            for model, scenario in todo:
                status = run_root / model / scenario / "status.json"
                if args.resume and status.exists():
                    previous = json.loads(status.read_text(encoding="utf-8"))
                    if not previous.get("harness"):
                        continue
                print(f"running {model}/{scenario}", flush=True)
                out = run_scenario(
                    run_root, data_dir, model, scenario, args.turn_timeout
                )
                if out.harness:
                    print(f"  harness: {shown(out.harness)}", flush=True)
        end = claude_version(claude) if claude and args.source is None else None
        report = analyze(run_root, known_census())
        found = {tuple(c.label.split("/", 1)) for c in captures(run_root)}
        if not found:
            report.harness.append(f"no scenario ran in {run_root}")
        if args.source is None:
            for model, scenario in sorted(set(todo) - found):
                report.harness.append(f"{model}/{scenario}: didn't finish")
        if start and end and start != end:
            report.failures.append(
                f"Claude Code changed from {start} to {end} during the run"
            )
        version = end if start == end else None
        if version:
            report.versions.add(version)
        if len(report.versions) > 1:
            report.failures.append(
                f"more than one Claude Code version: {sorted(report.versions)}"
            )
        print_report(report, run_root, args.verbose)
        code = report.exit_code()
        if args.update:
            if code == 2:
                print("\nNot updated: the run had failures.")
            else:
                single = (
                    next(iter(report.versions)) if len(report.versions) == 1 else None
                )
                for line in update(
                    report, known_census(), accept_new=args.accept_new, version=single
                ):
                    print(line)
                if args.accept_new:
                    print("Now run: uv run pytest tests/test_gateway_request.py")
        return code
    except KeyboardInterrupt:
        code = 130
        return code
    finally:
        if args.source is None:  # --from starts nothing
            sweep(run_root)
        if code == 0 and not args.keep and args.source is None and args.resume is None:
            shutil.rmtree(run_root, ignore_errors=True)
        else:
            print(f"kept: {run_root}")


if __name__ == "__main__":
    sys.exit(main())
