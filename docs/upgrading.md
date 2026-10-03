# Upgrade, rollback, and recovery

Use a private local data directory. It contains plaintext original values and
must stay separate from public issue attachments and source control.

## Upgrade

1. Finish work, exit all Veil-launched clients, and stop a background gateway
   with `veil stop`. A foreground gateway stops with Ctrl-C.
2. With all writers stopped, back up the **whole** selected data folder and the
   Codex configuration/setup receipt/backups. Preserve private permissions or
   Windows ACLs. Include SQLite sidecars if present; do not copy only a database
   while a gateway is writing it. Keep a copy of the old wheel/interpreter too.
3. Install the candidate wheel in a separate environment, including `[desktop]`
   when using Codex setup. Install the skill from that environment so its pinned
   runtime points to the candidate.
4. Use the same data folder, restart/relaunch, then run status/doctor and a fresh
   verification exchange. Preview a fictional mask/restore round trip.
5. Keep the private backup until the upgrade is accepted. Do not run old and new
   gateway versions concurrently against the same folder during migration.

## What was tested

`benchmarks/upgrade.py` runs **previous → 0.6.0b2 → previous → 0.6.0b2** in separate
installed environments, for previous releases **0.4.1, 0.5.0, and 0.6.0b1**. It verifies
stable existing placeholders, new person and confirmed-password mappings,
old masked ledger text, additive provider-block hashes, and deletion after
returning to the candidate. The test uses fictional local data and no provider.
CI performs this installed-wheel check on Linux. This does not promise downgrade
compatibility with every historical/future
schema or client conversation. Native Windows privacy validation is new in 0.5.0;
rolling back to 0.4.1 on Windows loses those checks.

For the 0.6.0b1 path, both gateways create persistent mappings using the installed
old detector, then reopen them with 0.6.0b2. Previously stored types and credential
words such as `String`, `api_key`, and `password` stay readable after upgrade;
stored weak passwords such as `postgres` and `letmein` mask whole words while
leaving `postgresql` and `letmein_old` readable. Every old placeholder still
restores, including the old false positives, through upgrade, rollback, and
return. A quoted weak password added in 0.6.0b2 also restores under 0.6.0b1.
Rollback restores the old detector's broader matching as well as its known
misses; preserving mappings does not backport the fixes.

The provider-block ledger table is additive. An old release ignores it and its
forget command does not know to delete it. Use the candidate to forget data
before rolling back. Prefer restoring the complete pre-upgrade backup over
mixing files from different points in time. Mappings created after that backup
will be absent, so later replies may no longer restore.

## Rollback and failure recovery

The 0.6 beta configuration can contain `secret_review`, which releases before
0.6.0b1 do not accept. Restore the old configuration from the private backup as well as
the old runtime; do not expect new commands or detection rules after rollback.
Mapping compatibility does not make an older detector equivalent to the beta.

Stop every writer first. Restore the complete private backup into its original
folder, reinstall the previous wheel, reinstall the matching skill, and relaunch.
If Codex setup is unwanted, run `veil undo codex`; it refuses conflicting edits
rather than overwriting them. Resolve conflicts against the saved private
backup without sharing its secrets. `veil stop --remove` removes worker control
state; it does not erase mappings, registrations, or Codex transcripts.

If a lock remains after a crash, confirm the process that held it is no longer
running before removing that specific lock. Do not delete database files to
“fix” a locked database. If restoration has warnings, keep the original reply
and the data folder; do not send partially restored output onward automatically.

Temporary sessions (`veil --forget-after-run claude` or
`veil --forget-after-run codex`) intentionally lose their mappings after exit.
They are for fresh disposable sessions, not an upgrade/resume strategy. Abnormal
process termination can leave a `veil-run-*` temporary folder; inspect and remove
only the abandoned folder once its process has stopped. Deletion is not secure
erasure, and client transcripts/backups remain separate.

## Retry and resume

Keep the same data directory and client conversation/session identity when
resuming. Persistent mappings and the reply ledger survive gateway restart;
activity evidence, verification probes, held reviews, and ignore decisions do
not. A confirmed masking choice is persisted when the retry reaches the masker.
After a restart, rerun verification and review any newly held request.

Rate limits and incomplete streams do not erase mappings. Retry in the same
session after the provider permits it. Veil does not replay a cancelled request
automatically. If an API stream ends before completion, it reports a stream
error; a partial answer is not verification success. Upstream work already sent
can still consume quota even when you cancel locally.

The regression suite uses fictional local providers: 36 turns across three
gateway instances per adapter, growing replayed history, forced session-cache
eviction, 32 concurrent conversations, client disconnects, rate-limit recovery,
and incomplete streams. This evidence does not replace long real-client beta
journeys, provider outage testing, or independent security review.
