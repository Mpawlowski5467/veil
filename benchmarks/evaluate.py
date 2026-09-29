"""Measure exact spans on a small, fictional, deliberately imperfect corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from veil import Shield, __version__

CORPUS = Path(__file__).with_name("corpus.json")
ANNOTATION = re.compile(r"\[\[([A-Z0-9_]+)\|(.+?)\]\]")
REGISTERED = {"PERSON", "ORGANIZATION", "ADDRESS", "SECRET"}


def unpack(document):
    """Remove annotation syntax while retaining expected character offsets."""
    expected = set()
    parts = []
    cursor = 0
    length = 0
    for match in ANNOTATION.finditer(document["text"]):
        prefix = document["text"][cursor : match.start()]
        parts.append(prefix)
        length += len(prefix)
        kind, value = match.groups()
        expected.add((kind, length, length + len(value)))
        parts.append(value)
        length += len(value)
        cursor = match.end()
    parts.append(document["text"][cursor:])
    text = "".join(parts)
    assert "[[" not in text, "malformed corpus annotation"
    return text, expected


def score(counts):
    """Use null for undefined ratios rather than implying perfect detection."""
    tp, fp, fn = (counts[key] for key in ("tp", "fp", "fn"))
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall": round(tp / (tp + fn), 4) if tp + fn else None,
    }


def evaluate(*, registered=False, repeats=20):
    """Measure fresh in-memory sessions; no model, disk vault, or network call."""
    documents = json.loads(CORPUS.read_text(encoding="utf-8"))
    types, languages, formats = (defaultdict(Counter) for _ in range(3))
    failures = []
    timings = []
    total = Counter()
    restores = 0
    covered = 0
    expected_count = 0
    for doc in documents:
        text, expected = unpack(doc)
        shield = Shield()
        if registered:
            for kind, start, end in expected:
                if kind in REGISTERED:
                    shield.add_entity(text[start:end], kind)
        masked = shield.mask(text)
        actual = {(e.entity_type, e.start, e.end) for e in masked.entities}
        expected_count += len(expected)
        covered += sum(
            any(a <= start and b >= end for _, a, b in actual)
            for _, start, end in expected
        )
        for label, spans in (
            ("tp", actual & expected),
            ("fp", actual - expected),
            ("fn", expected - actual),
        ):
            for kind, _, _ in spans:
                types[kind][label] += 1
                languages[doc["language"]][label] += 1
                formats[doc["format"]][label] += 1
                total[label] += 1
        if actual != expected:
            failures.append(
                {
                    "id": doc["id"],
                    "false_positives": sorted(actual - expected),
                    "misses": sorted(expected - actual),
                }
            )
        restores += shield.restore(masked.text).text == text
        for _ in range(repeats):
            fresh = Shield()
            if registered:
                for kind, start, end in expected:
                    if kind in REGISTERED:
                        fresh.add_entity(text[start:end], kind)
            before = time.perf_counter_ns()
            fresh.mask(text)
            timings.append((time.perf_counter_ns() - before) / 1_000_000)
    timings.sort()
    return {
        "version": __version__,
        "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
        "documents": len(documents),
        "mode": "registered" if registered else "defaults",
        "platform": platform.system(),
        "python": platform.python_version(),
        "total": score(total),
        "by_type": {k: score(v) for k, v in sorted(types.items())},
        "by_language": {k: score(v) for k, v in sorted(languages.items())},
        "by_format": {k: score(v) for k, v in sorted(formats.items())},
        "exact_restorations": restores,
        "fully_covered_annotations": covered,
        "expected_annotations": expected_count,
        "mismatches": failures,
        "mask_latency_ms": {
            "samples": len(timings),
            "median": round(statistics.median(timings), 4),
            "p95": round(timings[int((len(timings) - 1) * 0.95)], 4),
        },
    }


def main():
    """Write a reproducible report with optional exact-value registrations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registered", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = (
        json.dumps(evaluate(registered=args.registered), ensure_ascii=False, indent=2)
        + "\n"
    )
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
