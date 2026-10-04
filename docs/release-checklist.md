# Beta and 1.0 release checklist

The 0.6.0b2 checkout is a beta candidate under evaluation. It is not Veil 1.0.
All three target platforms — macOS, Linux, and native Windows — require evidence.
Windows background service management is explicitly outside the current scope.

## Automated candidate checks

- Full Linux suites on Python 3.10–3.14, lint, formatting, both type checkers.
- Native macOS/Linux/Windows suites on Python 3.10 and 3.14, including private
  storage, Unicode registration, mask/restore, forgetting, skill install/remove,
  Codex setup/undo, protocol regressions, clipboard, and clean wheel installs.
- Separate Windows installed-wheel checks under two ordinary local accounts,
  verifying non-administrative tokens, setup/undo, session separation, and real
  cross-account denial for private files and live SQLite sidecars. These remain
  scripted CLI/storage checks, separate from installed-client and human journeys.
- Install the base wheel without desktop dependencies, then add `[desktop]` and
  exercise setup/undo and the skill's pinned runtime from outside the checkout.
- Upgrade/rollback test using separate installed 0.4.1, 0.5.0, 0.6.0b1, and candidate wheels,
  including mappings actually created by the b1 secret detector.
- Live Codex ChatGPT and Claude runs; API-key runs separately with local keys.
- Reproduce [workflow measurements](leak-evaluation.md) against the unchanged corpus; explain per-case differences.
- Reproduce the separate [detection challenges](detection-challenge.md), retaining
  their documented misses and false positives rather than removing difficult cases.
- Run the local preview browser checks, report allowlist tests, and both adapters’ restart/concurrency/recovery scenarios.

CI uploads wheel/source distributions as `veil-distributions-from-*`. These are review
artifacts, not a PyPI release. Download artifacts only from the intended commit's
successful run. Never include personal logs, credentials, vaults, or transcripts.

## External beta protocol — participants still needed

Give participants the versioned **self-serve beta pack ZIP** on the release page.
Its [instructions](../beta/README.md), fictional workspace, local check script,
and [feedback worksheet](../beta/feedback.md) are also available in this repository.
The short first test is separate from [optional deeper exercises](../beta/extra-tests.md).
Use the [invitation copy](beta-launch.md) when sending it to participants.
The script records only software versions and fixed local-check outcomes and
makes no model calls. Client exercises are manual and reports are sent only by
the participant. Track aggregate outcomes in [beta results](beta-results.md).

Recruit at least one new user on each OS, with both clients represented. Use
invented contact details first. Record exact OS, Python, Veil commit, client
version, authentication mode, and steps that needed maintainer help.

Each participant should:

1. Install from the candidate wheel and install the skill.
2. Register a fictional name, launch the intended route, and complete a verified
   request. Explain which traffic the verification actually covers.
3. Work through file reading/editing, several conversations, resume, longer
   history, and concurrent sessions using fictional material.
4. Stop the gateway, confirm the failure is understandable, restart, and verify
   again. Exercise a cancelled turn and an upstream failure without losing data.
5. Upgrade the client, rerun verification, and report incompatible shapes.
6. Upgrade Veil, test restoration, exercise rollback from a private backup, and
   remove routing/skill configuration without losing unrelated settings.
7. Try forgetting and temporary mappings; confirm client transcripts remain.

Use an issue containing this safe report template:

```text
Veil commit / version:
OS and Python:
Client / version / authentication mode:
Scenario:
Expected result:
Observed result and redacted error code:
Verification state (not the secret or raw request):
Reproduction using invented values:
Maintainer assistance required:
Severity / workaround:
```

Do not ask participants to upload real prompts, vaults, secrets, transcripts, or
full configuration files. Publish a sanitized findings log and resolve blockers.
No participant testing has been claimed or performed by the automated suite.

### Candidate publication

Build from the intended commit after its full CI succeeds. Publish the wheel,
source distribution, beta pack ZIP, and SHA-256 checksums together as a GitHub
prerelease. Run the pack's check using the installed wheel outside the checkout.
The beta archive has an explicit file allowlist; never archive a working data
directory, environment, generated feedback, or live-test recordings.

Changing the client version recorded in the compatibility matrix requires an
actual observed check with that version. A selected live smoke suite does not
advance the full Claude request census or validate every model/feature.

## Stable contracts and upgrade policy

For 1.0, commit to the public `Shield`, result types, detector/vault protocols,
and documented CLI/configuration fields within a major version. Unknown config
keys fail visibly. Experimental gateway request shapes and client integration
behavior remain versioned in the [matrix](compatibility.md), not an assertion
that every future client release works. Pre-1.0 changes remain possible.

Follow [upgrade and rollback](upgrading.md). Prepare release notes, checksums,
license/source distributions, an install command for each OS, and updated
compatibility/detection results. Tag and publish only the exact verified commit;
never rename a tag to silently replace an artifact. A registry publish workflow
is not enabled by this preparation.

## Required human gates before 1.0

- [ ] New-user beta journeys completed on macOS, Linux, and native Windows.
- [ ] Live OpenAI API-key mode and advertised client/platform combinations tested.
- [ ] Desktop UI journeys and long-session/client-update behavior checked beyond
  the scripted app-server and request-corpus tests.
- [ ] Independent security review completed, with findings resolved or scope
  explicitly reduced. See the [review handoff](security-review.md).
- [ ] No known leak within the supported boundary or unresolved data-loss defect.
- [ ] Maintainer signs off on the documented scope and release artifacts.

These are genuine open gates. Automated tests and prepared documents cannot
replace other people using and reviewing the project.
