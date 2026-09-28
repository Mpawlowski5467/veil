# veil

**veil replaces personal information with placeholders before your text reaches an AI model, then puts the real values back into the model's reply.**

The model never sees the real data. veil is pure Python with no runtime dependencies.

> Status: v0.4, alpha. "veil" is a working name. See [CHANGELOG.md](CHANGELOG.md).

## Install

Requires Python 3.10+. veil is not on PyPI yet, so install it from a checkout:

```bash
pip install .
```

## 30-second quickstart

```python
from veil import Shield

shield = Shield()
shield.add_entity("Jan Nowak", "PERSON")  # names must be registered in v0.1


def my_llm(prompt: str) -> str:
    # Stand-in for your real model call. Any function str -> str works.
    return "Hi [PERSON_1], following up on the invoice..."


safe_llm = shield.wrap(my_llm)
print(safe_llm("Email Jan Nowak at jan.n@example.com about the invoice."))
# Hi Jan Nowak, following up on the invoice...
```

`wrap()` masks the prompt, calls your function with the masked text, and restores the reply.

## How it works

| Step | Text |
| --- | --- |
| Input | `Email Jan Nowak at jan.n@example.com about the invoice.` |
| Masked (sent to the model) | `Email [PERSON_1] at [EMAIL_1] about the invoice.` |
| Model reply | `Hi [PERSON_1], following up on the invoice...` |
| Restored | `Hi Jan Nowak, following up on the invoice...` |

The same steps without `wrap()`:

```python
>>> from veil import Shield
>>> shield = Shield()
>>> shield.add_entity("Jan Nowak", "PERSON")
>>> masked = shield.mask("Email Jan Nowak at jan.n@example.com about the invoice.")
>>> masked.text
'Email [PERSON_1] at [EMAIL_1] about the invoice.'
>>> [(e.placeholder, e.value) for e in masked.entities]
[('[PERSON_1]', 'Jan Nowak'), ('[EMAIL_1]', 'jan.n@example.com')]
>>> result = shield.restore("Hi [PERSON_1], following up on the invoice...")
>>> result.text
'Hi Jan Nowak, following up on the invoice...'
>>> result.restored_count
1
```

Placeholders look like `[TYPE_N]`. Numbering is per type and starts at 1.

## Using it with Claude Code

`veil claude` runs [Claude Code](https://claude.com/claude-code) through a masking gateway on your machine. Everything Claude Code sends to the model is masked on the way out, and every reply is restored on the way back. Claude Code and your files work with the real values; the model only ever sees placeholders.

```bash
pip install .            # from a checkout; this installs the veil command
veil claude              # instead of claude; any claude arguments work
```

What happens:

- **One private gateway per session.** `veil claude` starts a gateway on a free local port for this one Claude Code process, and stops it when Claude Code exits. It listens on your machine only, and answers only requests that carry a secret made for this launch. Claude Code gets its address through settings that outrank a project's settings and your own, kept in a file only you can read. Those settings also keep hooks on, other providers and Remote Control off. `veil claude` won't start with `--settings`, `--bare`, or `--safe-mode`, or when `ANTHROPIC_BASE_URL` is already set, since each would send requests past the gateway.
- **Everything the model reads is masked:** your prompts and pasted text, files you attach with `@`, every tool result (a failed command's output too), `CLAUDE.md` and memory, the git status, and the account email Claude Code adds to each request. A known value is masked even glued to a word (`Jan Nowakem` goes out as `[PERSON_1]em`). Content the gateway has no rule of its own for, such as a field or block type a newer Claude Code sends, is masked like any text; what can't be masked that way (a key, a type or a number that holds personal data, file bytes, encrypted data) is refused, never sent as it is (see [When a request is refused](#when-a-request-is-refused)). The model's thinking can't be changed (it is signed), so thinking about data it saw before it was masked, such as a name you registered later, is dropped.
- **Replies are restored as they stream.** Tool calls get the real values back, so an Edit matches the text in your file and a Write puts real values on disk. When Claude Code sends a reply back as history, the model's own words go back exactly as it wrote them.
- **Real values don't leave through tools.** A hook checks every tool call but plain file reads and edits. A shell command that contains a real value asks you first (in `claude -p`, where nobody can answer, it is refused). A call to any other tool that could send one off the machine, such as a web request, an MCP call, or a remote agent, is refused. Tools that send content through Anthropic's services (push notifications, routines, artifacts, file sharing, messages to other sessions) are turned off. If the hooks can't run, `veil claude` doesn't start Claude Code, and before each prompt a hook checks that the gateway is still there.
- **Placeholders last per session.** They are kept in `~/.veil/vault.db`, one set per Claude Code session, so `--resume` works. Sessions unused for 30 days are deleted.

Settings live in `~/.veil/config.json`, and only there, so a repository you clone can't change them:

```json
{
  "entities": {"PERSON": ["Jan Nowak"], "CLIENT": ["Example Corp"]},
  "patterns": {"ORDER": "#\\d{5}"},
  "identity": true,
  "retention_days": 30,
  "note": true,
  "allow_mcp_tools": ["mcp__crm__lookup"]
}
```

`entities` are names and other values no pattern finds, one line each (with `identity`, your git name and email are added). `patterns` are extra types, as for `custom_patterns`. `note` adds a line to the system prompt telling the model about placeholders. `allow_mcp_tools` lists MCP tools that may receive real values. Every key is optional. An unknown key or a wrong value stops `veil claude` with a message that names the key.

What it doesn't cover:

- **Personal data veil doesn't detect** (see [Limitations](#limitations)), such as names you haven't registered, street addresses, and local numbers without an area code like `555-0100`.
- **Images and PDFs** are sent as they are.
- **Tool definitions**, including the descriptions MCP servers give their tools, and the results of Anthropic's server-side web search. The one exception is the schema you give `claude -p --json-schema`: its descriptions and example values are masked (the model writes placeholders, and the reply is restored before Claude Code checks it against your schema); a property name or `pattern` holding personal data can't be masked, so the request is refused.
- **Who you are.** Your login tells Anthropic which account is calling.
- **Copies on your own machine.** Claude Code's transcripts under `~/.claude/projects` hold the real values, and so does `~/.veil/vault.db` (plain text, readable by you only).
- **The Claude desktop app**, which doesn't read `ANTHROPIC_BASE_URL`. `veil claude` is for Claude Code in a terminal.

It was tested with Claude Code 2.1.283. When yours is another version, `veil claude` says so in one line when it starts (once for each version) and starts it anyway: a newer Claude Code may send something veil doesn't know yet, which is refused, never sent unmasked. After an update, run the live tests (see [Development](#development)) to check that nothing it relies on changed.

Two more commands: `veil forget --session ID` (or `--all`) deletes stored mappings, and `veil gateway` runs a long-lived gateway on a fixed port and prints the settings to point Claude Code or another client at it. Prefer `veil claude`: while a long-lived gateway isn't running, another program could take its port.

### When a request is refused

A request the gateway can't mask is never sent. Claude Code shows why, for example:

```
API Error: 400 veil: can't mask this request, so nothing was sent. This is Claude Code 2.1.290, and veil 0.5.0 was tested with 2.1.283: update veil. If it happens on every prompt, it is in the conversation: /rewind to before the prompt that brought it in, or start a new one. Not handled: messages[4].content[1].type (unknown block type 'future_block')
```

It names every part it couldn't handle and the Claude Code version that sent it. What to do:

- **Update veil**, especially when Claude Code is newer than the version veil was tested with. A new Claude Code release can send something veil doesn't know yet.
- **If it happens on every prompt**, the content is in the conversation, which Claude Code sends again with each prompt (and `/compact` too). Use `/rewind` to go back to before the prompt that brought it in, or start a new conversation; `--resume` of that conversation fails the same way. When the message says the part is sent with every request (a tool definition or a setting, not the conversation), `/rewind` won't help: update veil.
- Requests Claude Code makes in the background, such as the one that names the session, can be refused without a message. When Claude Code exits, `veil claude` lists what it couldn't send.

Refusals, and the gateway's own failures before a reply starts, are final: Claude Code doesn't send them again, on another model or otherwise. A refusal inside a feature Claude Code can do without (auto mode's safety checks, a turn's effort level) is the exception: Claude Code sends the request again without it. A busy data folder or an unreachable API is retried as usual.

## Usage

### What gets detected

| Type | Matched | Not matched |
| --- | --- | --- |
| `EMAIL` | `jan.n@example.com`, `first.last+tag@sub.example.co.uk`, `sean.o'brien@example.com`, `łucja@example.com` | `user@localhost`, `name@example` |
| `PHONE` | `555-123-4567`, `(555) 123-4567`, `+1 555 123 4567`, `555-123-4567 ext. 89`, `+44 20 7946 0958`, `(+48) 123 456 789` | `5551234567` (no separators), `601 234 567` (no `+` country code), `2024-01-15` |
| `IPV4` | `192.168.0.1`, `10.0.0.1` in `10.0.0.1:8080` | `256.1.1.1`, `192.168.01.1`, `1.2.3.4.5` |
| `IPV6` | `2001:db8::1`, `fe80::1ff:fe23:4567:890a`, `::ffff:192.0.2.1`, `[IPv6:2001:db8::1]`, `2001:db8::1` in `2001:db8::1:54321` | `::1` (loopback), `12:30:45`, `std::vector`, `a[1::2]` |
| `CREDIT_CARD` | `4111 1111 1111 1111`, `5555-5555-5555-4444`, `378282246310005` | digit runs that fail the Luhn check, lack a card network's prefix or length, or aren't in a printed card layout |
| `IBAN` | `DE89 3704 0044 0532 0130 00`, `GB82WEST12345698765432` | text that isn't laid out like an IBAN, uses a country code that doesn't issue IBANs, or fails the mod-97 checksum |

Phone numbers outside North America need a leading `+` and country code. That keeps order numbers, IDs, and amounts from being masked as phones. Numbers and addresses are also found inside Chinese, Japanese, and Korean text, where there are no spaces around them.

### Custom patterns

```python
>>> shield = Shield(custom_patterns={"ORDER": r"#\d{5}"})
>>> shield.mask("Where is order #12345? Ask jan.n@example.com").text
'Where is order [ORDER_1]? Ask [EMAIL_1]'
```

A custom pattern with a built-in name (`"EMAIL"`, `"PHONE"`, `"IPV4"`) replaces that built-in. Type names use upper-case letters, digits, and underscores, and start with a letter.

Python's `\b` treats underscores and Chinese, Japanese, and Korean characters as word characters, so `r"\bEMP\d{6}\b"` won't match `_EMP123456_` or `ID是EMP123456`. To reject only glued ASCII letters and digits, use `r"(?<![A-Za-z0-9])EMP\d{6}(?![A-Za-z0-9])"`.

### Names and other values patterns can't find

```python
>>> shield = Shield()
>>> shield.add_entity("Anna Kowalska", "PERSON")
>>> shield.add_entity("Jan", "PERSON")
>>> shield.mask("Anna Kowalska and Jan meet in January.").text
'[PERSON_1] and [PERSON_2] meet in January.'
```

Registered values match exactly and case-sensitively, but never inside a longer word: `"Jan"` does not mask the start of `"January"`. In scripts written without spaces (Chinese, Japanese, Thai, ...), where word boundaries aren't visible, a registered value matches wherever it appears. When matches overlap, the longest one wins. On a tie, a registered value beats a pattern match. If the match that lost sticks out past the winner, the two are masked together as one placeholder of the winner's type, so no part of either stays visible:

```python
>>> shield = Shield(detectors=[])
>>> shield.add_entity("Anna Maria", "PERSON")
>>> shield.add_entity("Maria Kowalska", "PERSON")
>>> shield.mask("Present: Anna Maria Kowalska").text
'Present: [PERSON_1]'
```

### Multi-turn conversations

Within one `Shield`, a value keeps its placeholder across every `mask()` call. Use one `Shield` per conversation.

```python
>>> shield = Shield()
>>> shield.mask("Reach me at jan.n@example.com").text
'Reach me at [EMAIL_1]'
>>> shield.mask("Or anna.k@example.com, cc jan.n@example.com").text
'Or [EMAIL_2], cc [EMAIL_1]'
>>> shield.reset()  # new conversation: forget mappings, restart numbering
>>> shield.mask("anna.k@example.com").text
'[EMAIL_1]'
```

`reset()` keeps registered entities and custom patterns.

### Keeping mappings between runs

A `Shield` keeps its mappings in memory, so they are gone when the process exits. To keep them, and to share them between processes (a web server's workers, or one Claude Code hook call after another), give it an `SQLiteVault`:

```python
>>> from datetime import timedelta
>>> from veil import SQLiteVault
>>> shield = Shield(vault=SQLiteVault("conversations.db", session="chat-42"))
>>> shield.mask("Email jan.n@example.com about the invoice.").text
'Email [EMAIL_1] about the invoice.'
>>> later = Shield(vault=SQLiteVault("conversations.db", session="chat-42"))
>>> later.restore("I emailed [EMAIL_1].").text  # a new Shield, maybe a new process
'I emailed jan.n@example.com.'
>>> later.vault.purge(timedelta(days=30))  # delete sessions unused for 30 days
0
```

One file holds any number of sessions, each with its own placeholders; `reset()` clears only its own. Processes and threads can use the same file at once: a value always gets one placeholder, and a number is never handed out twice. The leak check keeps knowing the spellings and merged matches it has seen, across `Shield`s and processes. With `normalize=True`, two spellings of a new value masked at the same moment by two processes can still get two placeholders; each restores correctly.

The vault holds mappings, not settings: build every `Shield` with the same `custom_patterns` and `detectors`, and call `add_entity` again for the same names, or a new `Shield` won't mask them (the leak check still warns). A copy of an `SQLiteVault` (pickled, or with `copy.deepcopy`) opens the same file and session, so it shares the data.

The file holds the real values in plain text. veil creates it readable by its owner only and overwrites values when they are deleted; keep it on an encrypted disk, and `purge` old sessions. It uses SQLite's write-ahead log, which needs a local disk (not a network share).

### One placeholder however a value is written

By default, values are matched exactly, so `(555) 555-0123` and `555.555.0123` get two placeholders. Pass `normalize=True` to give every spelling of the same value one placeholder:

```python
>>> shield = Shield(normalize=True)
>>> shield.mask("Call (555) 555-0123 or 555.555.0123, or mail Jan.N@Example.com.").text
'Call [PHONE_1] or [PHONE_1], or mail [EMAIL_1].'
>>> shield.mask("New address: jan.n@example.com").text
'New address: [EMAIL_1]'
>>> shield.restore("I called [PHONE_1] and wrote to [EMAIL_1].").text
'I called (555) 555-0123 and wrote to Jan.N@Example.com.'
```

`restore()` writes each value the way it was first seen. What counts as the same value:

| Type | Shares a placeholder | Stays apart |
| --- | --- | --- |
| `EMAIL` | Different letter case | Plus tags and dots (`jan+news@`, `j.an@`), anything glued before the address (`ADMIN_EMAIL=`) |
| `PHONE` | Separators and brackets, a `+1` or `1` before a North American number, an explicit `(0)` trunk prefix (`+44 (0)20 ...`) | A different extension, a trunk `0` without brackets (`+44 020 ...`), a label captured with the number (`Tel: ...`) |
| `IPV6` | Upper or lower case, zeros written out or compressed | |
| `CREDIT_CARD` | Spaces and dashes between the groups | |
| `IBAN` | Spaces between the groups, letter case | |

Values of different types never share a placeholder, and names, IPv4 addresses, and custom types are always matched exactly. Each `MaskedEntity` still holds the text as it was written, so several entities can share a placeholder with different values.

### When the model rewrites a placeholder

Models sometimes change a placeholder's case or spacing, or use CJK or Markdown-escaped brackets. `restore()` still finds these, and lists each one in `repaired`:

```python
>>> shield = Shield()
>>> shield.add_entity("Jan Nowak", "PERSON")
>>> shield.mask("Write to Jan Nowak").text
'Write to [PERSON_1]'
>>> result = shield.restore("Dear [person 1], and 【PERSON_1】 again")
>>> result.text
'Dear Jan Nowak, and Jan Nowak again'
>>> [(r.written, r.placeholder) for r in result.repaired]
[('[person 1]', '[PERSON_1]'), ('【PERSON_1】', '[PERSON_1]')]
```

A rewritten form is only restored if it is in brackets and its normalized form (`[PERSON_1]`) is in the vault, so bracketed text like `[Figure 2]` is left alone. A rewritten form right after a word or a closing bracket, like the code subscript `scores[email1]`, is left alone too. To turn this off, use `Shield(tolerant_restore=False)`.

### Streaming replies

When a reply streams in, a placeholder can be split across chunks (`"[EMA"`, `"IL_1]"`). `restore_stream()` restores the chunks as they arrive, holding back only text that could still turn out to be a placeholder:

```python
>>> shield = Shield()
>>> shield.mask("Email jan.n@example.com").text
'Email [EMAIL_1]'
>>> chunks = ["I emailed [EMA", "IL_1] and ", "will follow up."]
>>> list(shield.restore_stream(chunks))
['I emailed ', 'jan.n@example.com and ', 'will follow up.']
```

Joined, the pieces are exactly what `restore()` gives for the whole reply, however it was split. Nothing but a possible placeholder is delayed: text from a bracket onward that could still become one, always under 76 characters. A Markdown link or an array index goes straight through. When the chunks come from callbacks rather than an iterable, use `shield.stream_restorer()`: call `feed(chunk)` for each chunk and send on what it returns, then `finish()` at the end, and `result()` for the count, warnings, and repairs.

### Warnings

veil reports problems instead of raising:

- **`mask()`** runs a leak check. If a known value (anything masked earlier or detected now) still appears in the masked text, it adds a warning. It also warns when the input already contains text that `restore()` would treat as a placeholder: an exact one such as `[PERSON_1]`, or a rewritten form such as `[Person 1]` of a type in use.
- **`restore()`** leaves unknown placeholders (well-formed, but not in the vault) unchanged and lists them.

```python
>>> shield = Shield()
>>> shield.restore("Hi [PERSON_7]").warnings
['Unknown placeholder [PERSON_7] was left unchanged.']
```

`wrap()` emits these warnings through Python's `warnings` module as `ShieldWarning`. To fail closed instead, pass `strict=True`. If masking warns, `wrap()` raises `ShieldError` and never calls the model. If restoring the reply warns, it raises instead of returning a partly restored reply:

```python
from veil import Shield, ShieldError

shield = Shield()
shield.mask("Call 555-123-4567")
safe_llm = shield.wrap(lambda prompt: "ok", strict=True)
try:
    safe_llm("Call 555-123-4567-2")  # the known number would reach the model
except ShieldError as error:
    assert error.stage == "mask"
```

By default, leak warnings quote the value that leaked. To log them safely, create the shield with `Shield(redact_warnings=True)`. Warnings then give only the type, placeholder, and length:

```python
>>> shield = Shield(redact_warnings=True)
>>> shield.mask("Call 555-123-4567").text
'Call [PHONE_1]'
>>> shield.mask("Call 555-123-4567-2").warnings
['Leak check: a known PHONE value ([PHONE_1]) still appears in the masked text.']
```

### Text that already looks like a placeholder

A document can contain placeholder-shaped text of its own, like a template with `[EMAIL_1]` in it. Left as it is, it would come back from the model as a real placeholder, and `restore()` would put a real value there. Add a `LiteralPlaceholderDetector` to mask such text too. It gets a placeholder of its own and restores to exactly what was written:

```python
>>> from veil import LiteralPlaceholderDetector, RegexDetector
>>> shield = Shield(detectors=[LiteralPlaceholderDetector({"EMAIL"}), RegexDetector()])
>>> masked = shield.mask("Template: Dear [EMAIL_1]. Sent by jan.n@example.com.").text
>>> masked
'Template: Dear [LITERAL_1]. Sent by [EMAIL_1].'
>>> shield.restore(masked).text
'Template: Dear [EMAIL_1]. Sent by jan.n@example.com.'
```

Give it the types that matter, so code like `row[COL_1]` is left alone (or `None` for every type). Keep the set the same for the whole conversation, so the same text always masks the same way.

When the restored text will be written somewhere, not just read, such as a tool call's arguments, restore only exact placeholders: `shield.restore(text, tolerant=False)`. A rewritten form like `[Email 1]` in such text is then left as it is.

### Plugging in your own parts

Detectors and vaults are protocols. Any object with `detect(text) -> list[Span]` is a detector, and anything that implements the `Vault` methods can replace `MemoryVault`.

```python
import re

from veil import RegexDetector, Shield, Span


class TicketDetector:
    def detect(self, text: str) -> list[Span]:
        return [
            Span(m.start(), m.end(), m.group(0), "TICKET", source="tickets")
            for m in re.finditer(r"\bTKT-\d+\b", text)
        ]


shield = Shield(detectors=[TicketDetector(), RegexDetector()])
assert shield.mask("TKT-42 from jan.n@example.com").text == "[TICKET_1] from [EMAIL_1]"
```

Registered entities (`add_entity`) are always detected, whichever detectors you pass.

## Limitations

- **Regex detection is not exhaustive.** Anything outside the formats above is missed. That includes national phone numbers without `+`, obfuscated addresses (`jan at example dot com`), card numbers split across lines or in unusual groupings, IPv4 addresses with a port or a zero-padded octet (`198.51.100.123.2222`, `010.000.100.123`), non-ASCII domain names (punycode `xn--` works), values split across lines, and addresses containing `?`, `*`, `` ` ``, `{`, `|`, or `}` (only the part after that character is masked). Some non-PII gets masked too: version strings like `1.2.3.4` look like IPv4 addresses, `icon@2x.png` looks like an email, and about one in ten runs of 13-19 digits with a card network's prefix passes the Luhn check (a list of four-digit IDs, a long order number). Don't make veil your only safeguard for regulated data.
- **Text glued to an email address can be masked with it.** Characters like `=`, `&`, and `/` can be part of an address (bounce addresses use `=`), so `ADMIN_EMAIL=jan@example.com` becomes a single `[EMAIL_1]`. Chinese or Japanese written right before an address (`連絡先はjan@example.com`) is masked with it too, because CJK characters can be part of an address as well. Add a space or quotes (`ADMIN_EMAIL="jan@example.com"`) to keep the text before it visible.
- **Overlapping matches are masked as one placeholder.** With `"Anna Kowalska"` registered, `Owner: Anna Kowalska/anna.k@example.com` becomes `Owner: [EMAIL_1]`: the `/` makes `Kowalska/anna.k@example.com` an address that overlaps the name. The model sees one placeholder where there were two values, and a reply that uses it restores both. The name, seen alone later, gets a placeholder of its own.
- **Names must be registered manually in v0.1.** Nothing detects names automatically. Only the exact strings you register are masked, so `"Jan Nowak"` does not cover `"Nowak"`, `"JAN NOWAK"`, or inflected forms like `"Janem Nowakiem"`. Register each form you expect.
- **Values are matched exactly unless you opt in.** Without `normalize=True`, `(555) 123-4567` and `555-123-4567` get different placeholders, and so do `Jan.N@Example.com` and `jan.n@example.com`. With it, `restore()` writes the first spelling seen, so a reply can come back spelled differently from the prompt. Names, IPv4 addresses, and custom types are never normalized. Upper-case letters merge with their lower-case forms by Python's `str.lower()` (`I` with `i`, `Σ` with `σ`), and so do characters that Unicode treats as the same (NFC).
- **The model must keep the brackets.** Rewritten forms like `[person 1]` are restored, but a placeholder without its brackets (`PERSON_1`, `(PERSON_1)`) is not.
- **`MemoryVault` lives in memory and isn't thread-safe.** Its mappings last as long as the `Shield`. Use `SQLiteVault` to keep them between runs, or to share them between threads and processes. `MemoryVault` and `SQLiteVault` also remember, for the leak check, the spellings merged by `normalize=True` and the matches inside a merged placeholder; with a vault of your own, each `Shield` remembers them separately.
- **Search patterns hold parts of values.** To search for many values at once, veil compiles regular expressions that contain up to 64 characters of them, and Python's `re` module may keep those in its cache after `reset()`. Call `re.purge()` if that matters.
- **A large vault adds to every call.** `mask()` takes about linear time in the length of the input, but every stored value is looked at on every call: about 10 ms per call at 100,000 values. A new `Shield(normalize=True)` on a large existing vault also works out the key of each stored value on its first lookup, about 2-3 µs per value. Text in which thousands of overlapping matches merge is checked one known value at a time, as all text was in v0.2.
- **Placeholders reveal types and counts.** The model can tell there are two people and one email address, just not who they are.
- **International numbers can take in digits that follow them.** When it's unclear where a number ends, veil masks too much rather than too little. In `+44 20 7946 0958 24 hours`, the separate ` 24` is masked with the number. In `+48 123 456 789 2024-01-15`, the `2024` of the date is masked with it. The model doesn't see those digits, but restoring still returns the exact original text. A short group glued to a word, such as `24h` or `9am`, is recognised and left out.

## Roadmap

- **Next:** `veil mask` and `veil restore` commands for copy-and-paste use with any chat app, and Claude Code hooks for the desktop app, which doesn't use a gateway.
- **Later:** an optional Presidio/spaCy detector for names, and normalizers for your own entity types.

## Development

```bash
uv sync                    # create .venv with pytest and ruff
uv run pytest              # tests, docstring examples, and this README's examples
uv run ruff check .
uv run ruff format --check .
```

The live tests check the Claude Code behavior the hooks rely on. They are skipped unless you opt in, and they need a logged-in `claude` CLI. Each one makes real, small model calls on the cheapest model:

```bash
VEIL_LIVE_CLAUDE=1 uv run pytest -m live
```

To rename the package:

1. In `pyproject.toml`, change `name` and the `[tool.hatch.version]` path.
2. Rename `src/veil/`.
3. Update the `veil` imports in `tests/` and in this README.
4. Run `uv lock`.

Internal imports are relative, and no class, function, or docstring contains "veil".

## License

MIT. See [LICENSE](LICENSE).
