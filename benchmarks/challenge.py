"""Separately authored fictional detection challenges, using actual request adapters.

No network or model calls. Labels score results; only the explicit registration
fixtures may teach values to the detector, and only in the registered runs.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from veil import LiteralPlaceholderDetector, RegexDetector, Shield, __version__
from veil.gateway import MemoryLedger, RequestMasker, UnsupportedRequestError
from veil.gateway.openai_request import ResponsesRequestMasker
from veil.secret_review import ReviewError

SPEC = importlib.util.spec_from_file_location(
    "veil_challenge_scoring", Path(__file__).with_name("leaks.py")
)
assert SPEC is not None
assert SPEC.loader is not None
leaks = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = leaks
SPEC.loader.exec_module(leaks)

CORPUS = Path(__file__).with_name("challenge_corpus.json")
BASELINE = Path(__file__).with_name("challenge_baseline.json")


@dataclass(frozen=True)
class Challenge:
    """A labeled document and separately supplied fictional user registrations."""

    document: leaks.Document
    registrations: tuple[tuple[str, str], ...]


def load_corpus(path: Path = CORPUS) -> list[Challenge]:
    """Read all labels before evaluating; reject malformed or ambiguous fixtures."""
    source = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(source, list) or not source:
        raise ValueError("corpus must be a nonempty list")
    result, ids = [], set()
    for row in source:
        if not isinstance(row, dict) or set(row) != {
            "id",
            "format",
            "language",
            "sections",
            "registrations",
        }:
            raise ValueError("invalid corpus fields")
        if (
            any(
                not isinstance(row[key], str)
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", row[key])
                for key in ("id", "format", "language")
            )
            or row["id"] in ids
        ):
            raise ValueError("invalid corpus metadata or duplicate ID")
        ids.add(row["id"])
        if not isinstance(row["sections"], list) or not row["sections"]:
            raise ValueError("corpus document needs sections")
        sections = []
        for section in row["sections"]:
            if not isinstance(section, dict) or set(section) != {"surface", "text"}:
                raise ValueError("invalid corpus section")
            sections.append(leaks.unpack(section["surface"], section["text"]))
        if not isinstance(row["registrations"], list):
            raise ValueError("invalid corpus registrations")
        registrations = []
        for registration in row["registrations"]:
            if (
                not isinstance(registration, dict)
                or set(registration) != {"type", "value"}
                or not isinstance(registration["type"], str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", registration["type"])
                or not isinstance(registration["value"], str)
                or not registration["value"].strip()
            ):
                raise ValueError("invalid corpus registration")
            registrations.append((registration["type"], registration["value"]))
        result.append(
            Challenge(
                leaks.Document(
                    row["id"], row["format"], row["language"], tuple(sections)
                ),
                tuple(registrations),
            )
        )
    return result


def make_masker(api: str, review: bool, registrations=()):
    """Use a fresh local shield; never infer registrations from annotations."""
    if api not in {"anthropic", "openai"}:
        raise ValueError("unknown API")
    regex = RegexDetector()
    shield = Shield(
        detectors=[LiteralPlaceholderDetector(regex.entity_types), regex],
        redact_warnings=True,
    )
    for kind, value in registrations:
        shield.add_entity(value, kind)
    ledger = MemoryLedger()
    cls = ResponsesRequestMasker if api == "openai" else RequestMasker
    return shield, ledger, cls(shield, ledger, secret_review=review, note=None)


def trace_masks(original: str, masked: str, shield: Shield) -> list:
    """Trace masks while preserving an unchanged literal of an unknown type.

    The existing evaluator requires every placeholder-looking string to have a
    mapping. A document may contain an untouched example such as [PERSON_1]
    without having registered PERSON. It is harmless only if the same original
    characters occupy the current source position; generated unknowns still fail.
    """
    spans, parts = [], []
    cursor = length = 0
    for match in leaks.PLACEHOLDER_RE.finditer(masked):
        prefix = masked[cursor : match.start()]
        parts.append(prefix)
        length += len(prefix)
        value = shield.vault.get_value(match[0])
        if value is None:
            if original[length : length + len(match[0])] != match[0]:
                raise ValueError("unrecognized placeholder in evaluated output")
            value = match[0]
        elif match["type"] != "LITERAL":
            spans.append(leaks.Mark(match["type"], length, length + len(value)))
        parts.append(value)
        length += len(value)
        cursor = match.end()
    parts.append(masked[cursor:])
    if "".join(parts) != original:
        raise ValueError("cannot trace masked output back to source exactly")
    return spans


def measure(case: Challenge, api: str, review: bool, registered: bool = False) -> dict:
    """Count exact spans, ready-request exposure, and review burden separately."""
    document = case.document
    registrations = case.registrations if registered else ()
    shield, _, masker = make_masker(api, review, registrations)
    body, paths = leaks.request(document, api)
    row = {
        "id": document.id,
        "status": "ready",
        "masked_characters": [],
        "review_characters": [],
        "masked_or_flagged_characters": [],
        "exposed_annotations": 0,
        "exact_tp": 0,
        "exact_fp": 0,
        "exact_fn": 0,
        "overmasked_characters": 0,
        "review_harmless_characters": 0,
        "review_findings": 0,
        "registrations_applied": len(registrations),
        "restored": True,
        "restoration_warnings": 0,
    }
    try:
        masked = masker.mask(body)
        findings = masker.review_findings() if review else ()
        row["review_findings"] = len(findings)
        if findings:
            row["status"] = "held"
    except (UnsupportedRequestError, ReviewError):
        row["status"] = "refused"
        row["restored"] = None
        masked, findings = None, ()
    for section, path in zip(document.sections, paths, strict=True):
        spans = []
        reviewed = leaks.review_spans(section.text, findings)
        if masked is not None:
            output = leaks.at_path(masked, path)
            spans = trace_masks(section.text, output, shield)
            restored = shield.restore(output)
            row["restored"] &= restored.text == section.text
            row["restoration_warnings"] += len(restored.warnings)
        hidden, flagged, expected = (
            leaks.positions(spans),
            leaks.positions(reviewed),
            leaks.positions(section.marks),
        )
        for mark in section.marks:
            chars = set(range(mark.start, mark.end))
            row["masked_characters"].append(len(chars & hidden))
            row["review_characters"].append(len(chars & flagged))
            row["masked_or_flagged_characters"].append(len(chars & (hidden | flagged)))
            row["exposed_annotations"] += not chars <= hidden
        actual, labels = set(spans), set(section.marks)
        row["exact_tp"] += len(actual & labels)
        row["exact_fp"] += len(actual - labels)
        row["exact_fn"] += len(labels - actual)
        row["overmasked_characters"] += len(hidden - expected)
        row["review_harmless_characters"] += len(flagged - expected)
    return row


def evaluate(corpus: Path = CORPUS) -> dict:
    """Compare defaults, review, and explicitly configured registration scenarios."""
    cases = load_corpus(corpus)
    documents = [case.document for case in cases]
    report = {
        "schema": 1,
        "version": __version__,
        "implementation_sha256": leaks.source_fingerprint(),
        "corpus_sha256": hashlib.sha256(
            corpus.read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest(),
        "platform": platform.system(),
        "python": platform.python_version(),
        "documents": len(cases),
        "authorship": (
            "separately authored fictional challenges; not human independent validation"
        ),
        "runs": {},
    }
    for api in ("anthropic", "openai"):
        for registered in (False, True):
            for review in (False, True):
                rows = [measure(case, api, review, registered) for case in cases]
                run = leaks.summarize(documents, rows)
                run["cases"] = rows
                mode = ("registered-" if registered else "") + (
                    "review" if review else "defaults"
                )
                report["runs"][f"{api}/{mode}"] = run
    return report


def regressions(report: dict, baseline: dict) -> list[str]:
    """Also preserve the warning count; exact text and warnings are distinct."""
    failures = leaks.regressions(report, baseline)
    if "runs" not in baseline:
        return failures
    for name, run in report["runs"].items():
        old = {row["id"]: row for row in baseline["runs"].get(name, [])}
        for row in run["cases"]:
            if row["id"] in old and row["restoration_warnings"] > old[row["id"]].get(
                "restoration_warnings", 0
            ):
                failures.append(f"{name}/{row['id']}: more restoration warnings")
    return failures


def main() -> int:
    """Save reproducible evidence and enforce the same per-case regression policy."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()
    report = evaluate()
    for name, run in report["runs"].items():
        total = run["total"]
        print(
            f"{name}: {total['fully_masked']}/{total['annotations']} fully masked; "
            f"{total['held_requests']}/{total['requests']} requests withheld; "
            f"{total['exposed_in_ready_requests']} exposed in ready requests; "
            f"{total['exact_fp']} extra/inexact masks; "
            f"{total['negative_held_requests']} harmless requests held"
        )
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.write_baseline:
        BASELINE.write_text(
            json.dumps(leaks.snapshot(report), indent=2) + "\n", encoding="utf-8"
        )
    if args.check:
        failures = regressions(report, json.loads(BASELINE.read_text(encoding="utf-8")))
        for failure in failures:
            print(f"REGRESSION: {failure}")
        return int(bool(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
