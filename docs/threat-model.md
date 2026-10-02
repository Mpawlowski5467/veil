# Veil threat model and storage decision

Status: maintainer review, 2026-09-29; independent review pending. Applies to the
0.6.0b1 checkout and the supported text workflows in the [compatibility matrix](compatibility.md).

## Assets and boundary

Protect detected text and registered values from supported model request bodies
sent to the provider. Preserve correct local restoration and session separation.
Keep the mapping vault, registrations, gateway secret, launch records, setup
backups, and client credentials out of diagnostics and source control.

The local client sends its request to a loopback gateway. Veil parses and masks
supported content, forwards the request over TLS using the client's credentials,
then restores supported reply content before returning it locally. A skill is a
local command guide; it cannot intercept text already sent in its invocation.
Clipboard use has a separate, explicit boundary: mask before pasting.

Trust the installed Veil code, Python/runtime, OS, client, local tools, and the
current OS account. The provider may retain what it receives. A malicious
process running as the same user, an administrator/root, or a compromised client
can bypass or read Veil. That is outside the protection this package provides.

## Threats, controls, and residual risks

| Threat | Control and evidence | Residual risk |
| --- | --- | --- |
| Private text in prompts, history, tool results | Request adapters, registered values, pattern detectors, replay ledger; gateway and live masking tests. | Unrecognized data is still text. Obfuscation, unsupported formats, and contextual identification remain possible. See [measurements](detection-results.md). |
| New client request shapes | Claude masks new text fields generically and refuses unmaskable keys, types, numeric/opaque content; OpenAI rejects unsupported shapes. Census, fuzz, and leak regression suites. | Client updates can change routing or semantics; revalidate after updates. |
| Incomplete/failed reply marked verified | Completion must follow closed content blocks and successful forwarding/restoration. Verification regression tests. | One test exchange proves only itself, not future routing or every data type. |
| Browser access or unrelated local callers | Bind loopback only, validate Host/browser headers, require gateway secret for model/activity routes. Non-secret identity/connection checks do not forward model calls. | A process in the same user account can read that account's secret. |
| Original text in metadata or unsupported surfaces | Documented text-only boundary; the OpenAI adapter refuses media/hosted tools. Claude image/PDF contents pass through and are not masked. | Authentication, routing/account headers, local client telemetry, direct tool network calls, and other client traffic are separate surfaces. Veil cannot make an authenticated provider unaware of the account's identity. |
| Tools sending restored information | Claude hooks and OpenAI tool-input guards check known private argument values; direct local patches are permitted. | A command can read a file and upload it without including private text in its arguments. User approvals and OS/tool sandboxing still matter. |
| Another local account reading storage | Unix private directories/files; Windows ACL validation allows the user, SYSTEM, and Administrators and rejects other grants. Native CI covers creation/inheritance and rejecting shared ACLs. | Administrators, same-account malware, backups, snapshots, and synchronized folders remain in scope of OS security, not Veil. Store databases in a private local folder. |
| Diagnostic leakage and memory growth | No gateway request/access logging; redacted CLI errors, salted bounded activity summaries, bounded refusal digests. | Direct Python warnings quote values unless `redact_warnings=True`; third-party client/debug logs can contain originals. Memory vaults and active session mappings grow with the conversation. |
| Retention/deletion mistaken for erasure | Explicit forget commands, retention on store open, temporary launcher storage, deletion tests. | Deletion is not forensic erasure and does not delete client transcripts, registrations, backups, clipboard history, or provider records. |
| Repository content changing privacy policy | Configuration comes from the selected user data folder, not project config; hooks use the installed runtime. | Project/client tools still execute code under their own permissions. |

## Storage and encryption decision

For the scoped first stable release, retain local plaintext SQLite storage with
private filesystem permissions. There is **no application-level vault encryption**
and no OS keychain integration. An encrypted database whose key is available to
the same compromised account would not fix the main excluded threat. OS-backed
key storage and encrypted exports need a separate recovery/key-lifecycle design;
they are deferred, not implicitly provided by disk encryption.

Use OS full-disk encryption and a private local data folder. Avoid shared/network
or cloud-synchronized folders. The Python `SQLiteVault` accepts caller-selected
paths, so its caller must also protect the containing directory and SQLite
sidecars. ACL checks on Windows accept privileged owners because elevated Windows
processes can create files owned by Administrators.

The vault contains original values and mappings. The ledger stores masked text,
masked tool inputs, and unsalted hashes of restored text or provider blocks.
Hashes can reveal a known low-entropy value by guessing; the ledger is private
data, not safe telemetry. Registered values and setup backups are also plaintext.

Mappings default to 30 days of inactivity. Purging occurs when storage opens,
not on a timer. Vault deletion enables SQLite secure deletion and attempts a WAL
checkpoint, but active readers, SSD wear leveling, snapshots, and backups prevent
an erasure guarantee. Old versions do not know the newer provider-block ledger
table; use the current version to forget data before rollback.

For fresh sessions use `veil --forget-after-run claude` or
`veil --forget-after-run codex`. This copies detector settings into a private
temporary data folder and removes it after a normal exit or handled failure.
It leaves existing data alone. Killing the process/OS can leave temporary files;
client transcripts still contain restored text. Do not rely on this mode to
restore replies later or resume an earlier masked conversation.

## Independent review brief

Review request traversal and unknown/opaque fields; JSON escaping and replay;
stream completion and tool delivery; session/cache isolation; local route/auth
checks; Windows ACL parsing/inheritance and Unix link handling; setup/undo
conflicts; deletion/WAL behavior; diagnostic paths; and inherited client/tool
context. Use fictional values and capture only evidence necessary to reproduce.
Record findings with affected versions, boundary, severity, regression, and
resolution. No unresolved known leak within the promised boundary or data-loss
issue may remain at 1.0. No independent review has been performed by this work.
