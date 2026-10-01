# Shareable diagnostics

`veil report` generates support JSON **locally**. Nothing is uploaded or sent.
It uses an explicit allowlist rather than trying to redact a raw diagnostic dump.

```bash
veil report > veil-report.json
veil report --client claude --gateway-url http://127.0.0.1:8484 > veil-report.json
```

In PowerShell, run `.\.venv\Scripts\veil.exe report | Out-File -Encoding utf8 veil-report.json`.
Use the gateway's actual port; the examples do not start or change a worker.
Custom locations can be selected with global `--data-dir` before `report`, and
`--config` after it. They are read locally and never copied into the exported JSON.

To include one existing fictional verification test, run
`veil report --verification ID`. Get the ID from `veil verify`; this option
checks that test but does not create a probe, send a model request, or include
the ID in the report. Restarting a gateway clears its activity and probes.

The report includes:

- Veil and Python versions, platform family, and a strict client **CLI** version
  when available. This is not the installed desktop app's build number.
- Known Codex configuration/doctor check names and states, without their details,
  commands, remedies, paths, or configuration values. Claude mode does not run
  Codex configuration checks.
- Worker state when available, authenticated gateway reachability, and aggregate
  request/forwarded/completed/failed counts from the gateway's bounded activity.
- An explicitly selected probe's state and masking/forwarding/restoration/completion
  booleans. No session identifiers, timestamps, prompts, custom type names, or originals.
- Fixed error codes such as `diagnostics_unavailable`, `gateway_report_unavailable`,
  and `verification_id_invalid`. Exception messages and client stderr are excluded.

Exit status is zero when the selected gateway is reachable and no failed check
or collection error is reported; otherwise it is one. JSON is still emitted
when the gateway is stopped. `null`, `unknown`, or `unavailable` means evidence
is missing. “Reachable” is not proof that the current task is routed through
Veil, and a completed probe covers only its one request.

Review the file, then deliberately attach it to an issue if useful. Add only a
fictional reproduction and a description of what you expected. `status --json`
and `doctor --json` are detailed local diagnostics and can contain paths; share
the dedicated `report` output instead. Never attach real prompts, configuration
files, tokens, authentication headers, transcripts, vaults, or personal paths.
