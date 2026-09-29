---
name: veil
description: Use the installed Veil package to check gateway readiness and request evidence, verify a client test request, guide Codex or Claude Code setup, and mask or restore local text files. Use when the user asks to use Veil or invokes the Veil skill.
---

# Veil

Call the installed Veil CLI for the requested operation. First read
[runtime.md](runtime.md) beside this file for its interpreter and command.
In the examples below, `VEIL` means that entire command, not a shell variable.
Check `VEIL --help` if the installed command differs from these instructions.

## Privacy boundary

Invoking this skill does not intercept the message that invoked it or reroute
an existing conversation. Never claim otherwise. Gateway readiness is not proof
that the current task used Veil. A running background worker may serve a different
client, and Claude Code's launcher manages its own separate gateway.

Do not ask the user to paste private values into the conversation. For file work,
pass the file directly to Veil locally; do not read, preview, grep, or attach its
original contents first. Do not print vaults, ledgers, gateway secrets, auth files,
or raw detector/client configuration. Use Veil's diagnostic output instead.
Restore into a local file or clipboard without returning restored contents in
tool output. A tool result containing original values can enter the next model
request when the session is not routed through Veil.

Veil covers supported, detected text. Email and phone detection are built in;
names need registration, and SSNs need a custom pattern. Undetected values can
remain in masked output. Do not promise complete anonymization. Local file access
and other tool/network traffic are not automatically covered by the gateway.

## Choose the requested operation

With no operation, check readiness for the known client and summarize the limits.
If the client or intended workflow is unclear, ask only for that missing detail.
A request for status does not authorize changing settings or restarting a worker.
Honor a data folder, config path, port, and auth mode already chosen in the
conversation. Do not replace them with defaults. Put `--data-dir PATH` before
the subcommand. `--config PATH` selects Codex's config file on setup, status,
doctor, start, stop, restart, or undo; it is not the detector config.

### Status and troubleshooting

- For Codex desktop readiness: `VEIL status --json`. For more detail:
  `VEIL doctor --json`. These check saved routing, gateway identity, and local
  settings and recent activity; they do not verify the active task or make model calls. Exit 1 means
  readiness needs attention, and exit 2 means a command/configuration error.
- For a managed background worker: `VEIL status --service --json`. This is a
  worker check only. An `unmanaged` result does not rule out a foreground gateway.
- For gateway activity, use `VEIL status --activity --json`. Launchers pass the
  local endpoint privately to their child tools; outside a launcher, this uses
  saved Codex settings. Never print environment variables to find the secret.
  A custom manual gateway can be selected with `--gateway-url URL` and the
  matching `--data-dir` holding its secret. Missing/old gateways need setup or
  an upgrade/restart, not a fallback to another route.
- If a verification ID from this conversation is known, use `VEIL status
  --verification ID --json`. Describe its evidence and opaque session reference.
  Otherwise, report readiness and activity only. Do not assume the newest or
  most recently verified session in the report is this conversation. Counts
  include repeated history, not unique people or newly masked values.
- Report failed checks and their remedies without dumping underlying files.
  Do not automatically change routes, restart a client, or repair configuration
  just because a diagnostic failed.

### Verify a conversation's request

Only create a test when the user asks to verify. Run `VEIL verify --json` to
register a one-use, ten-minute challenge with the intended gateway. Show the
returned fictional prompt and ask the user to send it as a new message in the
conversation they want to test. Sending that prompt uses the client's normal
model account. Creating/checking the challenge only contacts the local gateway.

Do not simulate the client with curl, launch a separate client, or put the prompt
into a tool result as a substitute: that would not verify the user's intended
conversation. Only its latest plain user message can consume the challenge.
If the user sends the generated "Do not use tools" test prompt, answer it as
requested; check evidence on their subsequent request, after the reply finishes.

After that reply, use `VEIL verify --check ID --json` with the same endpoint, or
`VEIL status --verification ID --json`. A `verified` result means this gateway
observed the fictional email masked in an outbound request, forwarded it, then
restored it in a successfully completed response for the reported session.
It proves that test request, not future routing, all sensitive data, UI rendering,
or other tool traffic. A model merely echoing text is not proof without evidence.
`pending` means not observed yet; `in_progress` means wait for the reply;
`incomplete` means some checks failed. `expired` and `unknown` need a new probe.
Never call any of those states verified. Evidence resets on gateway restart.

### Enable or start

- **Codex desktop, fresh setup:** when the user requests setup, run
  `VEIL setup codex`, then `VEIL start`, then `VEIL status --json`, checking each
  result before proceeding. Setup requires the `desktop` extra. On a missing
  dependency, help install that extra in the same Veil environment. Setup keeps
  a private backup and preserves unrelated configuration. For an existing setup,
  inspect status first and reuse its data folder and provider settings. If an
  older manual setup has no data-folder receipt, obtain the intended folder
  before configuring or starting another gateway.
- Explain that the user must restart Codex and open a fresh **local** task after
  setup; do not quit the app yourself. Existing tasks and cloud tasks can use
  other routes. Report readiness, never "this conversation is now protected."
- **Claude Code:** provide the installed runtime command followed by `claude`,
  with the intended `--data-dir` before it, for the user to launch in a terminal.
  **Codex CLI:** similarly use `codex --` (ChatGPT login by default) or
  `codex --auth api-key --`. These launchers route the newly launched client;
  starting an interactive client inside an assistant tool does not switch the
  current conversation. Do not do that as a substitute for a fresh session.
- `VEIL start` starts only the background worker. macOS/Linux are supported;
  Windows background management is not. For a Windows desktop workflow, provide
  `VEIL gateway --api openai --auth chatgpt --port PORT` to run in a terminal,
  matching the selected setup. Do not capture or share its printed secret.
- Stop/restart only when requested. They can interrupt active model calls.
  `VEIL undo codex` reverses managed setup; stopping the worker alone leaves
  Codex pointing at it. Never use `forget` as a setup or troubleshooting step.

### Mask or restore a file

1. Get the source path, a new output path, and a non-sensitive session label.
   Use one label per conversation; mask and restore must use the same data
   folder and label. Resolve paths without reading the source. Do not use a
   person's name or email as the label.
2. Send the source to the CLI through stdin and direct stdout into the new
   output file. Do not put the contents in command arguments, a heredoc, or a
   tool response. On POSIX shells the pattern is:

   ```sh
   (umask 077; set -C; VEIL --data-dir DATA mask --session LABEL < INPUT > OUTPUT)
   (umask 077; set -C; VEIL --data-dir DATA restore --session LABEL < INPUT > OUTPUT)
   ```

   Substitute and shell-quote each path/label; use the real runtime command.
   The examples are alternatives, not a sequence to run automatically. On other
   shells, use local subprocess file handles with exclusive output creation
   and UTF-8-compatible I/O. Preserve the source and refuse existing outputs.
3. Check the exit code. Veil emits no transformed output on reported masking or
   restoration warnings. A failed redirection workflow may leave an empty output
   file; report failure and do not treat it as a successful result.
4. Report the output path and session label. Do not read the restored output
   back into the conversation. Only inspect masked output if the user requests
   it, explaining that detection can miss values. The user can inspect original
   or restored files locally in their editor.

When explicitly asked to use the clipboard, `VEIL mask --session LABEL
--clipboard` and `VEIL restore --session LABEL --clipboard` read and replace it
locally. Do not preview clipboard contents first. Use the same data folder and
session label; these commands print only completion status.
