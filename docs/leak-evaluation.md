# Workflow leak evaluation

Veil 0.6.0b1 has an offline evaluation of **52 authored, fictional
requests, 52 annotated private occurrences, and 15 requests with no private
values**. It covers 14 formats, including emails, `.env` files, JSON, YAML,
shell commands, HTTP headers, logs, diffs, and conversations with tool output.
The language tags are English, Polish, and Spanish; these few examples do not
establish general language coverage.

This is an intentionally difficult regression corpus, including unsupported
formats and future detection targets. It is **not an independently sampled
accuracy study, a security audit, or a guarantee that every secret is hidden**.
All values are invented; no credentials are validated and no model is called.
The results below are for the current source, which adds fixes made after the
0.6.0b1 beta; the older 0.5.0 wheel lacks these secret rules.

## Current results

Both adapters produced the same coverage on this corpus. Each request gets a
fresh in-memory session with no identity or manual registrations. Review mode
does not approve anything automatically or learn the answers from the labels.

| Measurement, per adapter | Review off | Review on |
| --- | ---: | ---: |
| Annotated occurrences fully masked | 41 / 52 | 41 / 52 |
| Annotated occurrences partially masked | 1 | 1 |
| Annotated occurrences not masked | 10 | 10 |
| Requests held for local review | 0 / 52 | 11 / 52 |
| Annotated occurrences still exposed in requests ready to send | 11 | 0 |
| Requests with no private values held for review | 0 / 15 | 3 / 15 |
| Extra or inexact mask spans | 1 | 1 |
| Harmless characters masked | 0 | 0 |
| Prepared requests restored exactly as Python text | 52 / 52 | 52 / 52 |

The 11 review holds contain eight requests with private values and three harmless
requests. All 11 occurrences not fully masked automatically are fully covered by
actual review findings. A test checks this per occurrence: an incidental hold
for an unrelated value cannot satisfy it. **Review is off by default.** Without
review, the 11 occurrences in the table remain exposed.

The same unchanged corpus originally had 27 fully masked occurrences and 17
still exposed with review on. The preceding update reached 34 automatic masks
and zero exposure with review. The preceding automatic-syntax update raised automatic masks from **34 to
41 (65.38% to 78.85%)**, moving seven formerly held requests to automatic
forwarding. These are the YAML block, command option, quoted support-email
password, Polish and Spanish password phrases, recovery code, and password
containing an email. Extra/inexact masks fall from 11 to 9; harmless characters
masked fall from 89 to 88. No case regresses against the preceding baseline.
Corpus labels and difficult cases were not removed.

The 0.6.0b1 precision change then removes two harmless property-reference masks
from the `code-attribute-reference` case. Extra/inexact spans fall from nine to
seven and harmless characters masked from 88 to 48. All other occurrence coverage,
review holds, and exact restoration are unchanged; every per-case check passes.

After 0.6.0b1, unquoted values that are code (types, echoes of the field name,
and credential-named identifiers) are no longer masked. `code-self-reference`
and `code-concatenated-password` lose their six extra spans: extra/inexact spans
fall from seven to one and harmless characters masked from 48 to zero.
`code-concatenated-password` is no longer held for review, so review holds fall
from 12 to 11. Automatic coverage (41/52), zero exposure with review, and exact
restoration are unchanged. The other fixes made since 0.6.0b1 (typed
declarations, unquoted clause values, stringified JSON, `=>` assignments, and
run-together names) change no case in this corpus.

The local HTTP retry tests simulate confirming each actual finding using its
suggested type, without using corpus labels to find values. After that explicit
simulation, all 52 annotated occurrences are masked and every document restores
exactly. This is conditional workflow validation, **not automatic detection or a
study of whether users make correct decisions**. Choosing to allow a private
value can still expose it.

Exact span/type precision is 97.62% and recall is 78.85% on these deliberately
selected examples: 41 exact true positives, one extra/inexact span, and 11 exact
misses. A partial mask, wrong type, or oversized replacement can count as both
an extra span and a miss. Full character coverage therefore differs from exact
span recall. These figures should not be advertised as overall Veil accuracy.

The [measured JSON report](../benchmarks/leak_results.json) includes breakdowns
by type, format, and language, per-case character coverage, OS/Python metadata,
and hashes of the corpus and imported source implementation. Its timing samples
measure only local masking and triage, once per small request. They exclude user
review, storage, HTTP, and model latency, and are not performance thresholds.
Hashes normalize file line endings so a Windows CRLF checkout is comparable
with a Unix LF checkout; line-ending escapes inside corpus text stay intact.

## What this found

- **Fixed:** a removed diff line such as `-DB_PASSWORD=...` skipped labelled
  credential detection. The fix covers removed, added, and context lines with
  Unix or Windows line endings. Both the old and new passwords are now masked.
- **Automatic credential fixes:** supported session/auth cookies in pasted HTTP
  headers; credential/signature query parameters (including the tested SAS and
  S3 examples); percent-encoded query names; Unicode-escaped JSON field names;
  and simple quoted credential concatenations. Values keep their source spelling.
- **Additional automatic syntax:** quoted credential assertions and delimited
  token-like values, specific Polish/Spanish password phrases, recovery/backup
  codes, supported shell credential options, and indented YAML credential blocks.
  Full-value masks replace partial email masking inside a quoted password. The
  raw YAML block, including indentation, becomes one placeholder; exact textual
  restoration is tested, not execution of masked YAML.
- **Review coverage:** ambiguous sign-in instructions and multiword values,
  explicitly labelled webhook endpoints, explicit
  base64 payloads, and short token-like fields accompanied by a credential-split
  cue in the same request. No recursive decoding or code execution is performed.
- **Labelled PII review:** the name, street address, birth date, passport, and
  driver's-license examples now have dedicated local review classifications.
  These are scoped context cues, not automatic recognition of arbitrary people
  or addresses. Unlabelled or differently formatted values can still be missed.
- **False positives:** unquoted types (`String`, `Option<String>`), echoes of
  the field name (`api_key=api_key`), and credential-named identifiers such as
  `settings.database_password` or `db_password` remain readable, while quoted
  lookalikes and weak bare passwords remain protected. Other code-looking
  strings may still get masked; hashes, trace IDs, and explanatory prose can
  trigger review. The YAML block marker is
  now preserved while its content is masked. URL credential masking preserves
  neighboring query parameters in the measured cases.

The webhook finding still covers the entire labelled endpoint, including 41
harmless URL characters: approval protects the whole endpoint instead of guessing
which path component is sensitive. This earlier baseline tradeoff is unchanged;
these characters are not automatically masked before approval. The existing three
harmless-request holds did not increase.

Do not silently allow all hashes or code-looking values to improve a score:
real secrets can have those forms too. Each change needs positive and negative
examples and a clear decision about when Veil should ask locally.

## Run it yourself

From an updated source checkout:

```bash
uv run python benchmarks/leaks.py --check --output /tmp/veil-leak-results.json
uv run pytest -q tests/test_leak_evaluation.py
uv run pytest -q tests/test_privacy_context.py
uv run pytest -q tests/test_secret_automatic.py
```

On Windows, use a local output path such as `veil-leak-results.json` instead of
`/tmp/veil-leak-results.json`. The command runs the checked-in fictional corpus;
it does not inspect your files, configuration, credentials, or running gateway.
The JSON report contains IDs, offsets/counts, and metadata, not prompt text,
private values, masked prompts, or vault mappings.

The first command exercises the real Anthropic Messages and OpenAI Responses
request adapters with review off and on: **208 request evaluations**. The tests
also send every case through a gateway to a fake provider listening on loopback.
They check observed outgoing masking, zero upstream requests for review holds,
and exact restoration of echoed masked text for requests ready to send. The
fake provider has no external network destination and uses no real credentials.
The privacy-context tests add different values, casing, accents, quoting,
line endings, URL/JSON encodings, code and cookie negatives, review bounds,
persistent CLI mappings, and actual gateway confirmation/retry for every original
document. Automatic-syntax tests add other values, multilingual label variants,
clause boundaries, shell quoting/escapes, YAML dedents/indicators/comments,
references, custom-rule overrides, long nonmatches, and real HTTP masking and
restoration with review on and off. The native CI jobs run these tests too.

Native CI runs this evaluation and its transport checks on macOS, Linux, and
Windows with Python 3.10 and 3.14. Each native job uploads its measured JSON as a
`leak-evaluation-*` artifact, including on a regression when a report is available.

## How accounting and regression checks work

The [corpus](../benchmarks/leak_corpus.json) uses `[[TYPE|value]]` annotations in
each `prompt`, `system`, or `tool_output` section. Labels are removed before
masking and used only for scoring. A repeated occurrence is counted separately.
For split-secret examples, each annotated fragment is a separate occurrence;
they do not inflate the number of distinct credentials. Surrounding characters
without annotations are expected to remain untouched. `TOKEN` includes Basic
authentication values, following Veil's documented type categories.

The evaluator traces actual placeholder replacements back to the source using
the fresh session's vault. A whole secret disappearing from a substring search
is insufficient: masking only the email inside a longer password still counts
as a partial mask. Overlapping character coverage is counted once. Literal
placeholder examples do not count as detected private values. An unexplained
source transformation fails evaluation instead of receiving coverage credit.

`--check` compares each occurrence and request with the committed
[baseline](../benchmarks/leak_baseline.json). It rejects less automatic masking,
less combined mask/review coverage, additional inexact masks or harmless
characters masked/flagged, new holds on entirely harmless requests, unsupported
refusals, broken restoration, and removal of a review hold while annotated data
is still exposed. A better aggregate score cannot hide a worse individual case.
Replacing review with complete automatic masking may pass.

Known misses are recorded, not skipped or marked as successful. Passing CI means
the known baseline did not regress; it does not mean there are no leaks. Corpus
or label changes require an explicit baseline update:

```bash
uv run python benchmarks/leaks.py --write-baseline --output benchmarks/leak_results.json
```

Review every changed case before committing that update. Never delete a hard
case or change its intended privacy label merely to improve the score. Reports
include the version and imported source hash so results can be tied to an exact
implementation.

## Scope still to validate

File examples here are **text contents carried in supported prompt/tool fields**.
This suite does not validate image/PDF OCR, tool definitions, authentication
headers, browser/client telemetry, remote tools, live provider behavior, or a
user's actual desktop routing. Simulated confirmations do not measure human
decision quality or inference from surrounding context. Local review only holds findings
it recognizes. Independently reviewed real-workflow sampling, longer sessions,
external beta testing, and security review remain [roadmap items](../ROADMAP.md).
