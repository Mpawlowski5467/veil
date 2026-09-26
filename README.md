# veil

**veil replaces personal information with placeholders before your text reaches an AI model, then puts the real values back into the model's reply.**

The model never sees the real data. veil is pure Python with no runtime dependencies.

> Status: v0.1, alpha. "veil" is a working name.

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

Registered values match exactly and case-sensitively, but never inside a longer word: `"Jan"` does not mask the start of `"January"`. In scripts written without spaces (Chinese, Japanese, Thai, ...), where word boundaries aren't visible, a registered value matches wherever it appears. When matches overlap, the longest one wins. On a tie, a registered value beats a pattern match.

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

### Warnings

veil reports problems instead of raising:

- **`mask()`** runs a leak check. If a known value (anything masked earlier or detected now) still appears in the masked text, it adds a warning. It also warns when a detected value was only partly masked because it overlapped another match that was kept, and when the input already contains placeholder-like text such as `[PERSON_1]`, because `restore()` would replace it.
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

By default, leak and partial-mask warnings quote the value that leaked. To log them safely, create the shield with `Shield(redact_warnings=True)`. Warnings then give only the type, placeholder, and length:

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

- **Regex detection is not exhaustive.** Anything outside the formats above is missed. That includes national phone numbers without `+`, obfuscated addresses (`jan at example dot com`), IPv6, non-ASCII domain names (punycode `xn--` works), values split across lines, and addresses containing `?`, `*`, `` ` ``, `{`, `|`, or `}` (only the part after that character is masked). Some non-PII gets masked too: version strings like `1.2.3.4` look like IPv4 addresses, and `icon@2x.png` looks like an email. Don't make veil your only safeguard for regulated data.
- **Text glued to an email address can be masked with it.** Characters like `=`, `&`, and `/` can be part of an address (bounce addresses use `=`), so `ADMIN_EMAIL=jan@example.com` becomes a single `[EMAIL_1]`. Chinese or Japanese written right before an address (`連絡先はjan@example.com`) is masked with it too, because CJK characters can be part of an address as well. Add a space or quotes (`ADMIN_EMAIL="jan@example.com"`) to keep the text before it visible.
- **Names must be registered manually in v0.1.** Nothing detects names automatically. Only the exact strings you register are masked, so `"Jan Nowak"` does not cover `"Nowak"`, `"JAN NOWAK"`, or inflected forms like `"Janem Nowakiem"`. Register each form you expect.
- **Values are matched exactly, with no normalization.** `(555) 123-4567` and `555-123-4567` get different placeholders, and so do `Jan.N@Example.com` and `jan.n@example.com`.
- **The model must copy placeholders exactly.** `[person_1]`, `PERSON_1`, or `[PERSON 1]` in a reply are not restored.
- **The vault lives in memory.** Mappings last as long as the `Shield` and are gone when the process exits. It is not thread-safe.
- **Placeholders reveal types and counts.** The model can tell there are two people and one email address, just not who they are.
- **International numbers can take in digits that follow them.** When it's unclear where a number ends, veil masks too much rather than too little. In `+44 20 7946 0958 24 hours`, the separate ` 24` is masked with the number. In `+48 123 456 789 2024-01-15`, the `2024` of the date is masked with it. The model doesn't see those digits, but restoring still returns the exact original text. A short group glued to a word, such as `24h` or `9am`, is recognised and left out.

## Roadmap

- **v0.2:** tolerant placeholder restoring (case, missing brackets), optional value normalization, more built-in types (IPv6, credit cards with a Luhn check, IBAN).
- **Later:** an optional Presidio/spaCy detector for names, a persistent SQLite vault, and streaming restore for placeholders split across chunks.

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
