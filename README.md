# Veil

<p align="center">
  <img src="docs/assets/veil-logo.png" alt="Veil privacy ribbon logo" width="420">
</p>

**Mask private values before an AI call. Restore them in the reply.**

Veil runs on your computer. It replaces detected values with placeholders such as
`[EMAIL_1]`, then restores the originals in the model's response. Use it with
Claude Code, Codex, or your own Python integration.

**[0.6.0b2 · Beta](https://github.com/Mpawlowski5467/veil/releases/tag/v0.6.0b2) · Python 3.10+ · Dependency-free base library · [MIT](LICENSE)**

[Try the beta](beta/README.md) · [Python quickstart](#use-veil-in-python) · [What gets masked](#what-veil-detects) · [Limits](#understand-the-boundaries) · [Guides](#guides)

> **New here? Start with the [beta tester guide](beta/README.md).** The first
> check takes about 10 minutes and needs no AI account or API key. Use the
> fictional examples first: Veil can miss private values, and names need registration.

The published release is **0.6.0b2**. This repository's `main` branch also contains
[unreleased changes](CHANGELOG.md#unreleased); those changes are not in the
published wheel or release ZIP.

## Try Veil

The [beta tester guide](beta/README.md) covers macOS, Linux, and Windows:

1. Install the published beta and run five local checks with fictional data.
2. Optionally launch Claude Code or Codex CLI and verify one real exchange.
3. Send [feedback](https://github.com/Mpawlowski5467/veil/issues/new?template=beta-feedback.yml), including where you got stuck or what worked.

If you cloned or downloaded this repository, use its **`beta/` folder** as the
test pack. Client exercises use your normal account and may consume usage or
credits. For a shorter demonstration, see the [five-minute quickstart](docs/first-five-minutes.md).

After installing Veil, `veil preview` opens a [local preview](docs/local-preview.md)
for masking, review, and restoration without a model account. It does not
configure or verify your chat's routing.

### How the round trip works

| Stage | Text |
| --- | --- |
| Your input | `Email Jan Nowak at jan.n@example.com.` |
| Sent to the model | `Email [PERSON_1] at [EMAIL_1].` |
| Example model reply | `Hi [PERSON_1], following up on the invoice...` |
| Restored locally | `Hi Jan Nowak, following up on the invoice...` |

The name is registered beforehand; email detection is built in. Seeing the
original value in the reply is expected after restoration. Use
[verification](#verify-your-conversation) to check whether an exchange went through Veil.

<details>
<summary>Watch the local demo</summary>

![Fictional contact details become placeholders and are restored locally. The model reply is simulated.](docs/assets/veil-demo.gif)

[36-second video](docs/assets/veil-demo.mp4) · [Runnable example](examples/first_round_trip.py)

This recording uses actual Veil 0.5.0 masking and restoration with a **simulated
model reply**. It demonstrates the local transformation, not a live client exchange.

</details>

## Choose your workflow

Commands below assume Veil is installed and available in your terminal. The
[beta guide](beta/README.md) includes explicit Python paths for each platform.

| You use | Start here | Scope |
| --- | --- | --- |
| Claude Code | `veil claude` · [Guide](docs/claude-code.md) | Gateway for the newly launched terminal session. |
| Codex CLI | `veil codex` · [Guide](docs/openai-integration.md) | Experimental gateway; ChatGPT sign-in by default, with optional API-key mode. |
| Codex desktop | [Setup and recovery](docs/openai-integration.md#codex-desktop-setup-and-recovery) | Experimental; restart Codex and open a fresh local task after setup. |
| Python or another AI provider | [Quickstart below](#use-veil-in-python) | You control which text is masked before sending and restored afterward. |
| Ordinary ChatGPT or browser chats | [File and clipboard workflow](docs/chat-workflow.md) | Explicitly mask copied text; no automatic interception. |

## Start with the Veil skill

The optional assistant skill helps you set up Veil, check status, verify a
request, and mask or restore local files. After [installing the package](docs/first-five-minutes.md#1-install-the-released-version),
run this in its Python environment:

```bash
veil skill install
```

Then ask your assistant for help:

| Client | Send in chat |
| --- | --- |
| Codex desktop | Select **Veil** in the skills picker, then send `Help me set up Veil for Codex desktop.` |
| Codex CLI | `$veil help me start a Codex CLI session through Veil` |
| Claude Code | `/veil help me start Claude Code through Veil` |

**Installing the skill does not enable automatic masking.** The client must
send requests through the gateway; the skill cannot reroute an existing chat.
For file tasks, give it local **file paths**, not private values pasted into chat.
Keep the Python environment used to install the skill available.
[Skill installation, examples, and removal](docs/assistant-skills.md).

## Verify your conversation

<a id="4-verify-your-conversation"></a>

In the new session you want to test, select the Veil skill and ask
`Verify this conversation through Veil.` Codex CLI also accepts `$veil verify`;
Claude Code uses `/veil verify`.

1. Send the exact fictional prompt it creates as a **new message in that same conversation**.
2. Wait for the reply to finish.
3. Ask it to check the printed verification ID: `Check verification ID <ID>.`

**Success means `verified`.** It confirms that this one exchange was masked,
forwarded, and restored. An echoed email alone, `pending`, or `incomplete` is
not a pass. Verification does not prove that every future message or private
value is covered. [Terminal commands and result states](docs/verification.md).

## Register names and other private values

Names, organizations, and street addresses need explicit registration. In your
own terminal:

```bash
veil entities add PERSON
veil entities list
```

`add` prompts for a value with typing hidden; `list` shows counts only. Values
are case-sensitive, so register each spelling you expect. Use the **same data
folder** for registration and your gateway, then restart the gateway or relaunch
the client through Veil. The default folder is `~/.veil`; a custom `--data-dir`
goes before the subcommand. [Registration and removal](docs/entities.md).

## Use Veil in Python

This example runs locally without an API key:

```python
from veil import Shield

shield = Shield(redact_warnings=True)
shield.add_entity("Jan Nowak", "PERSON")

masked = shield.mask("Email Jan Nowak at jan.n@example.com.")
if masked.warnings:
    raise RuntimeError("Review masking warnings before sending this text.")

assert masked.text == "Email [PERSON_1] at [EMAIL_1]."
print(masked.text)

# Send masked.text to your model. This is a simulated reply:
reply = "Hi [PERSON_1], following up on the invoice..."
restored = shield.restore(reply)
if restored.warnings:
    raise RuntimeError("Review restoration warnings before using this reply.")

print(restored.text)
# Hi Jan Nowak, following up on the invoice...
```

Use one `Shield` per conversation. Send only the masked text to your provider
and keep the originals and mappings local. The [Python guide](docs/python-guide.md)
covers model wrappers, custom patterns, streaming, storage, and warning handling.

## What Veil detects

| Type | Supported examples |
| --- | --- |
| Email | Addresses such as `jan.n@example.com` and `first.last+tag@example.co.uk`. |
| Phone | Supported North American layouts and international numbers with a leading `+` and country code. |
| IP addresses | Supported IPv4 and IPv6 forms. |
| Credit cards and IBANs | Supported layouts with checksum validation; cards also use issuer and length checks. |
| US Social Security numbers | Hyphenated forms; compact or space-separated forms need an explicit SSN label. Invalid ranges are rejected. |
| Coding secrets | Supported API keys, tokens, labelled passwords, private-key blocks, and URL credentials. [Formats and limits](docs/coding-secrets.md). |
| Your values | Exact registrations and custom regular expressions. |

Detection uses patterns and registrations. False positives and missed values
are possible; arbitrary names, addresses, and secrets are not automatically covered.

[Local secret review](docs/secret-review.md) is **off by default**. When enabled,
it holds requests with uncertain findings for your decision on a private local
page. Confirmed private values are masked; unresolved findings keep the request
local. Review can still miss values. See the [workflow evaluation](docs/leak-evaluation.md)
and its separate challenge corpus for measured misses and false positives.

## Understand the boundaries

- **Coverage depends on the adapter.** Claude image/PDF contents pass through unmasked; OpenAI media is refused. Tool definitions are generally not scrubbed, and hosted content has separate limits. See the [Claude](docs/claude-code.md) and [Codex](docs/openai-integration.md) guides.
- **Direct tool traffic is outside Veil.** A tool can read a file and send it over the network without its contents passing through the gateway. Veil is not a sandbox or firewall.
- **Mappings contain the originals.** Persistent vaults use plaintext local storage with private permissions, without application-level encryption. Use a private local folder and OS disk encryption. Forgetting mappings does not erase client transcripts or backups.
- **Context can still identify you.** Placeholders reveal types and repeated references; surrounding text may reveal identity. Your provider still knows the account authenticating the request.
- **Restoration and warnings need care.** Unknown placeholders remain unchanged and produce warnings. Handle library warnings, and use exact restoration (`tolerant=False`) for output used as executable code or tool arguments. `wrap(strict=True)` raises on reported warnings, not undetected values.

Read the [threat model](docs/threat-model.md) for the full scope and
[report security issues privately](SECURITY.md).

## Troubleshooting and feedback

| Problem | Next step |
| --- | --- |
| `veil` is not found | Use the explicit environment path in the [beta guide](beta/README.md), or `uv run veil` from a prepared source checkout. |
| The skill is missing | Run `veil skill install` in the correct environment, then start a new client session. |
| The gateway is unreachable or a request is refused | Check the [Claude](docs/claude-code.md) or [Codex](docs/openai-integration.md) guide and [gateway recovery](docs/background-gateway.md). |
| A name is not masked | Register its exact spelling in the gateway's data folder and restart/relaunch. A restored name in the reply is expected. |
| You want ordinary Codex desktop routing back | Run `veil undo codex`, restart Codex, and open a fresh local task. Stop the unused managed gateway with `veil stop`. |

To collect a local diagnostic report:

```bash
veil report > veil-report.json
```

It contains allowlisted versions, status codes, and aggregate counts, excluding
prompts and original values. Nothing is uploaded. Review it before sharing;
[report options](docs/support-report.md) explain how to select another gateway.
Use fictional examples in [beta feedback](https://github.com/Mpawlowski5467/veil/issues/new?template=beta-feedback.yml).

## Guides

| Topic | Guide |
| --- | --- |
| Getting started | [Beta test](beta/README.md) · [Five-minute demo](docs/first-five-minutes.md) · [Local preview](docs/local-preview.md) |
| Integrations | [Claude Code](docs/claude-code.md) · [Codex](docs/openai-integration.md) · [Assistant skill](docs/assistant-skills.md) |
| Everyday use | [Files and clipboard](docs/chat-workflow.md) · [Verification](docs/verification.md) · [Upgrade and recovery](docs/upgrading.md) |
| Configuration | [Private values](docs/entities.md) · [Settings and storage](docs/configuration.md) · [Secret review](docs/secret-review.md) |
| Evidence and limits | [Compatibility](docs/compatibility.md) · [Detection baseline](docs/detection-results.md) · [Workflow evaluation](docs/leak-evaluation.md) · [Threat model](docs/threat-model.md) |

## Beta status

Veil is ready for external beta testing. The road to 1.0 still includes new-user
journeys across macOS, Linux, and Windows, live API-key validation, long-session
and desktop UI checks, and independent security review. See the
[release checklist](docs/release-checklist.md) and [roadmap](ROADMAP.md#10-release-gate).

Veil was tested with Claude Code 2.1.283. This is the full request-census baseline;
[compatibility notes](docs/compatibility.md) record additional client checks.
Verify your route again after a client update.

## Development and contributing

To work from this source checkout:

```bash
uv sync --locked
uv run pytest -m "not live"
```

The [development guide](docs/development.md) covers linting, type checks, optional
live tests, and the client request census. The routine test command above makes
no model calls. Add `--extra desktop` to `uv sync` for Codex configuration editing.

Contributions are welcome: reproducible misses, false positives, integration
fixes, and new client fixtures. Use fictional data in examples and reports.
See the [invitation kit](docs/beta-launch.md) to invite testers,
[CHANGELOG](CHANGELOG.md) for release history, and [LICENSE](LICENSE) for the MIT license.
