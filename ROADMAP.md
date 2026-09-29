# Veil roadmap

Veil is an alpha with working text masking/restoration, a Python API, Claude Code
and Codex gateways, and explicit clipboard workflows. The next goal is a beta
that a new user can install, verify, troubleshoot, and remove without help.

This is an ordered plan, not a release-date commitment. Checked items are
implemented in this branch; they do not imply a published package or validation
of every supported client feature. The current package version is 0.5.0; this is not a 1.0 release.

## Where we are now

**Stage: late alpha, preparing for a small external beta.** The core workflow is
implemented; the remaining 1.0 work centers on evidence from real use, privacy
review, and a dependable release process.

| Area | Implemented in this checkout | Still needed for 1.0 |
| --- | --- | --- |
| Mask and restore | Python API, persistent mappings, streaming, built-in patterns including US SSNs, and private entity registration. | Expand the published 30-document fictional baseline with independently reviewed, representative samples. |
| Setup and daily use | Codex setup/undo, CLI launchers, assistant skills, background controls, diagnostics, and request verification. | New users completing install, verification, recovery, and removal on each advertised platform. |
| Client compatibility | Claude Code and experimental Codex adapters, scripted regression tests, and selected live round trips. | Broader desktop UI and long-session coverage, live API-key checks, and real client journeys on every target OS. |
| Privacy and storage | Local masking, owner-only storage, bounded activity metadata, retention, and explicit deletion commands. | Independent review and resolved findings; the threat model and plaintext-storage decision are now documented. |
| Release readiness | Cross-platform CI, package artifacts, wheel smoke checks, and 0.4.1 upgrade/rollback checks. | Complete the external beta and resolve release-blocking findings. |

The pending compatibility, verification, and detection branches are integrated
and tested together in the readiness candidate. Passing unit tests or one echo demonstration is not
enough to call the advertised workflows stable.

The next sequence is:

1. Keep all candidate checks green, including native Windows, before sharing
   the built wheel with beta participants.
2. Run a small beta with people new to Veil, recording setup friction and failures
   during ordinary work, including client updates and long conversations.
3. Publish detection and compatibility results, complete the privacy/storage
   review, and fix release-blocking findings.
4. Ship 1.0 only when the [release gate](#10-release-gate) is met.

Automatic login startup, local name recognition, secret/token detection, and
additional media/providers are useful extensions. They can remain outside 1.0
when the supported scope and exclusions are clear. The storage/security decisions
and validation for the features we do advertise still need to be completed.

## The 1.0 promise

**Mask supported, detected text locally before supported model calls, then
restore it locally in supported replies.** Make the active route and limits
understandable. Never imply that all computer traffic or every secret is covered.

macOS, Linux, and native Windows are all 1.0 targets; Windows validation is
required. See the [evidence and exclusions](docs/compatibility.md).

The first stable scope is the Python library and supported local text workflows
in Claude Code and Codex. The explicit mask/restore helper can support other chat
apps. Automatic interception of ordinary ChatGPT chats is outside this scope.

## 1. Setup, readiness, and recovery — implemented; beta validation next

- [x] `veil setup codex`: preserve unrelated TOML settings and comments, save a
  private backup, and select a local provider with supported features.
- [x] `veil undo codex`: restore managed settings, preserve unrelated later edits,
  and stop if a managed setting conflicts with a user's later change.
- [x] `veil status`: inspect saved routing and verify the local gateway's identity
  and API/auth mode. Clearly distinguish readiness from active task coverage.
- [x] `veil doctor`: add actionable configuration, storage, local round-trip, and
  credential-environment checks without dumping private values or making a model call.
- [x] JSON diagnostic output and optional desktop dependencies; keep the base
  Python library free of runtime dependencies.
- [ ] Test the install/setup/undo journey with new users on each platform we
  intend to advertise. Record where they need help.
- [x] Add CI wheel smoke checks for the base library and desktop setup/status/undo.

**Exit:** a new user completes setup, recognizes a stopped or mismatched gateway,
and undoes setup without manually editing TOML or losing unrelated settings.

## 2. Everyday operation and visible evidence — in progress

- [x] Ship an installable assistant skill for Codex and Claude Code, with setup,
  readiness, and local file workflows that distinguish skill invocation from
  gateway protection. See [assistant skills](docs/assistant-skills.md).
- [x] Manage a detached gateway on macOS/Linux with `start`, `stop`, `restart`,
  and `stop --remove`. Check startup, preserve client settings/mappings, refuse
  occupied ports, and recover from stale state without signalling saved PIDs.
  See [operation, crash recovery, and upgrades](docs/background-gateway.md).
- [ ] Add automatic login/reboot startup and supervised crash recovery after
  defining installation/removal behavior for each OS. Windows background
  management remains unsupported; the foreground gateway remains available.
- [x] Provide an opt-in, one-use verification prompt that proves a particular
  client request reached Veil, was masked, and had its reply restored. Evidence
  identifies the observed session and expires; it does not prove future routing.
- [x] Show bounded in-memory activity with request times and placeholder counts.
  Include replayed history in counts, group custom types, salt session IDs, and
  exclude original values, prompts, and authentication headers from reports.
- [x] Distinguish worker states, gateway readiness, recent test evidence, and
  unknown/expired probes without implying every client or task is covered.
  See [verification and activity](docs/verification.md).

**Exit:** users can tell whether their intended task used Veil, recover after a
restart, and troubleshoot without keeping a terminal open or sharing raw requests.

## 3. Detection that matches everyday documents

- [x] Add a tested built-in US SSN detector with explicit supported formats and
  false-positive rules; replace the ad hoc demo regex for normal use.
- [x] Add easy commands to register, list, and remove names, organizations, and
  custom values, with private storage and clear case/variant behavior.
- [ ] Evaluate opt-in secret/token detection against realistic examples before
  promising API-key or password coverage.
- [x] Maintain labeled, fictional test corpora; measure missed values and false
  positives by entity type, format, and language, plus masking latency.
  [Initial 30-document baseline](docs/detection-results.md); broader sampling remains.
- [ ] Investigate optional local name recognition. Keep it optional and publish
  its limitations; don't silently send unmasked text to a remote detector.

**Exit:** published coverage matches measured behavior. Users understand which
values need registration and can preview a representative document locally.

## 4. Real workflow compatibility

- [ ] Test multi-turn conversations, resume, concurrent sessions, file edits,
  restored tool arguments, cancellations, retries, and provider failures.
- [ ] Address long-session compaction: either support it safely or give an
  actionable recovery path while keeping masking intact.
- [ ] Complete real API-key tests separately from ChatGPT sign-in tests.
- [ ] Expand desktop testing beyond the verified basic text round trip.
- [x] Run native clipboard checks on macOS, Windows, and Linux X11. Native
  Wayland remains unvalidated.
- [x] Publish a [client/version/feature matrix](docs/compatibility.md) and a
  repeatable census for client updates. Unsupported request formats must fail clearly.

**Exit:** supported workflows work repeatedly under normal use and realistic
failures, and regressions have reproducible tests. Unsupported features are explicit.

## 5. Private storage, review, and stable release

- [x] Offer a clear forget-after-run mode for the launchers and understandable
  retention/deletion controls. Explain that client transcripts are separate.
- [x] Decide on vault encryption and OS-backed key storage against a written
  [threat model](docs/threat-model.md). Private plaintext files remain the scoped
  default; application encryption/keychain support is explicitly deferred.
- [x] Review how tool execution, shell reads/network access, inherited context,
  metadata, and alternate routes can bypass the model-request boundary.
- [ ] Arrange an independent security review before making stronger privacy claims.
- [x] Publish the planned 1.0 contracts, [upgrade/rollback instructions](docs/upgrading.md),
  compatibility matrix, and installable CI artifacts. Registry publishing and
  stable release approval remain manual gates.
- [ ] Complete a small external beta and resolve release-blocking findings.

The [beta protocol and release checklist](docs/release-checklist.md) records the
human validation still required. Automated CI is not an external beta.

## 1.0 release gate

Release 1.0 when the scoped promise above is dependable, with:

- Successful install, verification, recovery, and removal on advertised platforms.
- Passing real-workflow and privacy regression suites for advertised integrations.
- Published detection measurements, supported surfaces, and known limitations.
- No unresolved known leaks inside the supported boundary or data-loss defects.
- Explicit local storage behavior and tested upgrade/rollback procedures.
- Beta users completing normal work without maintainer assistance.

Large test counts and a successful echo demo are useful evidence, but neither
alone establishes these criteria. Features can remain excluded when the product
refuses them clearly and does not promise their coverage.

## Later, after the supported text workflows are dependable

Consider a small desktop/menu-bar interface, optional local name recognition,
document/PDF processing, image/OCR and voice workflows, more provider adapters,
and team policies. Each expands the privacy boundary and needs its own design
and validation. A broad browser extension or automatic protection across every
chat application is a separate project, not a prerequisite for this 1.0.
