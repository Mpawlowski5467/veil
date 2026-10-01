# Workflow leak evaluation

Veil's source checkout has an offline evaluation of **52 authored, fictional
requests, 52 annotated private occurrences, and 15 requests with no private
values**. It covers 14 formats, including emails, `.env` files, JSON, YAML,
shell commands, HTTP headers, logs, diffs, and conversations with tool output.
The language tags are English, Polish, and Spanish; these few examples do not
establish general language coverage.

This is an intentionally difficult regression corpus, including unsupported
formats and future detection targets. It is **not an independently sampled
accuracy study, a security audit, or a guarantee that every secret is hidden**.
All values are invented; no credentials are validated and no model is called.
The results apply to the source checkout after 0.5.0, not the published wheel.

## Current results

Both adapters produced the same coverage on this corpus. Each request gets a
fresh in-memory session with no identity or manual registrations. Review mode
does not approve anything automatically or learn the answers from the labels.

| Measurement, per adapter | Review off | Review on |
| --- | ---: | ---: |
| Annotated occurrences fully masked | 27 / 52 | 27 / 52 |
| Annotated occurrences partially masked | 2 | 2 |
| Annotated occurrences not masked | 23 | 23 |
| Requests held for local review | 0 / 52 | 11 / 52 |
| Annotated occurrences still exposed in requests ready to send | 25 | 17 |
| Requests with no private values held for review | 0 / 15 | 3 / 15 |
| Extra or inexact mask spans | 13 | 13 |
| Harmless characters masked | 116 | 116 |
| Prepared requests restored exactly as Python text | 52 / 52 | 52 / 52 |

The 11 review holds contain eight requests with private values and three harmless
requests. Seven annotated occurrences are fully covered by review findings.
An eighth exposed occurrence happens to be in a request held for a different,
harmless finding. That incidental hold is **not detection of that secret**.
The other 17 exposed occurrences remain in requests ready to send.

Exact span/type precision is 65.79% and recall is 48.08% on these deliberately
selected examples: 25 exact true positives, 13 extra/inexact spans, and 27 exact
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
- **Remaining credential gaps:** ordinary session cookies, the tested SAS
  signature and webhook URL path, recovery codes, short unlabelled passwords,
  and the Polish/Spanish prose examples. The S3 signature example triggers an
  entropy review; this does not establish format-specific signed-URL support.
- **Remaining encoding/context gaps:** a percent-encoded credential field name,
  a Unicode-escaped JSON field name pasted as text, a short base64 credential,
  and credentials split across strings or sections. Parsed structured data is
  a different surface from pasted JSON text and needs separate evaluation.
- **Existing PII limits:** the name, address, birth date, passport, and driver's
  license examples require registration or additional detectors.
- **False positives:** code attribute/self references get masked; hashes, trace
  IDs, and explanatory prose can trigger review. Some URL credentials consume
  neighboring query parameters, and a YAML block marker is masked as a password.

Do not silently allow all hashes or code-looking values to improve a score:
real secrets can have those forms too. Each change needs positive and negative
examples and a clear decision about when Veil should ask locally.

## Run it yourself

From an updated source checkout:

```bash
uv run python benchmarks/leaks.py --check --output /tmp/veil-leak-results.json
uv run pytest -q tests/test_leak_evaluation.py
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
contain the imported source hash because the unreleased checkout still carries
the 0.5.0 version string.

## Scope still to validate

File examples here are **text contents carried in supported prompt/tool fields**.
This suite does not validate image/PDF OCR, tool definitions, authentication
headers, browser/client telemetry, remote tools, live provider behavior, or a
user's actual desktop routing. It does not simulate people making review choices
or assess inference from surrounding context. Local review only holds findings
it recognizes. Independently reviewed real-workflow sampling, longer sessions,
external beta testing, and security review remain [roadmap items](../ROADMAP.md).
