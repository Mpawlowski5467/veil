# Changelog

## Unreleased

### Added

- `SQLiteVault(path, session=...)` keeps mappings in an SQLite file (standard library only), so they survive the process and can be shared between processes and threads. One file holds many sessions; `clear()` empties only its own, and `purge(older_than)` deletes sessions unused for that long. Writes take the database's write lock, so concurrent processes never give one value two placeholders or hand out a number twice. Each vault keeps a cache that it refreshes only when another connection has changed the file, and a `mask()` or `restore()` call checks the file once and writes in one transaction, so masking against it is about as fast as against a `MemoryVault`. The spellings and merged matches the leak check remembers are stored too, and so is which placeholders are merged, so a new `Shield` never normalizes them. A vault inherited across `fork()` opens its own connection in the child. The file is created owner-only, holds the real values in plain text, and overwrites deleted values.

- `Shield.restore_stream(chunks)` restores a reply that arrives in pieces, and `Shield.stream_restorer()` returns a `StreamRestorer` to feed pieces one at a time (`feed()`, `finish()`, `result()`). A placeholder split across pieces is held back until it is complete, and nothing else is delayed; joined, the pieces are exactly what `restore()` gives for the whole text.

- `LiteralPlaceholderDetector(types)` masks text that is already shaped like a placeholder of those types (such as a template's `[EMAIL_1]`), as type `LITERAL`, so it restores to exactly what was written instead of a real value.
- `restore()`, `stream_restorer()`, and `restore_stream()` take `tolerant=` to override `tolerant_restore` for one call, e.g. `tolerant=False` for text that will be written or run, where only exact placeholders should become real values.

- `veil.gateway` (in progress; not usable on its own yet): `RequestMasker` masks a Messages API request body. Every field has a rule: text the model reads is masked (system text, messages, tool results, past tool inputs, stop sequences, auto mode's review context), settings, tool definitions, signed thinking, and image and PDF data pass unchanged, and anything else is refused with `UnsupportedRequestError`, which names the field but never a value. A reply the gateway restored goes back as the model wrote it, from a `Ledger`; a line that still holds a known value after masking is withheld; and a text masks the same way every time it is sent. `ResponseRestorer` restores the streamed reply: text as it arrives (holding back only a possible placeholder, and keeping citations behind the text before them), and each tool call's input once the call is complete, exact placeholders only; thinking and every other event pass through unchanged, and a tool input that can't be read ends the stream with an error rather than reaching the client unrestored. Each restored reply is recorded in the ledger. `restore_message()` does the same for a reply that isn't streamed.
- `veil.gateway.Gateway` serves this over HTTP (standard library only) for a client such as Claude Code: it listens on 127.0.0.1 only, refuses a wrong Host header, browser requests, and requests without its per-launch secret (which it never forwards), serves only the model endpoints and the connectivity check, and keeps one conversation per session id (`Sessions`). A request it can't mask is answered with an error in the API's format and never forwarded; no error is printed, since it could quote a request. A held tool call gets keep-alive comments.
- The gateway keeps its conversations in `~/.veil`, a folder only its owner may use: placeholders in an `SQLiteVault` (one session per client session id) and the model's own replies in an `SQLiteLedger`, which holds only masked text and hashes, never a real value. Both last across launches and are purged after `retention_days` unused. Settings come from `~/.veil/config.json` only, never from a project, and are checked strictly (an unknown key or wrong value is an error that names the key, not the value): registered values by type, custom patterns, whether to mask the user's git name and email (on by default), retention, the system note, and MCP tools allowed to receive real values.
- A `veil` command (also `python -m veil`). `veil claude [ARGS...]` starts a private gateway for one Claude Code process and runs `claude ARGS...` through it, with `--settings` that outrank a project's settings and the environment: the gateway's address and per-launch secret, nonessential traffic off, and tools that send content through Anthropic-hosted services or to other sessions (`PushNotification`, `RemoteTrigger`, `SendUserFile`, `SendMessage`, `ShareOnboardingGuide`, `DesignSync`, `Artifact`) denied. It also runs two hooks: a shell command that holds a real value (put back by the gateway) asks the user first, a web request or MCP call holding one is refused (unless the MCP tool is listed in `allow_mcp_tools`), and a prompt is held back if Claude Code isn't routed through the gateway after all; both fail closed. When Claude Code exits, so does the gateway. `veil gateway` runs a long-lived one on a fixed port with a stored secret, and prints the settings to point a client at it. `veil forget --session ID` or `--all` deletes stored mappings.
- `StreamRestorer.held` says how many received characters are still held back.

### Changed

- `mask()` warns about placeholder-like input only when that text is left unmasked. Text that was masked itself (by `LiteralPlaceholderDetector`, or as part of a registered value) restores as written, so it no longer warns.
- Placeholders have a maximum length, so a stream never holds back more than 76 characters: entity types are at most 64 characters (`add_entity` and `custom_patterns` reject longer ones), placeholder numbers at most nine digits, and a rewritten placeholder is only restored with up to eight spaces or tabs of padding inside its brackets.
- A `Shield` can be shared between threads: its `mask()` calls take turns.
- A pickled `Shield(normalize=True)` no longer holds the values cached by its normalization index; they are rebuilt from the vault.
- `restore()` only looks at the types in the vault when it meets a rewritten placeholder it can't restore.

## 0.3.0

### Added

- `Shield(normalize=True)` gives one placeholder to every spelling of the same value: email addresses that differ only in case; phone numbers written with different separators, with or without `+1`, or with a `(0)` trunk prefix (extensions are kept apart); IPv6 addresses in any form; and card numbers and IBANs with or without spaces. `restore()` writes each value as it was first seen. Values of different types never merge, and names, IPv4 addresses, and custom types stay exact, as does anything that isn't one whole value (`Tel: ...`, `ADMIN_EMAIL=...`). The leak check still knows every spelling it has masked until `reset()` (with a vault other than `MemoryVault`, each `Shield` knows only the spellings it masked itself). Off by default.

### Changed

- Overlapping matches no longer leave part of a value visible. When the match that lost an overlap sticks out past the winner with a letter or digit, the two are masked together as one placeholder of the winner's type, and the entity's `source` is `"merged"`. This replaces the "Partial mask" warning, so `wrap(strict=True)` no longer raises for such input. The matches inside a merged placeholder get no placeholder of their own, but the leak check keeps knowing them (with a vault other than `MemoryVault`, only in the `Shield` that masked them).
- The end of an IP address is no longer read as a US phone number. A number without a country code may not start inside an IPv4 address (`100.123 2222` in `ssh 198.51.100.123 2222`, `123 443 1024` in `198.51.100.123 443 1024`), and the `1` ending an IPv6 address is no longer taken as a country code (`2001:db8::1 555-123-4567`). After `::` or a hex group between colons (`id:42:1 212 200 0123`), that `1` now stays visible while the rest of the number is masked.

### Performance

- Resolving overlapping matches takes linear time. 200,000 detected values used to take about 3.5 s.
- The leak check searches for every known value in about one pass, with an index kept from call to call, instead of scanning the text once per value. Masking 1.3 MB with 48,000 values went from 4.5 s to 0.4 s.
- A large vault costs less per call: with 100,000 stored values, masking a short prompt went from 70 ms to 10 ms.
- Registered values are found in about one pass over the text, however many there are (from 8 on). Detecting 5,000 registered names in 1.2 MB went from 2.2 s to 0.05 s.

## 0.2.0

### Added

- New built-in detectors: `IPV6` (also after a `label:` and before an unbracketed port), `CREDIT_CARD` (issuer prefix and length, printed layout, and Luhn check), and `IBAN` (printed layout, issuing country, and mod-97 checksum). Cards and IBANs may be grouped with no-break, thin, or ideographic spaces.
- Tolerant restore: placeholders the model rewrote, such as `[person 1]`, `[ PERSON_1 ]`, `[PERSON_01]`, `【PERSON_1】`, or a Markdown-escaped `\[PERSON_1\]`, are restored when their normalized form is in the vault. They are listed in the new `RestoreResult.repaired`. Code subscripts like `scores[email1]` are left alone, and `mask()` warns when the input already contains such rewritten forms of a type in use. Turn this off with `Shield(tolerant_restore=False)`.
- `Shield.wrap(llm, strict=True)` raises the new `ShieldError` instead of warning. It raises before calling the model if masking warned, and after the call if restoring warned.
- `Shield(redact_warnings=True)` describes leaked values by type, placeholder, and length instead of quoting them, so warnings and errors are safe to log.
- CI on Python 3.10 to 3.14, a seeded fuzz test, and mypy and pyright in the dev dependencies.

### Changed

- `RegexDetector.entity_types` now includes `IPV6`, `CREDIT_CARD`, and `IBAN`, so text containing them is masked by default.
- A `(0)` trunk prefix (`+49 (0)711 ...`) no longer counts toward the 15-digit limit for phone numbers, so a German or Austrian extension is masked along with the number.
- `[PERSON_01]` is restored as `[PERSON_1]` when that placeholder exists. Before, it was reported as unknown.

## 0.1.0

First release: regex detection of emails, phone numbers, and IPv4 addresses; custom patterns; manually registered entities; an in-memory vault; exact restoring; and the `Shield` API.
