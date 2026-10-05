# Optional beta exercises

Start with [the tester guide](README.md). These exercises are optional: try the
ones that match your workflow and mark the rest **Not tried** in
[feedback.md](feedback.md). Use invented values throughout.

All commands below run from the supplied `workspace/` folder, using the same
`beta-data` folder as the main guide. Exit the client before changing settings,
then launch it through Veil again. Do not use your everyday Veil data folder.

## Review an uncertain secret

Review pauses an uncertain request so you can decide locally whether a value is
private. Review is off by default and does not catch every secret.

1. Exit the client normally.
2. Open `beta-data/config.json` in a text editor. This folder is next to
   `workspace/`, one level above your terminal's current folder.
3. For the fresh exercise with only Mira registered, the **complete JSON file**
   is shown below. If you added other registrations or settings, keep them and
   add `"secret_review": true` to the existing object instead. Do not save only
   a single property without the surrounding braces.

```json
{
  "entities": {
    "PERSON": ["Mira Quill"]
  },
  "identity": false,
  "secret_review": true
}
```

`identity: false` keeps this fictional test from also importing your Git name.
Save the file, then relaunch using step 3 of the main guide. Send:

> Use 'fictional orchard meadow phrase' to sign in.

**Expected:** Veil holds the request for review before sending it to the model.
In the second terminal, open the local review page:

**macOS or Linux**

```bash
../.venv/bin/python -m veil --data-dir ../beta-data review
```

**Windows PowerShell**

```powershell
..\.venv\Scripts\python.exe -m veil --data-dir ../beta-data review
```

If the refusal specifies `--gateway-url`, include that same option and address.
On the local page, classify the fictional phrase as **Password or passphrase**,
save the decision, then retry the original message in the client. Do the review
yourself; do not ask the AI client to approve it.

When you have finished reviewing, press **Ctrl-C in the review terminal** to
close the review server and get that terminal's command prompt back. Leave the
client terminal running if you want to continue testing.

Record whether the hold, review page, and retry were understandable. Unnecessary
holds are useful feedback too. Do not share the private review URL or a screenshot
showing its revealed values. Decisions expire, so a delayed retry may need review
again. After this exercise, you can keep review on or change it to `false` and
relaunch the client.

## Understand a detection limit

Open `example.env` locally. Its values are invented and do not work as credentials.

| Example | Expected in 0.6.0b2 |
| --- | --- |
| `DATABASE_PASSWORD="orchard-demo-724"` | The quoted password is automatically masked. |
| `ADMIN_PASSWORD=admin` | The bare, code-looking value can leave unchanged, even with review on. |
| `CONTACT_EMAIL=mira.quill@example.org` | The email is automatically masked. |

Quoting the weak password (`ADMIN_PASSWORD="admin"`) or explicitly registering
its exact value as `PASSWORD` protects that value. Veil avoids treating ordinary
code references as credentials; this creates a tradeoff for ambiguous defaults.
Report whether the distinction was clear. Do not use a real password to test it.

Names and their spellings also need care: registering **Mira Quill** does not
register **MIRA QUILL** or a nickname. Add each spelling you want protected.
Supported examples are not a guarantee for every format or language.

## Resume a conversation

Exit the client, then use the matching command below. Choose only the fictional
session from this exercise. Ask it to recall the contact email, then run a fresh
verification as described in the main guide.

**macOS or Linux — choose one**

```bash
../.venv/bin/python -m veil --data-dir ../beta-data claude --resume
../.venv/bin/python -m veil --data-dir ../beta-data codex resume
```

**Windows PowerShell — choose one**

```powershell
..\.venv\Scripts\python.exe -m veil --data-dir ../beta-data claude --resume
..\.venv\Scripts\python.exe -m veil --data-dir ../beta-data codex resume
```

API-key users must use the separate commands below to keep the same auth route.
Do not resume a session started with temporary `--forget-after-run` mappings.
Record refusals or missing recalled values; repeated retries are not required.

## Try recovery during normal work

Pick any of these and note the result:

| Exercise | What to check |
| --- | --- |
| Cancel one turn, then send another | Can you continue and verify a new exchange? |
| Exit the launcher, then relaunch through Veil | Does the old gateway stop? Can the new session verify? CLI launchers manage their own gateway; background `start`/`stop` commands are a different workflow. |
| Open a second Veil-launched client in another terminal | Give one chat `first.contact@example.org` and the other `second.contact@example.org`. Ask each to recall its own contact. Record unexpected mixing. If verification sees multiple gateways, select the intended address. |
| Work for longer or try Codex `/compact` | Does work continue, or is the refusal/recovery message clear? |
| Update your AI client normally | Repeat verification and the contact-file edit; record the before/after client versions. |

Some Codex compaction requests are refused because remote compaction is not
supported. If refused, use `/new` within the Veil-launched client, carry over a
short fictional handoff you have checked locally, and verify again. Do not switch
away from Veil to continue the old session. Record a model refusal separately
from a gateway error when you can tell them apart.

## Codex with an OpenAI API key

Try this only if you already have a key available privately as `OPENAI_API_KEY`
in your terminal environment. API calls can incur charges. Do not put the key
in the commands below, the prompt, or your feedback.

**macOS or Linux**

```bash
../.venv/bin/python -m veil --data-dir ../beta-data codex --auth api-key
```

To resume:

```bash
../.venv/bin/python -m veil --data-dir ../beta-data codex --auth api-key resume
```

**Windows PowerShell**

```powershell
..\.venv\Scripts\python.exe -m veil --data-dir ../beta-data codex --auth api-key
```

To resume:

```powershell
..\.venv\Scripts\python.exe -m veil --data-dir ../beta-data codex --auth api-key resume
```

Keep `--auth api-key` on every relaunch. Without it, the launcher defaults to
ChatGPT sign-in. Use the main guide's verification and file-edit steps, and
identify the auth method in your feedback without including account details.

## Desktop setup, upgrades, or rollback

These change more than the basic CLI exercise. Try them only if they are part
of what you want to test; **Not tried** is fine.

- **Codex desktop:** follow the versioned
  [desktop setup guide](https://github.com/Mpawlowski5467/veil/blob/v0.6.0b2/docs/openai-integration.md).
  This pack already installed the wheel and desktop extra: skip its source-checkout
  `pip install '.[desktop]'` step. From `workspace/`, replace bare `veil` in its
  commands with `../.venv/bin/python -m veil --data-dir ../beta-data` on
  macOS/Linux, or `..\.venv\Scripts\python.exe -m veil --data-dir ../beta-data`
  on Windows, keeping the subcommand and remaining options. Keep this same
  dedicated data folder throughout; if a command already has `--data-dir`,
  replace its path rather than adding a second option. Setup changes saved routing;
  restart Codex and open a fresh local chat before verifying. Follow **undo** when
  finished, stop the test gateway, and check that unrelated settings survive.
  Undoing routing does not stop a gateway. Native Windows uses a
  foreground gateway; background service management is not supported there.
- **Veil upgrade/rollback:** follow the
  [installed-wheel guide](https://github.com/Mpawlowski5467/veil/blob/v0.6.0b2/docs/upgrading.md)
  using fictional data and a private backup. Record exact versions and whether
  values still restore. Keep backups and configurations out of feedback.

Finish with the cleanup steps in [the main guide](README.md#6-finish-and-send-feedback).
Send only your reviewed feedback and optional `beta-check.json`, never the used
exercise folder. Submit security findings through the
[private security form](https://github.com/Mpawlowski5467/veil/security/advisories/new).
