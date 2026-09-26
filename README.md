# veil

**veil replaces personal information with placeholders before your text reaches an AI model, then puts the real values back into the model's reply.**

The model never sees the real data. veil is pure Python with no runtime dependencies.

> Status: v0.3, alpha. "veil" is a working name. See [CHANGELOG.md](CHANGELOG.md).

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

One file holds any number of sessions, each with its own placeholders; `reset()` clears only its own. Processes and threads can use the same file at once: a value always gets one placeholder, and a number is never handed out twice. The leak check keeps knowing the spellings and merged matches it has seen, across `Shield`s and processes.

The file holds the real values in plain text. veil creates it readable by its owner only; keep it on an encrypted disk, and `purge` old sessions. It uses SQLite's write-ahead log, which needs a local disk (not a network share).

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

- **Next:** ready-made Claude Code hooks, `veil mask` and `veil restore` commands for copy-and-paste use with any chat app, and streaming restore for placeholders split across chunks.
- **Later:** a local gateway in front of the model's API (so everything a tool like Claude Code sends is masked), an optional Presidio/spaCy detector for names, and normalizers for your own entity types.

## Development

```bash
uv sync                    # create .venv with pytest and ruff
uv run pytest              # tests, docstring examples, and this README's examples
uv run ruff check .
uv run ruff format --check .
```

To rename the package:

1. In `pyproject.toml`, change `name` and the `[tool.hatch.version]` path.
2. Rename `src/veil/`.
3. Update the `veil` imports in `tests/` and in this README.
4. Run `uv lock`.

Internal imports are relative, and no class, function, or docstring contains "veil".

## License

MIT. See [LICENSE](LICENSE).
