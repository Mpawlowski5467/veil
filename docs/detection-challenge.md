# Separate detection challenge set

This challenge set adds **44 fictional requests, 36 annotated private occurrences,
and 11 harmless requests** across 15 code, configuration, log, HTTP, and prose
formats. It was authored separately from the existing 52-request leak corpus,
with privacy labels written before running the detector. The examples deliberately
include ambiguous and unsupported forms. English and German are sample language
tags, not evidence of broad language coverage.

This is still **agent-authored regression evidence, not an independent human
assessment, real-user beta evidence, or a general accuracy estimate**. Once a
challenge informs a fix, it is no longer a held-out evaluation. The existing
[52-request corpus and its labels](leak-evaluation.md) remain unchanged.

## Method

The evaluator sends the same source text through the actual Anthropic Messages
and OpenAI Responses request adapters, with review off and on, each using a fresh
in-memory session. It makes no network or provider call and never approves a
review finding. All 44 requests run a second time with the corpus's explicit
registration fixtures enabled, for **352 adapter evaluations** in total.

Only three documents provide a fictional user registration: a code-shaped
password, a person's exact name, and that name followed by a differently cased
occurrence. Registrations are listed separately from privacy annotations; the
other private labels never become registrations. The case-variant scenario
checks the documented exact-match boundary rather than assuming automatic name
recognition or case folding.

The report records per-occurrence masked and flagged characters, exact span/type
matches, harmless characters masked or flagged, request status, and exact local
text restoration. A held request is **withheld pending a human decision**, not an
automatically masked success. No simulated approval earns detection credit.
A harmless request can be held, and a request can be held redundantly for a value
already masked; both remain visible in the report.

An untouched literal such as `[PERSON_1]` can restore to the exact original text
and still produce an unknown-placeholder warning. These are separate report
fields (`restored` and `restoration_warnings`); no warning is silently counted as
a text change. Tracing accepts an unmapped literal only when it already occupies
the same position in the original text.

## Initial findings

Both adapters produced the following results before changes prompted by this
challenge. The immutable [initial report](../benchmarks/challenge_initial.json)
records the imported implementation hash; all corpus labels remain intact.

| Measurement, per adapter | Defaults | Review | Explicit registration | Registration + review |
| --- | ---: | ---: | ---: | ---: |
| Annotated occurrences fully masked | 19 / 36 | 19 / 36 | 21 / 36 | 21 / 36 |
| Occurrences exposed in ready requests | 17 | 15 | 15 | 13 |
| Requests withheld for review | 0 / 44 | 5 / 44 | 0 / 44 | 5 / 44 |
| Harmless requests withheld | 0 / 11 | 2 / 11 | 0 / 11 | 2 / 11 |
| Extra or inexact automatic mask spans | 1 | 1 | 1 | 1 |
| Harmless characters masked | 15 | 15 | 15 | 15 |
| Requests restored as exact local text | 44 / 44 | 44 / 44 | 44 / 44 | 44 / 44 |

These results illustrate why the earlier corpus's zero ready-request exposures
with review enabled cannot be generalized. The initial set includes:

- Unquoted weak/default/code-shaped passwords such as `ADMIN_PASSWORD=admin`
  and `PASSWORD=SuperPassword`. These are documented precision tradeoffs; review
  also leaves them alone. Quoting or explicitly registering them protects the
  tested values.
- A triple-quoted TOML password that was neither masked nor held, and an address
  introduced by `Customer address:` that did not produce a review finding.
- A custom `tenant_access` cookie, a JSON base64 credential field, and prose
  split-secret fragments that were not recognized. These examples do not imply
  support for arbitrary cookie names, encodings, or fragmented secrets.
- An exact registered name that is masked, but its upper-case spelling that is
  not. Register each required variant.
- A form-encoded password whose automatic mask also consumes the neighboring
  `&remember=false` parameter: the value is hidden and restores, but unrelated
  source characters are masked too.
- A masked `curl` header still held for review, and harmless build hashes and
  trace identifiers held for review. These quantify friction separately from
  coverage.

## Published 0.6.0b2 results

The unchanged labels exposed two gaps that are now covered: triple-quoted
credential assignments are automatically masked, and a qualified label such as
`Customer address:` followed by a numeric street address produces a local
review hold. Both adapter results agree:

| Measurement, per adapter | Defaults | Review | Explicit registration | Registration + review |
| --- | ---: | ---: | ---: | ---: |
| Annotated occurrences fully masked | 20 / 36 | 20 / 36 | 22 / 36 | 22 / 36 |
| Occurrences exposed in ready requests | 16 | 13 | 14 | 11 |
| Requests withheld for review | 0 / 44 | 6 / 44 | 0 / 44 | 6 / 44 |
| Harmless requests withheld | 0 / 11 | 2 / 11 | 0 / 11 | 2 / 11 |
| Extra or inexact automatic mask spans | 1 | 1 | 1 | 1 |
| Harmless characters masked | 15 | 15 | 15 | 15 |
| Requests restored as exact local text | 44 / 44 | 44 / 44 | 44 / 44 | 44 / 44 |

Compared with the initial result, automatic coverage increases by one occurrence,
and ready-request exposures with review decrease by two. The added address hold
is withheld for a user's decision, not credited as an automatic mask. Neither
change increases the harmless-request hold count. All other per-case measures
remain unchanged, including the unknown-literal warning. The remaining examples
above remain gaps or explicit scope limits; this candidate does not claim to
recognize every credential, spelling, name, address, or encoding.

## Unreleased follow-up results

After 0.6.0b2, the current source adds bounded form-body, explicit encoded
credential-field, and namespaced auth/session-cookie rules, plus review of
explicitly labelled credential fragments and more precise quoted-header review.
The published b2 wheel does not include these additions. With the same immutable
labels, both adapters now produce:

| Measurement, per adapter | Defaults | Review | Explicit registration | Registration + review |
| --- | ---: | ---: | ---: | ---: |
| Annotated occurrences fully masked | 21 / 36 | 21 / 36 | 23 / 36 | 23 / 36 |
| Occurrences exposed in ready requests | 15 | 10 | 13 | 8 |
| Requests withheld for review | 0 / 44 | 6 / 44 | 0 / 44 | 6 / 44 |
| Harmless requests withheld | 0 / 11 | 2 / 11 | 0 / 11 | 2 / 11 |
| Extra or inexact automatic mask spans | 0 | 0 | 0 | 0 |
| Harmless characters masked | 0 | 0 | 0 | 0 |
| Requests restored as exact local text | 44 / 44 | 44 / 44 | 44 / 44 | 44 / 44 |

Four cases change from b2. `http-form-password` keeps full coverage but now
preserves the 15 harmless characters in `&remember=false`, so its mask has the
exact annotated span. `base64-account-password` gains one automatic mask of its
28-character original encoded spelling; no decoding or inferred registration is
used.

The next review refinement changes two more rows, only in review-enabled runs.
`split-credential-fragments` is now withheld with two findings covering all 8 and
9 characters of its two labelled pieces. These are review findings, not automatic
masks. `shell-header-value` now proceeds with its complete automatic mask intact:
the matching quote after a `-H` header establishes that the following URL is
outside the credential. Its redundant finding, which also flagged 37 harmless
characters, disappears. Review exposures therefore fall from 12 to 10 (10 to 8
with registrations), while total holds stay at six and harmless holds stay at
two. Every other per-case metric is unchanged, including the unknown-literal
warning. The original 52-request evaluation also has no per-case metric changes.

Cookie additions have separate positive and negative tests rather than adding
names to fit the score. In particular, `tenant_access` remains a recorded miss:
that ambiguous suffix does not establish an authentication cookie. The
`feature_access`, `access_level`, preference-cookie, and code-reference negatives
remain readable. The [exact syntax and limits](coding-secrets.md#unreleased-explicit-forms-encoded-credential-fields-and-namespaced-cookies)
apply. [Labelled fragment review](secret-review.md#unreleased-review-boundary-refinements)
also has explicit context and delimiter limits; arbitrary names, encodings,
fragmentation, and code-looking defaults still need registration or further
bounded rules.

## Reproduce and compare

```bash
uv run python benchmarks/challenge.py --check --output /tmp/veil-challenge-results.json
uv run pytest -q tests/test_detection_challenge.py
```

On Windows, use a local output path such as `veil-challenge-results.json`. The
corpus includes only fictional values; evaluation does not inspect personal
files, client credentials, a running gateway, or the clipboard. The tests also
check representative ready, missed, held, registered, and harmless examples
against a fake provider on loopback, including both newly fixed cases: held
requests must make zero upstream requests, outgoing masks must match the
evaluator, and echoed replies must restore exact text. They make no external
provider calls. Native CI runs the challenge suite and evaluator on macOS,
Linux, and Windows with Python 3.10 and 3.14, uploading a
`detection-challenge-*` report for each job.

The [current report](../benchmarks/challenge_results.json) contains metadata and
counts, not prompts, original values, registrations, or masked text. The
[baseline](../benchmarks/challenge_baseline.json) preserves deterministic
per-case results. Source and corpus hashes normalize checkout line endings, so
native Windows and Unix results are comparable. Timing is intentionally not a
pass/fail gate.

`--check` allows improvements but rejects reduced automatic or combined
mask/review coverage, additional inexact masks or harmless-character masking,
new holds on harmless input, a removed hold that exposes a private occurrence,
additional restoration warnings, refused supported fixtures, and broken exact
restoration. An aggregate gain cannot excuse a worse individual case. Existing
misses are recorded rather than skipped or treated as passes. Corpus changes
require an explicit baseline review.

After reviewing every changed case, update only the current baseline and report:

```bash
uv run python benchmarks/challenge.py --write-baseline --output benchmarks/challenge_results.json
```

Keep `challenge_initial.json` as the initial evidence. Do not remove hard examples,
change their privacy labels to follow detector output, or describe these results
as proof that review catches all secrets. Independent sampling, human review
choices, fresh-client compatibility, and real beta journeys remain open work.
