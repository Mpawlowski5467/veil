# Detection baseline

This page records the published 0.5.0 baseline. The newer source-checkout
[coding-secret rules](coding-secrets.md) have separate fictional regression
fixtures and are not measured by the numbers below.

Measured 2026-09-29 with Veil 0.5.0. This is a small, authored regression corpus:
**30 fictional documents, 53 annotated occurrences, 8 language tags**, and
email, invoice, JSON, URL, log, negative, and unsupported-format examples.
It is not an independently sampled accuracy study or a privacy guarantee.
Language tags describe the surrounding writing, not broad language coverage.

The corpus deliberately includes names, addresses, arbitrary secrets, local
phone layouts, obfuscated/quoted emails, ambiguous SSNs, and loopback IPv6 that
the default detectors do not promise to find. Every annotated occurrence stays
in the denominator. Registrations are supplied from the corpus labels in the
second mode: this measures exact registration, not automatic name recognition.

| Type | Default exact TP | FP | Misses | Registered exact TP | FP | Misses |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `ADDRESS` | 0 | 0 | 1 | 1 | 0 | 0 |
| `CREDIT_CARD` | 4 | 0 | 0 | 4 | 0 | 0 |
| `EMAIL` | 13 | 2 | 4 | 13 | 2 | 4 |
| `IBAN` | 3 | 0 | 0 | 3 | 0 | 0 |
| `IPV4` | 3 | 0 | 0 | 3 | 0 | 0 |
| `IPV6` | 1 | 0 | 1 | 1 | 0 | 1 |
| `ORGANIZATION` | 0 | 0 | 2 | 2 | 0 | 0 |
| `PERSON` | 0 | 0 | 5 | 5 | 0 | 0 |
| `PHONE` | 8 | 0 | 2 | 8 | 0 | 2 |
| `SECRET` | 0 | 0 | 1 | 1 | 0 | 0 |
| `SSN` | 4 | 0 | 1 | 4 | 0 | 1 |

**Defaults:** exact-span precision 94.74%, recall 67.92%; 38/53 annotated values fully covered by a replacement. Masking median 0.0476 ms, p95 0.0855 ms across 600 small-document calls.

**Registered:** exact-span precision 95.74%, recall 84.91%; 47/53 annotated values fully covered by a replacement. Masking median 0.0497 ms, p95 0.0892 ms across 600 small-document calls.

A boundary mismatch counts as both an FP and a miss. In this corpus the two
extra email matches include adjacent Chinese text or a URL query key while
covering the email itself. Character coverage distinguishes that over-masking
from values left visible; it does not measure inference risk.

All 30 documents restored character-for-character as Python text in both modes. That means
replacements were reversible; it does **not** mean all annotated values were
masked. Remaining limitations include quoted/obfuscated email addresses, local
French/UK phone numbers without a country code, unlabeled compact SSNs, and
`::1` (deliberately excluded to reduce code/time false positives).

Timing uses fresh in-memory shields, excludes registration time, disk vaults,
network, and model latency, and is descriptive rather than a CI threshold.
The checked-in reports name the OS/Python version and corpus SHA-256 and include
results by type, format, language, and document. These tiny samples should not
be combined into a general “Veil accuracy” marketing claim.

## Reproduce and extend

```bash
uv run python benchmarks/evaluate.py --output benchmarks/defaults.json
uv run python benchmarks/evaluate.py --registered --output benchmarks/registered.json
```

See [labeled corpus](../benchmarks/corpus.json), [default report](../benchmarks/defaults.json),
and [registration report](../benchmarks/registered.json). `[[TYPE|value]]` marks
an expected exact span. Add realistic fictional documents and hard negatives
before making broader coverage claims. Keep expected spans independent of the
detector's output; review every new miss or extra match rather than relabeling
the corpus to make a score improve.
