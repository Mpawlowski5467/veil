# Veil 0.6.0b2 self-serve beta

This pack uses invented contacts and nonfunctional credentials. Start with a
10-minute local check, then allow 20–40 minutes for your normal AI client. Longer
sessions, upgrades, and rollback can be reported later. No report is sent
automatically. A local check does not establish that a client request used Veil.

## 1. Install in a new environment

Download the wheel and beta pack from the
[0.6.0b2 release](https://github.com/Mpawlowski5467/veil/releases/tag/v0.6.0b2).
Extract the pack into a new folder. You need Python 3.10 or newer and, for the
client exercises, an installed and signed-in Claude Code or Codex client.

On macOS/Linux, in the extracted folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install 'veil[desktop] @ https://github.com/Mpawlowski5467/veil/releases/download/v0.6.0b2/veil-0.6.0b2-py3-none-any.whl'
python check.py
```

On native Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install 'veil[desktop] @ https://github.com/Mpawlowski5467/veil/releases/download/v0.6.0b2/veil-0.6.0b2-py3-none-any.whl'
.\.venv\Scripts\python.exe check.py
```

You can activate the environment with `.\.venv\Scripts\Activate.ps1` for the
remaining commands. If PowerShell does not permit activation, use the environment's
Python directly instead of `python`: `.\.venv\Scripts\python.exe` from this folder,
or `..\.venv\Scripts\python.exe` after entering `workspace/`. No execution-policy
change is needed. Keep using the same interpreter throughout the exercise.

`check.py` exercises registration, masking, restoration, forgetting, and removal
in a temporary folder. All five steps should pass. It writes only software
versions and fixed outcome codes to `beta-check.json`; it does not read your
client configuration, contact a provider, or touch the clipboard. On a repeat
run choose another filename: `python check.py --output beta-check-2.json`.

## 2. Launch a fictional workspace

Open `workspace/` in your terminal. Use a dedicated data directory throughout:

```text
python -m veil --data-dir ../beta-data entities add PERSON
```

At the hidden prompt, enter **Mira Quill**. Launch one client:

```text
python -m veil --data-dir ../beta-data claude
python -m veil --data-dir ../beta-data codex
```

Choose one command, not both in the same terminal. Codex uses ChatGPT sign-in
by default; append `--auth api-key` only when `OPENAI_API_KEY` is already available
in your own environment. Never paste an API key into a prompt or report.

In a second terminal, activate the same environment, change to `workspace/`,
and run:

```text
python -m veil --data-dir ../beta-data verify
```

Send the exact generated prompt as a new message in the launched client. Then
run the printed `verify --check` command with the same `--data-dir`. Require
**verified**, not just an echoed email. If several gateways are running, use
the explicit `--gateway-url` shown by Veil to choose the intended one.

This proves that one exchange was masked and restored. Images/PDFs, direct
tool network traffic, and unknown secret formats are not covered by that proof.

## 3. Try normal work and detection boundaries

Ask the client to read `contact.txt`, change `Project: Orchard demo` to
`Project: Orchard follow-up`, and quote the contact email in its reply. Check
the file locally and confirm the real fictional email appears in the reply.
Ask it to explain `example.py`; its configuration references should be readable.

Inspect `example.env` locally. The quoted database password is supported, but
the bare `ADMIN_PASSWORD=admin` is deliberately treated as ambiguous code and
can go out unchanged, even with review enabled. Quote it or register `admin`
as `PASSWORD` if it must be private. Report whether this rule was understandable.
Do not interpret a successful example as protection of every credential.

To try review, exit the client and place this in `../beta-data/config.json` (the
folder created by this pack's commands), preserving the existing `entities`
registration if present:

```json
"secret_review": true
```

That is a property to add to the existing JSON object, not the whole file.
Relaunch through Veil and send: `Use 'fictional orchard meadow phrase' to sign in.`
When Veil holds the request, run the exact `veil review` command it prints,
using `python -m veil` if `veil` is not on your PATH. Classify the phrase as a
password in the local page, then retry the original request. Record unnecessary
holds too. Review is off by default and does not identify every private value.

## 4. Exercise recovery

- Cancel a turn and send another; check that it completes and verifies.
- Exit Claude and relaunch with `claude --resume` after `python -m veil --data-dir
  ../beta-data`. For Codex, relaunch with `codex resume` after that same prefix.
  API-key users must use `codex --auth api-key resume` and preserve `--auth api-key`
  on every relaunch; otherwise the launcher selects ChatGPT sign-in. Choose the
  fictional session and verify again.
- Launch a second client from another terminal using the same dedicated data
  directory; use a different fictional contact and check session separation.
- Exit a launcher and confirm it has stopped its gateway. Relaunch and verify.
  Desktop testers can exercise `stop`, `start`, and setup/undo using the
  [desktop guide](https://github.com/Mpawlowski5467/veil/blob/v0.6.0b2/docs/openai-integration.md).
- In Codex, `/compact` may be refused: remote compaction is unsupported. Use
  `/new` (or a new local desktop chat still routed through Veil), carry over a
  brief locally reviewed handoff, and verify the new exchange. Do not switch
  away from Veil to continue the old session. Record whether the message made
  recovery clear.
- After a normal client update, repeat verification and the file exercise.
  For Veil upgrades and rollback, follow the
  [installed-wheel guide](https://github.com/Mpawlowski5467/veil/blob/v0.6.0b2/docs/upgrading.md)
  with a private backup. Do not resume a temporary `--forget-after-run` session.

Desktop testing changes actual client routing, so follow setup/undo exactly
and report any unrelated settings that change. Do not count the app-server
tests in CI as a desktop UI journey.

## 5. Clean up and report

Exit the launched clients, then from `workspace/`:

```text
python -m veil --data-dir ../beta-data forget --all
python -m veil --data-dir ../beta-data entities remove PERSON
```

Enter **Mira Quill** at the removal prompt. Remove any extra fictional
registrations you added. `forget` removes Veil mappings, not client history,
backups, or screenshots. If you installed a skill or desktop routing separately,
also use the documented `skill uninstall` and `undo codex` commands.

Fill out `feedback.md`, including steps you did not try and any maintainer help
needed. Submit manually using the
[beta feedback form](https://github.com/Mpawlowski5467/veil/issues/new?template=beta-feedback.yml).
Optionally attach `beta-check.json`. Successful journeys are useful evidence too.
For security findings, use the repository's
[security policy](https://github.com/Mpawlowski5467/veil/blob/v0.6.0b2/SECURITY.md).
