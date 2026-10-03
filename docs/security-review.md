# Independent security review handoff

Prepared 2026-10-03. **An independent reviewer has not yet been engaged.**
Agent audits, passing tests, and this document do not close that release gate.
Private vulnerability reporting is enabled for the repository; use the
[private report form](https://github.com/Mpawlowski5467/veil/security/advisories/new)
for findings. See [SECURITY.md](../SECURITY.md) for the reporting policy.

## Pin the material under review

The published baseline is **0.6.0b2**, tag `v0.6.0b2`, commit
`8125209db73be01ad36ded261cdc7de1a2efdf98`. Its
[release](https://github.com/Mpawlowski5467/veil/releases/tag/v0.6.0b2) contains
the wheel, source distribution, fictional beta exercises, and `SHA256SUMS`.
The tag must remain immutable. Subsequent fixes live on `main` until released;
record their full commit ID separately from the installed version string.

In a fresh checkout of the chosen commit:

```bash
git rev-parse HEAD
uv sync --locked
uv run python -c 'import veil; print(veil.__version__)'
uv run pytest -q -m 'not live'
uv run python benchmarks/leaks.py --check --output /tmp/veil-review-leaks.json
uv run python benchmarks/challenge.py --check --output /tmp/veil-review-challenge.json
```

On Windows choose writable local output filenames instead of `/tmp/...`.
Record the OS, account privilege level, Python version, selected commit, and
test summary. Skipped tests are gaps. The default suite uses fictional data
and loopback providers; it is not a live client or desktop UI assessment.
The beta pack's `check.py` exercises the installed wheel outside the checkout.

## Boundary to assess

Read the [threat model](threat-model.md),
[compatibility matrix](compatibility.md), and
[detection limitations](coding-secrets.md) before testing. Assess these claims:

1. Supported requests route through the gateway, and detected or explicitly
   registered text is replaced before forwarding. Unknown request structures
   must follow each adapter's documented masking/refusal policy.
2. A request-side refusal or unresolved review hold before forwarding sends no
   model request upstream. Response-side refusals occur after forwarding and
   must withhold unsafe tool delivery or stop an incomplete stream without
   claiming success. Diagnostics must not echo private input or credentials.
3. Responses restore only the correct session's values; restart, eviction,
   retries, concurrency, and incomplete streams preserve that boundary.
4. Local browser and gateway routes enforce their authentication and origin
   rules. Other ordinary OS accounts cannot read private storage or use
   protected interfaces under the documented platform guarantees.
5. Setup, rollback, removal, and forgetting preserve unrelated settings and
   satisfy their stated retention behavior without claiming forensic erasure.

The trusted current OS account, administrators, client/local tools, and installed
runtime can access originals. Veil does not protect arbitrary encodings, unknown
private values, direct tool network traffic, or Claude image/PDF contents.
Authentication/account metadata and client telemetry are separate surfaces.
These exclusions should be assessed for clarity as well as technical accuracy.

## Review map

Existing tests are starting points, not proof that each area is complete. Add
new adversarial examples without removing hard cases or relabelling misses.

| Area | Implementation | Starting tests and reviewer exercise |
| --- | --- | --- |
| Request traversal and refusal | `gateway/request.py`, `gateway/openai_request.py` | `test_gateway_request_fuzz.py`, `test_gateway_blocks.py`, `test_openai_gateway.py`; mutate unknown fields, nested strings, keys, numeric and opaque values. Assert masked output or zero upstream calls. |
| Replay, escaping, and streams | `gateway/ledger.py`, `gateway/response.py`, `gateway/openai_response.py` | `test_gateway_golden.py`, `test_gateway_response.py`, `test_stream.py`; test replay after restart, escaped tool JSON, interrupted streams, and final completion accounting. |
| Session separation and recovery | `gateway/store.py`, `gateway/server.py` | `test_workflow_reliability.py`, `test_compaction_recovery.py`; mix concurrent sessions, evict caches, retry after cancellation, and confirm mappings survive refused compaction. |
| Local authentication and browser access | `gateway/server.py`, `review_cli.py`, `preview.py` | `test_gateway_server.py`, `test_secret_review.py`, `test_preview.py`, `test_launch_review.py`; challenge Host/Origin checks, launch-code reuse, gateway tokens, and Linux cross-account connections. |
| Private files and persistence | `gateway/config.py`, `_windows.py`, `vault/sqlite.py` | `test_gateway_store.py`, `test_sqlite_vault.py`, `test_platform_workflows.py`; inspect links, owners, ACL inheritance, SQLite sidecars, deletion, and failures under a normal Windows account. |
| Tool delivery and configuration | `gateway/hooks.py`, `gateway/openai_tools.py`, `codex_setup.py` | `test_gateway_hooks.py`, `test_codex_setup.py`, `test_launchers.py`; examine restored arguments, hook failures, config conflicts, setup/undo preservation, and launch cleanup. |
| Diagnostics | `diagnostics.py`, `support_report.py`, `gateway/activity.py` | `test_diagnostics.py`, `test_support_report.py`, `test_verification.py`; inject fictional secrets into errors and unexpected fields, then inspect every emitted report. |
| Detection precision and misses | `detectors/_secrets.py`, `_review_context.py` | `test_secret_precision.py`, `test_detection_challenge.py`; separately record exposed occurrences, partial/inexact masks, harmless changes, review holds, and exact restoration. |

Implementation paths above are relative to `src/veil/`; test paths are relative
to `tests/`. Native CI is useful evidence, but its elevated Windows runner does
not establish ordinary-user isolation. Use disposable accounts or virtual
machines for cross-account checks. Do not use personal data as attack material.

## Internal agent audit follow-up, 2026-10-03

The internal audit reproduced one route-validation gap in the published b2
baseline: a request to `/v1/messages?private=fictional-audit@example.org`
reached a fake upstream with that query unchanged even while the body email was
masked. Request-target fragments and absolute-form URLs could also survive
path parsing. This concerns URL targets outside the documented request-body
masking promise; it is still a useful restriction on unintended forwarding.

The unreleased fix accepts only origin-form model targets. Anthropic's two
model routes allow no query or exactly `?beta=true`; OpenAI retains its existing
model-catalog query validation. The regression in `tests/test_anthropic_routes.py`
checks both adapters, fixed nonretryable errors without value echoes, zero
upstream requests for invalid targets, and rejection before waiting for a body.
The focused route/server/OpenAI suites passed 206 tests locally. The published
b2 artifacts do not contain this follow-up.

An outside reviewer should reproduce the old behavior at the pinned baseline
and independently retest the updated commit. This audit does not assert that
all request, storage, stream, or authentication defects have been found, and
does not close the independent-review gate.

## Known gaps and evidence to obtain

- The frozen 44-request challenge still contains intentional misses. Consult
  its [current per-case results](detection-challenge.md), including code-shaped
  bare passwords, arbitrary cookie names, fragmented values, and unregistered
  name variants. Do not turn a held request into an automatic masking success.
- Live API-key routing requires an account owner to provide a key privately in
  their local environment. Never include it in a prompt, command argument,
  screenshot, issue, or submitted test artifact.
- A new user must complete each platform journey. Automated CLI/app-server
  scripts cannot establish desktop UI behavior, natural long sessions, or
  usability. Use the [beta worksheet](../beta/feedback.md) and record untried
  steps explicitly in [beta results](beta-results.md).
- Application-level vault encryption and key recovery are deferred. Inspect
  the actual plaintext-storage promise and OS permissions instead of assuming
  encryption exists.

## Findings and completion

For each finding, submit this information privately using fictional values:

```text
Finding identifier and short title:
Reviewer and review date:
Full source commit / installed version:
OS, account privilege level, client version and auth route:
Claim or boundary being tested:
Preconditions and minimal reproduction:
Expected behavior / observed behavior:
Impact and proposed severity:
Sanitized evidence, including whether any upstream request occurred:
Regression test:
Fix commit and independent retest result:
```

Separate verified defects from suspicions and documented exclusions. Preserve
failed cases and explain any scope reduction. A finding is resolved only after
its regression fails before the fix, passes afterward, and the reviewer retests
the affected boundary. A changed documented promise needs an explicit decision.

Before closing the independent-review gate, record the reviewer, exact commits,
surfaces and platforms examined, methods, findings, fixes, retest outcomes, and
unexamined areas. The reviewer must be independent of the implementation work;
another coding agent's review does not satisfy this requirement. Any unresolved
leak within the supported boundary or data-loss defect blocks 1.0. Passing this
review does not replace the other [release gates](release-checklist.md).
