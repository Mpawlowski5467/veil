# Veil roadmap

Veil is an alpha with working text masking/restoration, a Python API, Claude Code
and Codex gateways, and explicit clipboard workflows. The next goal is a beta
that a new user can install, verify, troubleshoot, and remove without help.

This is an ordered plan, not a release-date commitment. Checked items are
implemented in this branch; they do not imply a published package or validation
of every supported client feature. The current package version remains 0.4.1.

## The 1.0 promise

**Mask supported, detected text locally before supported model calls, then
restore it locally in supported replies.** Make the active route and limits
understandable. Never imply that all computer traffic or every secret is covered.

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
- [ ] Maintain labeled, fictional test corpora; measure missed values and false
  positives by entity type, format, and language, plus masking latency.
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
- [ ] Run native clipboard checks on supported operating systems.
- [ ] Publish a client/version/feature matrix and rerun compatibility checks
  after client updates. Unsupported request formats must fail clearly.

**Exit:** supported workflows work repeatedly under normal use and realistic
failures, and regressions have reproducible tests. Unsupported features are explicit.

## 5. Private storage, review, and stable release

- [ ] Offer a clear forget-after-run mode for the launchers and understandable
  retention/deletion controls. Explain that client transcripts are separate.
- [ ] Decide on vault encryption and OS-backed key storage against a written
  threat model. Owner-only plaintext files are the current behavior.
- [ ] Review how tool execution, shell reads/network access, inherited context,
  metadata, and alternate routes can bypass the model-request boundary.
- [ ] Arrange an independent security review before making stronger privacy claims.
- [ ] Publish stable API/configuration contracts, migration/rollback instructions,
  a compatibility matrix, and installable release artifacts.
- [ ] Complete a small external beta and resolve release-blocking findings.

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
