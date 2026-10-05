# Try Veil: beta tester guide

**For Veil 0.6.0b2 · Guide updated October 4, 2026**

Help us find confusing steps, broken workflows, and gaps in masking. You do not
need to finish every exercise. A report that says where you got stuck is useful.

| Choose your test | Time | What you need |
| --- | --- | --- |
| **Start here: local check** | About 10 minutes | Python 3.10 or newer |
| **Then try an AI client** | Another 20–30 minutes | Claude Code or Codex CLI, installed and signed in |
| **Optional deeper tests** | At your own pace | [Extra exercises](extra-tests.md) |

Use only the fictional files in this pack. The local check makes no AI calls.
Client exercises use your normal account and may consume usage or credits.
Veil is a beta: it masks supported text, not every private value. Names need
registration. Claude image/PDF contents pass through unmasked; OpenAI media is
refused. Direct tool/network traffic is outside the gateway's masking. Normal
user accounts are welcome; administrator access is not
required for this test.

## 1. Install and run the local check

If you cloned or downloaded this repository, open a terminal in its **`beta/`
folder** and skip the ZIP download below.

Otherwise, download the beta ZIP from the
[0.6.0b2 release page](https://github.com/Mpawlowski5467/veil/releases/tag/v0.6.0b2),
or use the revised ZIP sent with this guide. Extract it, then open a terminal
**inside the folder containing `check.py`, `feedback.md`, and `workspace/`**.
Do not run commands inside the ZIP viewer.

Run the commands for your system. They create a separate Python environment
inside this folder and install the published 0.6.0b2 wheel.

**macOS or Linux**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'veil[desktop] @ https://github.com/Mpawlowski5467/veil/releases/download/v0.6.0b2/veil-0.6.0b2-py3-none-any.whl'
.venv/bin/python check.py
```

**Windows PowerShell**

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install 'veil[desktop] @ https://github.com/Mpawlowski5467/veil/releases/download/v0.6.0b2/veil-0.6.0b2-py3-none-any.whl'
.\.venv\Scripts\python.exe check.py
```

No environment activation or PowerShell execution-policy change is needed.
If Python is missing or older than 3.10, install a supported version first.

**Success:** all five checks say `PASS` (or `pass` in the original release pack).
They test registration, masking, restoration, forgetting, and registration
removal. The script saves `beta-check.json`; it sends nothing automatically,
changes no client settings, and does not touch your clipboard.

**If any check fails, stop here and report it.** Do not troubleshoot by using
real data or changing permissions. To repeat the check, choose a new report name:
append `--output beta-check-2.json` to the last command.

You can finish here: fill in [feedback.md](feedback.md). The rest is optional.

## 2. Register the fictional name

In the same terminal, enter the supplied workspace and register its contact.
Keep all remaining terminal commands in `workspace/` unless a step says otherwise.

**macOS or Linux**

```bash
cd workspace
../.venv/bin/python -m veil --data-dir ../beta-data entities add PERSON
```

**Windows PowerShell**

```powershell
cd workspace
..\.venv\Scripts\python.exe -m veil --data-dir ../beta-data entities add PERSON
```

At `Private value (hidden):`, type **Mira Quill** and press Enter. It is normal
for nothing to appear while you type. Expect `Registered one value`.

The `beta-data` folder keeps this exercise separate from your usual Veil data.
Keep it private; never attach it to feedback.

## 3. Launch one client through Veil

Run **one** command below, choosing your client and system. Leave this terminal
open while testing. These commands launch a new CLI session; they do not change
an already-open desktop chat's routing.

| Client | macOS / Linux command |
| --- | --- |
| Claude Code | `../.venv/bin/python -m veil --data-dir ../beta-data claude` |
| Codex CLI | `../.venv/bin/python -m veil --data-dir ../beta-data codex` |

| Client | Windows PowerShell command |
| --- | --- |
| Claude Code | `..\.venv\Scripts\python.exe -m veil --data-dir ../beta-data claude` |
| Codex CLI | `..\.venv\Scripts\python.exe -m veil --data-dir ../beta-data codex` |

Codex uses ChatGPT sign-in by default. API-key and desktop setup are separate
[optional tests](extra-tests.md); do not paste an API key into a chat or report.

## 4. Verify one exchange

Open a **second terminal** and navigate to the same `workspace/` folder. Keep
the client running in the first terminal.

**macOS or Linux**

```bash
../.venv/bin/python -m veil --data-dir ../beta-data verify
```

**Windows PowerShell**

```powershell
..\.venv\Scripts\python.exe -m veil --data-dir ../beta-data verify
```

1. Copy the exact fictional prompt printed by Veil into the client in the first
   terminal. Send it as a new message and wait for the reply to finish.
2. Back in the second terminal, run the command below. Replace
   `VERIFICATION_ID` with the ID printed by `verify`.

**macOS or Linux**

```bash
../.venv/bin/python -m veil --data-dir ../beta-data verify --check VERIFICATION_ID
```

**Windows PowerShell**

```powershell
..\.venv\Scripts\python.exe -m veil --data-dir ../beta-data verify --check VERIFICATION_ID
```

**Success means `verified`.** An echoed email alone is not enough. Verification
confirms that this one exchange was masked before forwarding and restored on
return; it does not prove that every future request or private value is covered.
If Veil asks you to select a gateway, add its displayed `--gateway-url` to both
verification commands. See [help with common problems](#if-you-get-stuck).

## 5. Try a small file edit

Send this prompt in the same client:

> Read contact.txt. Change only “Project: Orchard demo” to “Project: Orchard
> follow-up”. Save the file, then tell me the contact's name and email address.

Approve only the file access needed for this exercise. Open `contact.txt`
locally afterward. Check that the project line changed and the name/email stayed
intact. The reply should contain **Mira Quill** and **mira.quill@example.org**.
Record a refusal, an incorrect edit, or a missing value as a failure; you do not
need to make it pass before reporting.

Then ask:

> Explain example.py without changing it.

Check that references such as `settings.database_password` and `settings.api_key`
remain useful code. If you have time, try [review, resume, and recovery](extra-tests.md).

## 6. Finish and send feedback

Exit **all** clients you launched for this test. If you tried desktop setup,
stop its separately started gateway and undo the test routing first, following
that guide. Then, from `workspace/`, remove this exercise's mappings and registration:

**macOS or Linux**

```bash
../.venv/bin/python -m veil --data-dir ../beta-data forget --all
../.venv/bin/python -m veil --data-dir ../beta-data entities remove PERSON
```

**Windows PowerShell**

```powershell
..\.venv\Scripts\python.exe -m veil --data-dir ../beta-data forget --all
..\.venv\Scripts\python.exe -m veil --data-dir ../beta-data entities remove PERSON
```

Enter **Mira Quill** at the removal prompt. Remove any extra fictional
registrations you added. Forgetting removes Veil mappings; it does not delete
client chat history, backups, or screenshots. If you tried desktop setup or skill
installation separately, follow that guide's undo/uninstall steps too.

Fill in [feedback.md](feedback.md) and send it back to the person who invited you,
or use the [public beta feedback form](https://github.com/Mpawlowski5467/veil/issues/new?template=beta-feedback.yml).
You may attach `beta-check.json` after reading it. Successful tests matter too.

**Share only your reviewed feedback and optional `beta-check.json`. Do not send
`beta-data/`, `.venv/`, configuration files, transcripts, or a ZIP of your used
exercise folder.** Report a security issue through the
[private security form](https://github.com/Mpawlowski5467/veil/security/advisories/new),
not a public issue.

## If you get stuck

| What you see | What to do |
| --- | --- |
| `check.py` or the Python path cannot be found | For step 1, use the folder containing `check.py`. For steps 2–6, use its `workspace/` subfolder. |
| Python or `py` is not found | Install Python 3.10 or newer, reopen the terminal, and retry step 1. |
| The report already exists | Add `--output beta-check-2.json`; the script deliberately keeps the first report. |
| Claude or Codex is not found, or sign-in is needed | Install/sign in to your chosen client first, or submit the local-only result. |
| `pending` verification | Confirm you sent the generated prompt in the Veil-launched client and waited for its reply. |
| `incomplete`, `expired`, an error, or a model refusal | Record the state and the step. An expired probe needs a new `verify` prompt. Repeated failures are useful feedback. |
| Multiple gateways | Use the address Veil displays for this client with `--gateway-url` on both verification commands. |

This guide targets the published **0.6.0b2** wheel. Fixes described as unreleased
in the repository are not included in that wheel. Editing this guide does not
change the published release assets.
