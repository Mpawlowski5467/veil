"""Evaluate fictional workflow text at both gateway request boundaries, offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import veil
from veil import LiteralPlaceholderDetector, RegexDetector, Shield, __version__
from veil.gateway import MemoryLedger, RequestMasker, UnsupportedRequestError
from veil.gateway.openai_request import ResponsesRequestMasker
from veil.placeholders import PLACEHOLDER_RE
from veil.secret_review import ReviewError

CORPUS = Path(__file__).with_name("leak_corpus.json")
BASELINE = Path(__file__).with_name("leak_baseline.json")
ANNOTATION = re.compile(r"\[\[([A-Z][A-Z0-9_]{0,63})\|(.+?)\]\]", re.DOTALL)
SURFACES = {"prompt", "system", "tool_output"}


@dataclass(frozen=True)
class Mark:
    """One independently labeled occurrence, in source character offsets."""

    kind: str
    start: int
    end: int


@dataclass(frozen=True)
class Section:
    """A supported text field and its expected private spans."""

    surface: str
    text: str
    marks: tuple[Mark, ...]


@dataclass(frozen=True)
class Document:
    """A fictional request; sections share context, documents never share state."""

    id: str
    format: str
    language: str
    sections: tuple[Section, ...]


def unpack(surface: str, annotated: str) -> Section:
    """Strip annotation syntax, preserving multiline and escaped source text."""
    if surface not in SURFACES or not isinstance(annotated, str) or not annotated:
        raise ValueError("invalid corpus section")
    parts, marks = [], []
    cursor = length = 0
    for match in ANNOTATION.finditer(annotated):
        prefix = annotated[cursor : match.start()]
        parts.append(prefix)
        length += len(prefix)
        kind, value = match.groups()
        marks.append(Mark(kind, length, length + len(value)))
        parts.append(value)
        length += len(value)
        cursor = match.end()
    parts.append(annotated[cursor:])
    text = "".join(parts)
    if "[[" in text or "]]" in text:
        raise ValueError("malformed or nested corpus annotation")
    return Section(surface, text, tuple(marks))


def load_corpus(path: Path = CORPUS) -> list[Document]:
    """Validate all labels and IDs before running any detector."""
    source = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(source, list) or not source:
        raise ValueError("corpus must be a nonempty list")
    documents, ids = [], set()
    for doc in source:
        if not isinstance(doc, dict) or set(doc) != {
            "id",
            "format",
            "language",
            "sections",
        }:
            raise ValueError("invalid corpus document fields")
        if any(
            not isinstance(doc[key], str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", doc[key])
            for key in ("id", "format", "language")
        ):
            raise ValueError("invalid corpus document metadata")
        if doc["id"] in ids:
            raise ValueError("duplicate corpus ID")
        ids.add(doc["id"])
        if not isinstance(doc["sections"], list) or not doc["sections"]:
            raise ValueError("document needs text sections")
        sections = []
        for section in doc["sections"]:
            if not isinstance(section, dict) or set(section) != {"surface", "text"}:
                raise ValueError("invalid corpus section fields")
            sections.append(unpack(section["surface"], section["text"]))
        documents.append(
            Document(doc["id"], doc["format"], doc["language"], tuple(sections))
        )
    return documents


def request(document: Document, api: str) -> tuple[dict, list[tuple]]:
    """Wrap real text surfaces; return exact paths for their masked counterparts."""
    body: dict = {"model": "fictional-evaluation-model"}
    paths: list[tuple] = []
    if api == "anthropic":
        body.update(max_tokens=64, messages=[], system=[])
    elif api == "openai":
        body.update(input=[], store=False)
    else:
        raise ValueError("unknown API")
    for index, section in enumerate(document.sections):
        text = section.text
        call_id = f"call_{index}"
        if api == "openai":
            if section.surface == "tool_output":
                body["input"].append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": "read_file",
                        "arguments": "{}",
                    }
                )
                paths.append(("input", len(body["input"]), "output"))
                body["input"].append(
                    {"type": "function_call_output", "call_id": call_id, "output": text}
                )
            else:
                paths.append(("input", len(body["input"]), "content"))
                body["input"].append(
                    {
                        "role": "system" if section.surface == "system" else "user",
                        "content": text,
                    }
                )
        elif section.surface == "system":
            paths.append(("system", len(body["system"]), "text"))
            body["system"].append({"type": "text", "text": text})
        else:
            content: str | list = text
            if section.surface == "tool_output":
                body["messages"].append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call_id,
                                "name": "read_file",
                                "input": {},
                            }
                        ],
                    }
                )
                content = [
                    {"type": "tool_result", "tool_use_id": call_id, "content": text}
                ]
            path = ("messages", len(body["messages"]), "content")
            paths.append((*path, 0, "content") if isinstance(content, list) else path)
            body["messages"].append({"role": "user", "content": content})
    if api == "anthropic":
        if not body["messages"]:
            body["messages"].append({"role": "user", "content": "Please continue."})
        if not body["system"]:
            del body["system"]
    return body, paths


def at_path(body: dict, path: tuple) -> str:
    """Read only the fixture's model-facing text, never protocol configuration."""
    value = body
    for key in path:
        value = value[key]
    if not isinstance(value, str):
        raise ValueError("request adapter changed a measured text surface")
    return value


def trace_masks(original: str, masked: str, shield: Shield) -> list[Mark]:
    """Recover replaced source spans, checking exact reconstruction first.

    Searching only for an absent whole secret would miscount a partial mask as
    protection. Trace each replacement instead. Literal placeholders count as
    preserved harmless text, not detected secrets.
    """
    parts, spans = [], []
    cursor = length = 0
    for match in PLACEHOLDER_RE.finditer(masked):
        prefix = masked[cursor : match.start()]
        parts.append(prefix)
        length += len(prefix)
        value = shield.vault.get_value(match[0])
        if value is None:
            raise ValueError("unrecognized placeholder in evaluated output")
        parts.append(value)
        if match["type"] != "LITERAL":
            spans.append(Mark(match["type"], length, length + len(value)))
        length += len(value)
        cursor = match.end()
    parts.append(masked[cursor:])
    if "".join(parts) != original:
        raise ValueError("cannot trace masked output back to source exactly")
    return spans


def positions(spans) -> set[int]:
    """Union covered characters so overlapping spans are never double-counted."""
    return {i for span in spans for i in range(span.start, span.end)}


def review_spans(text: str, findings) -> list[Mark]:
    """Locate each real review finding without inventing findings from labels."""
    spans = []
    for finding in findings:
        start = text.find(finding.value)
        while start >= 0:
            spans.append(Mark(finding.kind, start, start + len(finding.value)))
            start = text.find(finding.value, start + 1)
    return spans


def measure(document: Document, api: str, review: bool) -> dict:
    """Measure one fresh request; never register labels or approve review findings."""
    regex = RegexDetector()
    shield = Shield(
        detectors=[LiteralPlaceholderDetector(regex.entity_types), regex],
        redact_warnings=True,
    )
    cls = ResponsesRequestMasker if api == "openai" else RequestMasker
    masker = cls(shield, MemoryLedger(), secret_review=review, note=None)
    body, paths = request(document, api)
    result = {
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
        "restored": True,
    }
    before = time.perf_counter_ns()
    try:
        masked = masker.mask(body)
        findings = masker.review_findings() if review else ()
        if findings:
            result["status"] = "held"
        result["review_findings"] = len(findings)
    except (UnsupportedRequestError, ReviewError):
        result["status"] = "refused"
        masked, findings = None, ()
        result["restored"] = None
    result["latency_ms"] = (time.perf_counter_ns() - before) / 1_000_000
    for section, path in zip(document.sections, paths, strict=True):
        spans, reviewed = [], review_spans(section.text, findings)
        if masked is not None:
            output = at_path(masked, path)
            spans = trace_masks(section.text, output, shield)
            restored = shield.restore(output)
            result["restored"] &= (
                restored.text == section.text and not restored.warnings
            )
        hidden, flagged, expected = (
            positions(spans),
            positions(reviewed),
            positions(section.marks),
        )
        for mark in section.marks:
            chars = set(range(mark.start, mark.end))
            result["masked_characters"].append(len(chars & hidden))
            result["review_characters"].append(len(chars & flagged))
            result["masked_or_flagged_characters"].append(
                len(chars & (hidden | flagged))
            )
            result["exposed_annotations"] += not chars <= hidden
        actual, labels = set(spans), set(section.marks)
        result["exact_tp"] += len(actual & labels)
        result["exact_fp"] += len(actual - labels)
        result["exact_fn"] += len(labels - actual)
        result["overmasked_characters"] += len(hidden - expected)
        result["review_harmless_characters"] += len(flagged - expected)
    return result


def summarize(documents: list[Document], rows: list[dict]) -> dict:
    """Keep automatic coverage, review burden, and readiness separate."""
    total = defaultdict(int)
    kinds = defaultdict(lambda: defaultdict(int))
    for document, row in zip(documents, rows, strict=True):
        marks = [m for section in document.sections for m in section.marks]
        total["requests"] += 1
        total[row["status"] + "_requests"] += 1
        total["negative_requests"] += not marks
        total["negative_held_requests"] += not marks and row["status"] == "held"
        total["restored_requests"] += row["restored"] is True
        for metric in (
            "exact_tp",
            "exact_fp",
            "exact_fn",
            "overmasked_characters",
            "review_findings",
            "review_harmless_characters",
        ):
            total[metric] += row[metric]
        for mark, hidden, flagged in zip(
            marks, row["masked_characters"], row["review_characters"], strict=True
        ):
            length = mark.end - mark.start
            for counter in (total, kinds[mark.kind]):
                counter["annotations"] += 1
                counter["fully_masked"] += hidden == length
                counter["partially_masked"] += 0 < hidden < length
                counter["unmasked"] += hidden == 0
                counter["fully_flagged_for_review"] += flagged == length
                counter["exposed_in_ready_requests"] += (
                    hidden < length and row["status"] == "ready"
                )
    for key in ("ready_requests", "held_requests", "refused_requests"):
        total[key] += 0
    total["exact_precision"] = ratio(
        total["exact_tp"], total["exact_tp"] + total["exact_fp"]
    )
    total["exact_recall"] = ratio(
        total["exact_tp"], total["exact_tp"] + total["exact_fn"]
    )
    return {
        "total": dict(total),
        "by_type": {k: dict(v) for k, v in sorted(kinds.items())},
    }


def ratio(numerator: int, denominator: int) -> float | None:
    """Undefined metrics stay null, not an implied perfect score."""
    return round(numerator / denominator, 4) if denominator else None


def evaluate(corpus: Path = CORPUS) -> dict:
    """Run both adapters with review off/on, using no disk vault or network."""
    documents = load_corpus(corpus)
    report = {
        "schema": 1,
        "version": __version__,
        "implementation_sha256": source_fingerprint(),
        # Universal newlines make a Git CRLF checkout comparable with LF CI.
        "corpus_sha256": hashlib.sha256(
            corpus.read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest(),
        "platform": platform.system(),
        "python": platform.python_version(),
        "documents": len(documents),
        "runs": {},
    }
    for api in ("anthropic", "openai"):
        for review in (False, True):
            rows = [measure(doc, api, review) for doc in documents]
            timings = sorted(row.pop("latency_ms") for row in rows)
            run = summarize(documents, rows)
            for attribute in ("format", "language"):
                groups = sorted({getattr(doc, attribute) for doc in documents})
                run["by_" + attribute] = {}
                for group in groups:
                    pairs = [
                        (doc, row)
                        for doc, row in zip(documents, rows, strict=True)
                        if getattr(doc, attribute) == group
                    ]
                    run["by_" + attribute][group] = summarize(
                        [doc for doc, _ in pairs], [row for _, row in pairs]
                    )["total"]
            run["latency_ms"] = {
                "samples": len(timings),
                "median": round(statistics.median(timings), 4),
                "p95": round(timings[int((len(timings) - 1) * 0.95)], 4),
            }
            run["cases"] = rows
            report["runs"][f"{api}/{'review' if review else 'defaults'}"] = run
    return report


def source_fingerprint() -> str:
    """Identify the imported Python implementation, including uncommitted edits."""
    root = Path(veil.__file__).resolve().parent
    digest = hashlib.sha256()
    for source in sorted(root.rglob("*.py")):
        digest.update(source.relative_to(root).as_posix().encode() + b"\0")
        digest.update(source.read_text(encoding="utf-8").encode("utf-8") + b"\0")
    return digest.hexdigest()


def snapshot(report: dict) -> dict:
    """Keep deterministic, per-occurrence evidence for regression checking."""
    return {
        "schema": report["schema"],
        "corpus_sha256": report["corpus_sha256"],
        "runs": {name: run["cases"] for name, run in report["runs"].items()},
    }


def regressions(report: dict, baseline: dict) -> list[str]:
    """Prevent worse per-case coverage or friction; improvements may pass.

    A new refusal or hold is not credited as successful masking. Corpus edits
    require explicit baseline review, so dropping a difficult case cannot hide
    behind a better aggregate percentage.
    """
    current = snapshot(report)
    if (current["schema"], current["corpus_sha256"], set(current["runs"])) != (
        baseline.get("schema"),
        baseline.get("corpus_sha256"),
        set(baseline.get("runs", {})),
    ):
        return ["corpus or report schema changed; review and update the baseline"]
    failures = []
    for name, rows in current["runs"].items():
        old = {row["id"]: row for row in baseline["runs"][name]}
        if set(old) != {row["id"] for row in rows}:
            failures.append(f"{name}: case IDs changed")
            continue
        for row in rows:
            previous = old[row["id"]]
            prefix = f"{name}/{row['id']}"
            for metric in ("masked_characters", "masked_or_flagged_characters"):
                now, before = row[metric], previous[metric]
                if len(now) != len(before) or any(
                    a < b for a, b in zip(now, before, strict=True)
                ):
                    failures.append(f"{prefix}: less {metric}")
            for metric in (
                "exact_fp",
                "exact_fn",
                "overmasked_characters",
                "review_harmless_characters",
            ):
                if row[metric] > previous[metric]:
                    failures.append(f"{prefix}: more {metric}")
            if row["restored"] is not True:
                failures.append(f"{prefix}: exact restoration unavailable")
            if row["status"] == "refused":
                failures.append(f"{prefix}: supported fixture refused")
            if (
                row["status"] == "held"
                and previous["status"] == "ready"
                and not row["masked_characters"]
            ):
                failures.append(f"{prefix}: new hold on harmless input")
            if (
                previous["status"] == "held"
                and row["status"] == "ready"
                and row["exposed_annotations"]
            ):
                failures.append(
                    f"{prefix}: review hold removed before complete masking"
                )
    return failures


def main() -> int:
    """Print a compact result, save optional metadata-only JSON, and enforce CI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the full measured report")
    parser.add_argument(
        "--check", action="store_true", help="fail on baseline regressions"
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="replace the checked-in baseline after reviewing findings",
    )
    args = parser.parse_args()
    if args.check and args.write_baseline:
        parser.error("--check and --write-baseline cannot be used together")
    report = evaluate()
    for name, run in report["runs"].items():
        total = run["total"]
        print(
            f"{name}: {total['fully_masked']}/{total['annotations']} fully masked; "
            f"{total['held_requests']}/{total['requests']} requests held; "
            f"{total['exposed_in_ready_requests']} exposed in ready requests; "
            f"{total['exact_fp']} extra/inexact masks; "
            f"{total['negative_held_requests']} harmless requests held"
        )
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.write_baseline:
        BASELINE.write_text(
            json.dumps(snapshot(report), indent=2) + "\n", encoding="utf-8"
        )
    if args.check:
        failures = regressions(report, json.loads(BASELINE.read_text(encoding="utf-8")))
        for failure in failures:
            print(f"REGRESSION: {failure}")
        return int(bool(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
