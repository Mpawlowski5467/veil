# veil

<p align="center">
  <img src="docs/assets/veil-logo.png" alt="Veil privacy ribbon logo" width="520">
</p>

**A local masking layer for your AI workflows.**

Veil replaces detected personal information with placeholders before you send text to an AI model, then restores the original values in the reply. Use it in Python scripts, run Claude Code or Codex CLI through a local gateway, or explicitly mask and restore text for chat apps.

Keep working with real names, email addresses, and account details while the model works with placeholders for the values Veil detects.

**[v0.4.1 · Alpha](CHANGELOG.md) · Python 3.10+ · No runtime dependencies · [MIT](LICENSE)**

[Get started](#choose-your-workflow) · [Python guide](docs/python-guide.md) · [Roadmap to 1.0](ROADMAP.md) · [Coverage and limits](#understand-the-boundaries) · [Contributing](#contributing)

> Detection has limits. Names need registration, and images and PDFs are not scrubbed by the gateway. Veil reduces exposure of supported text; it does not guarantee that all sensitive data stays on your machine.

## See the round trip

| Stage | Text |
| --- | --- |
| Your input | `Email Jan Nowak at jan.n@example.com.` |
| Sent to the model | `Email [PERSON_1] at [EMAIL_1].` |
| Model reply | `Hi [PERSON_1], following up on the invoice...` |
| Restored locally | `Hi Jan Nowak, following up on the invoice...` |

Names in this example are registered in advance. Email detection is built in. A value keeps the same placeholder throughout a conversation.

## Choose your workflow

| What you use | What works today |
| --- | --- |
| Python scripts and AI applications | Mask text before your provider call and restore the reply. The library is independent of any model SDK. |
| Claude Code in a terminal | `veil claude` starts a local gateway and launches Claude Code through it. |
| Codex CLI | Experimental `veil codex` launcher with ChatGPT login or `--auth api-key`. ChatGPT routing has passed a live test. |
| Codex desktop (local tasks) | Backed-up `veil setup codex`, readiness checks, and undo. A basic desktop text round trip was manually verified; broader workflows remain experimental. See the [integration guide](docs/openai-integration.md). |
| Other coding assistants | Automatic coverage needs an integration with that tool's request and response path. |
| ChatGPT app, browser chats, other chat apps | Explicit `veil mask` / `veil restore` commands, including optional clipboard mode. These do not intercept the app automatically. |

The Python library can be used with different AI providers wherever you control the text sent and received. The gateway has separate adapters for Anthropic Messages and an experimental subset of OpenAI Responses.

## Install

From a checkout of this repository, with Python 3.10 or newer:

```bash
python -m pip install .
```

Use a virtual environment if your Python installation requires one. This installs both the `veil` Python package and the `veil` command.

## Use Veil in Python

### Mask and restore

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

# Send masked.text to your model. This is an example reply:
reply = "Hi [PERSON_1], following up on the invoice..."
restored = shield.restore(reply)
if restored.warnings:
    raise RuntimeError("Review restoration warnings before using this reply.")

print(restored.text)
# Hi Jan Nowak, following up on the invoice...
```

Use one `Shield` per conversation. Keep the original text and the placeholder mappings local; send only the masked text to your provider.

### Wrap a model call

For a synchronous function that takes a string and returns a string:

```python
from veil import Shield

shield = Shield(redact_warnings=True)
shield.add_entity("Jan Nowak", "PERSON")


def call_model(prompt: str) -> str:
    # Replace this stand-in with your provider's SDK call.
    assert prompt == "Email [PERSON_1] at [EMAIL_1]."
    return "Hi [PERSON_1], following up on the invoice..."


safe_model = shield.wrap(call_model, strict=True)
print(safe_model("Email Jan Nowak at jan.n@example.com."))
```

With `strict=True`, masking warnings raise `ShieldError` before the model is called. Restoration warnings raise after the reply arrives. This catches reported problems; it cannot catch sensitive information the detectors never recognize.

For async calls, structured messages, or tool workflows, use `mask()` and `restore()` at the appropriate boundaries and handle their warnings. `wrap()` is a synchronous text-in, text-out helper.

## Use Veil with Claude Code

Have Claude Code installed and authenticated, then launch it through Veil:

```bash
veil claude
veil claude --resume
```

Veil starts a private gateway on `127.0.0.1`, routes the session's model requests through it, and stops the gateway when Claude Code exits. The gateway answers only requests that carry a secret made for the launch (and Claude Code's connection check, which it answers itself). Supported text in prompts, system instructions, file contents, and tool results is masked. Replies are restored locally, including tool arguments, so file edits can use the real values.

Content the gateway has no rule for, such as a field or block type a newer Claude Code adds, is masked like any text. What masking can't reach (a key, a type or a number that may hold personal data, file bytes, encrypted data) is refused, never sent unmasked. What Anthropic's servers add to a reply (web search results, a compacted summary) goes back as it came, unless its text holds a value you registered. Hooks check routing before each prompt and inspect tool arguments for known real values. Shell calls containing those values request approval; other outbound tools containing them are refused unless explicitly allowed. These checks do not inspect every action a shell command or external tool could perform.

Claude Code arguments are forwarded, except `--settings`, `--bare`, and `--safe-mode`, which Veil refuses because they can bypass its configuration. An existing `ANTHROPIC_BASE_URL` also prevents startup.

### Register names and other private values

Create a private configuration folder:

```bash
mkdir -p ~/.veil
chmod 700 ~/.veil
```

Save the following as `~/.veil/config.json`:

```json
{
  "entities": {
    "PERSON": ["Jan Nowak"],
    "CLIENT": ["Example Corp"]
  },
  "patterns": {
    "ORDER": "#\\d{5}"
  },
  "identity": true,
  "retention_days": 30,
  "note": true,
  "allow_mcp_tools": []
}
```

Every setting is optional:

| Setting | Purpose |
| --- | --- |
| `entities` | Exact names, organizations, and other values to mask, grouped by type. |
| `patterns` | Additional regular expressions, grouped by type. |
| `identity` | Register your Git name and email. Defaults to `true`. |
| `retention_days` | Purge sessions unused for this many days when the gateway opens its storage. Defaults to `30`. |
| `note` | Tell the model to preserve placeholders. Defaults to `true`. |
| `allow_mcp_tools` | MCP tools allowed to receive real values. Empty by default. |

Veil reads its configuration from `~/.veil`, not from a cloned project's files. Unknown settings and invalid values are rejected. The global `--data-dir` option selects a different configuration and storage folder.

### Session storage

Mappings live in `~/.veil/vault.db`. The gateway also keeps masked replies and hashes in `~/.veil/ledger.db` so conversation history can be replayed consistently.

The vault contains original values in plaintext. Veil creates private files with owner-only permissions; it does not encrypt the database. Claude Code's own local transcripts also contain restored values.

```bash
veil forget --session SESSION_ID
veil forget --all
```

These commands delete Veil's stored session mappings and ledger entries. They do not erase Claude Code's transcripts.

`veil gateway` is also available for manually configured clients. `veil claude` manages the gateway lifecycle for you.

It was tested with Claude Code 2.1.283. On another version, `veil claude` says so in one line when it starts (once for each Claude Code version and veil version) and starts it anyway. After a Claude Code update, the census (see [Development](#development)) checks what the new version sends.

### When a request is refused

A request the gateway can't mask is never sent. Claude Code shows why, for example:

```
API Error: 400 veil: can't mask this request, so nothing was sent. This is Claude Code 2.1.290, and veil 0.5.0 was tested with 2.1.283: update veil. If it happens on every prompt, it is in the conversation: /rewind to before the prompt that brought it in, or start a new one. Not handled: messages[4].content[1].attestation.signature (opaque data that can't be masked)
```

It names what it couldn't handle (each kind of problem once, with how many times it occurs, up to 50) and the Claude Code version that sent it. What to do:

- **Update veil**, especially when Claude Code is newer than the version veil was tested with.
- **If it happens on every prompt**, the content is in the conversation, which Claude Code sends again with each prompt. Use `/rewind` to go back to before the prompt that brought it in, or start a new conversation. When the message says the part is sent with every request (a tool definition or a setting), `/rewind` won't help: update veil.
- Requests Claude Code makes in the background, such as the one that names the session, can be refused without a message. When Claude Code exits, `veil claude` lists what it couldn't send.

Refusals and the gateway's own failures are final: Claude Code doesn't send them again, on another model or otherwise. A refusal inside a feature Claude Code can do without (auto mode's safety checks, a turn's effort level) is the exception: Claude Code sends the request again without it. A busy data folder or an unreachable API is retried as usual.

On Opus 5.5 and Fable 5.1, which keep the model's earlier thinking, resuming a conversation whose masking changed since (a name you registered in between, say) costs one rejected request: Claude Code then drops the earlier thinking and goes on, without the prompt cache for that turn.

## Try Veil with Codex CLI

From a checkout containing the experimental adapter, with Codex already signed in:

```bash
veil codex
veil codex --auth api-key
veil codex -- exec "Explain this project"
```

The launcher starts a private gateway, pins Codex's provider, disables hosted search, apps and subagents for that invocation, then stops the gateway when Codex exits. The default uses your existing ChatGPT login. `--auth api-key` requires `OPENAI_API_KEY` in the launching shell. Text requests and replies, local function/custom tool calls, and conversation replay are supported; unsupported request shapes are refused.

Returned tool inputs containing known private values are blocked except direct local `apply_patch` calls. This check happens before the gateway delivers executable input; it does not inspect what a command later reads or sends. Images, file attachments, hosted tools, and remote compaction are refused. The [OpenAI integration guide](docs/openai-integration.md) covers desktop setup, API clients, tested versions, and limits. Live API-key validation is still pending.

## Set up Codex desktop

From the checkout, install the optional TOML editor used by setup and diagnostics:

```bash
python -m pip install '.[desktop]'
veil setup codex
veil gateway --api openai --auth chatgpt --port 8485
```

Setup preserves unrelated settings and comments, saves a private backup, and
selects the Veil provider. It disables hosted search, apps, subagents, and
analytics in those saved settings. Keep the gateway running, restart Codex,
and start a fresh local task. In another terminal:

```bash
veil status
veil doctor
veil doctor --json
```

These commands check saved settings and the local gateway; they do not prove
that an already-open task used Veil. Doctor also checks local masking/restoration
and credential environment without making a model call. `veil undo codex`
restores the settings from before setup while preserving unrelated later edits.
See [setup and recovery](docs/openai-integration.md#codex-desktop-setup-and-recovery).
Background service management and per-task verification are next on the
[roadmap](ROADMAP.md).

## Use Veil with the ChatGPT app

Copy your prompt, mask the clipboard, then paste the masked text into ChatGPT:

```bash
veil mask --session chatgpt-notes --clipboard
```

Ask the model to keep bracketed placeholders unchanged. Copy its reply, then restore it locally:

```bash
veil restore --session chatgpt-notes --clipboard
```

Use the same session label for both operations and a different label for each conversation. Without `--clipboard`, the commands read stdin and write stdout. Warnings stop output instead of replacing the clipboard with a partial result. This is an explicit text workflow; attachments, voice, and messages sent directly in the app are not intercepted. See the [chat workflow guide](docs/chat-workflow.md).

## What Veil detects

| Type | Examples and scope |
| --- | --- |
| `EMAIL` | Addresses such as `jan.n@example.com` and `first.last+tag@example.co.uk`. |
| `PHONE` | Supported North American layouts and international numbers with a leading `+` and country code. |
| `IPV4` / `IPV6` | Supported IPv4 and IPv6 address forms. |
| `CREDIT_CARD` | Supported layouts checked against issuer prefixes, lengths, and the Luhn checksum. |
| `IBAN` | Supported country codes and layouts with checksum validation. |
| Your types | Exact registered values or custom regular expressions. |

Detection is based on patterns and explicit registration. Names, organizations, street addresses, and arbitrary secrets are not all discovered automatically. False positives and missed values are possible.

### Add your own values and patterns

```python
from veil import Shield

shield = Shield(custom_patterns={"ORDER": r"#\d{5}"})
shield.add_entity("Example Corp", "CLIENT")

masked = shield.mask("Order #12345 belongs to Example Corp.")
assert masked.text == "Order [ORDER_1] belongs to [CLIENT_1]."
```

Registered values are case-sensitive. Register each spelling you expect. In the Python library, matches usually respect word boundaries; the gateway adds a pass for known values of at least three characters even when attached to other text.

Custom types use uppercase letters, digits, and underscores, starting with a letter. A custom pattern named after a built-in type replaces that detector's pattern.

### Restore a streaming reply

```python
from veil import Shield

shield = Shield()
shield.mask("Email jan.n@example.com.")

chunks = ["Sent to [EMA", "IL_1]."]
reply = "".join(shield.restore_stream(chunks))

assert reply == "Sent to jan.n@example.com."
```

Placeholders can span chunks. For callback-based streaming and access to restoration warnings, use `stream_restorer()` with `feed()`, `finish()`, and `result()`.

### Keep mappings between runs

```python
from veil import Shield, SQLiteVault

with SQLiteVault("conversations.db", session="chat-42") as vault:
    shield = Shield(vault=vault)
    masked = shield.mask("Email jan.n@example.com.")
    assert masked.text == "Email [EMAIL_1]."

with SQLiteVault("conversations.db", session="chat-42") as vault:
    shield = Shield(vault=vault)
    assert shield.restore("Sent to [EMAIL_1].").text == "Sent to jan.n@example.com."
```

Each session has its own mappings. SQLite supports sharing them across processes. Reapply registered entities and detector configuration when creating a new `Shield`; the vault stores mappings, not configuration. Store the database on a local disk.

See the [Python guide](docs/python-guide.md) for detection rules, opt-in normalization (`normalize=True`), recovery of rewritten placeholders, literal placeholder protection, and custom detector and vault protocols.

## Understand the boundaries

- **Only detected text is masked.** Unregistered names, unsupported formats, and sensitive context can remain visible.
- **Coverage depends on the adapter.** Images and PDFs pass through the Anthropic adapter; the OpenAI adapter refuses them. Tool definitions are not scrubbed, except the schema you give `claude -p --json-schema` (its descriptions and example values are masked; a property name or `pattern` holding personal data is refused). Hosted search results come back from Anthropic and go back to it as they came, and OpenAI hosted tools are refused.
- **Masking is reversible.** The vault holds original values; placeholders still reveal types, counts, and repeated references. Surrounding context may reveal identity.
- **Tool checks cover arguments containing known values.** Claude uses hooks; the OpenAI gateway checks restored tool inputs before delivery. A command can read and send a file without including its contents in the arguments. Veil is not a sandbox or a network firewall.
- **Your provider still authenticates you.** Masking prompt content does not hide your account or prevent local transcripts.
- **Restoration depends on placeholders surviving.** Unknown placeholders remain unchanged and produce warnings. Use exact restoration (`tolerant=False`) for library output that will be executed or written as tool arguments.
- **Warnings need handling.** Direct `mask()` and `restore()` calls return warnings; `wrap()` emits them by default. Use `strict=True` to raise and `redact_warnings=True` to avoid quoting leaked values in warnings.

## Development

```bash
uv sync
uv run pytest -m "not live"
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
uv run pyright src
```

The test suite includes Python examples from this README and the Python guide, streaming and overlap tests, gateway tests, and randomized input tests.

Live integration tests require an authenticated Claude Code CLI and make real model calls:

```bash
VEIL_LIVE_CLAUDE=1 uv run pytest -m live
```

After a Claude Code update, run the census. It runs Claude Code through the gateway on four models, one scenario at a time (about 80-90 real model calls; `--models haiku` is a quick check), and drives an interactive session under GNU screen (`/usr/bin/screen`). It reports what Claude Code sends that `tests/gateway_payloads/` doesn't list yet, and every failure: a request refused, a real value in what left, a reply key the gateway dropped. It exits 0 when all is known and clean, 1 when something is new, 2 on a failure.

```bash
uv run python -m tests.live.census            # report only
uv run python -m tests.live.census --update   # also refresh the census after a clean run
```

With `--update`, a run without failures refreshes the census and the protocol words (`src/veil/gateway/vocab.py`); new paths go in only with `--accept-new`, after you've checked the gateway handles them. Once `-p`, `/compact` and an interactive session ran clean on all four models, with nothing new (or with `--accept-new`), it also moves the tested Claude Code version forward. A failed run is kept: `--from DIR` analyzes it again, and `--resume DIR` reruns what the harness couldn't finish.

## Contributing

Useful contributions include reproducible missed matches, false-positive reports, integration adapters, and tests for new client request formats. Use fictional data when sharing examples.

## License

MIT. See [LICENSE](LICENSE).
See [CHANGELOG.md](CHANGELOG.md) for release history.
