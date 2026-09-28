# Changelog

## 0.4.1

### Fixed

- **`veil claude` works on Claude Opus 5.5 and Claude Fable 5.1.** On these models (the `opus`, `fable` and `best` aliases, and `opusplan` in plan mode) Claude Code 2.1.283 sets the effort turn by turn, in the `output_config` of `role: "system"` messages in the conversation. The gateway refused any message field but `role` and `content`, so every request failed with "400 the gateway can't mask messages[1]". It now accepts `output_config` on a system message when it is exactly `{"effort": ...}` with one of `low`, `medium`, `high`, `xhigh` or `max`, and passes it on as it is; the message's text is masked as before. Anything else in it is refused, so no text goes out unmasked that way. A refused effort names `output_config`, which Claude Code takes as a sign to send the conversation again without its per-turn effort, so an effort level added later leaves the session working instead of failing.

### Changed

- A message field the gateway has no rule for is named in the refusal, like other unknown fields (`messages[0].clear_at: unknown field`), instead of "a message has only role and content".
- The request census in `tests/gateway_payloads/` now includes what Claude Code sends on Opus 5.5 and Sonnet 5, interactive requests included, each scenario labelled with its model, and a test fails when a recorded message field has no rule. The live masking tests (`VEIL_LIVE_MODEL=opus` and so on) check that the per-turn effort reaches the API, and compare a resumed session's history as the API reads it, since Claude Code sends one reminder as a text block or as plain text depending on where it is.

## 0.4.0

### Added

- **`veil claude`: Claude Code through a masking gateway.** `veil claude [ARGS...]` (or `python -m veil claude`) starts a gateway on a free local port for one Claude Code process, runs `claude ARGS...` through it, and stops it when Claude Code exits. Everything Claude Code sends to the model is masked on the way out: prompts and pasted text, `@`-attached files, every tool result (a failed command's output too), `CLAUDE.md` and memory, the git status, and the account email Claude Code adds. Every reply is restored on the way back, so tools act on real values (an Edit matches the text on disk) while the model only sees placeholders. See "Using it with Claude Code" in the README for what it covers and what it doesn't.
  - Claude Code gets the gateway through `--settings`, which outrank a project's settings, the user's, and the environment. They also turn nonessential traffic, Remote Control and other providers off, keep hooks on, deny tools that send content through Anthropic-hosted services or to other sessions (`PushNotification`, `RemoteTrigger`, `SendUserFile`, `SendFile`, `SendMessage`, `ShareOnboardingGuide`, `DesignSync`, `Artifact`), and run two hooks that fail closed: one checks tool calls for real values (MCP tools listed in `allow_mcp_tools` may receive them), one holds a prompt back unless Claude Code is still routed through the gateway.
  - The gateway listens on 127.0.0.1 only and refuses a wrong Host header, browser requests, and requests without its per-launch secret, which it never forwards. It serves only the model endpoints. Every field of a request has a rule (mask, pass through, or refuse); a request it can't account for is refused in the API's error format, never forwarded, and nothing is printed that could quote a request.
  - A known value is masked wherever it is, even glued to a word (`Jan Nowakem` goes out as `[PERSON_1]em`), and when Claude Code sends a restored reply back as history, the gateway sends the model's own words. Masking is repeatable, so history, prompt caching, and signed thinking stay the same from request to request, and across launches. Thinking that holds a known value (from before it was masked) is dropped, since it can't be changed.
  - The hook checks every tool call but plain file reads and edits: shell commands holding a real value ask first, tools that keep data on this machine go ahead, and any other call holding one is refused. `veil claude` checks that the hooks run before starting Claude Code, refuses options and settings that would bypass the gateway (`--settings`, `--bare`, `--safe-mode`, an existing `ANTHROPIC_BASE_URL`), keeps the gateway's secret off the command line, and passes SIGTERM on to Claude Code; before each prompt a hook asks the gateway to prove it holds the secret, so nothing else on its port can pass for it.
  - Placeholders are kept per Claude Code session in `~/.veil/vault.db`, and the model's replies in `~/.veil/ledger.db` (masked text and hashes only); both are owner-only and purged after `retention_days` unused.
  - Settings come from `~/.veil/config.json` only, never from a project, and are checked strictly: registered values, custom patterns, the user's git name and email (on by default), retention, a system-prompt note about placeholders, and MCP tools allowed to receive real values.
  - `veil gateway` runs a long-lived gateway with a stored secret and prints the settings to point a client at it; `veil forget --session ID` or `--all` deletes stored mappings.
  - The pieces are in `veil.gateway` (`Gateway`, `Sessions`, `RequestMasker`, `ResponseRestorer`, `SQLiteLedger`, `load_settings`, ...).
- `SQLiteVault(path, session=...)` keeps mappings in an SQLite file (standard library only), so they survive the process and can be shared between processes and threads. One file holds many sessions; `clear()` empties only its own, and `purge(older_than)` deletes sessions unused for that long. Writes take the database's write lock, so concurrent processes never give one value two placeholders or hand out a number twice. Each vault keeps a cache that it refreshes only when another connection has changed the file, and a `mask()` or `restore()` call checks the file once and writes in one transaction, so masking against it is about as fast as against a `MemoryVault`. The spellings and merged matches the leak check remembers are stored too, and so is which placeholders are merged, so a new `Shield` never normalizes them. A vault inherited across `fork()` opens its own connection in the child. The file is created owner-only, holds the real values in plain text, and overwrites deleted values.
- `Shield.restore_stream(chunks)` restores a reply that arrives in pieces, and `Shield.stream_restorer()` returns a `StreamRestorer` to feed pieces one at a time (`feed()`, `finish()`, `result()`, `held`). Only text that could still become a placeholder is held back (a bracket and what follows it, under 76 characters); joined, the pieces are exactly what `restore()` gives for the whole text.
- `LiteralPlaceholderDetector(types)` masks text that is already shaped like a placeholder of those types (such as a template's `[EMAIL_1]`), as type `LITERAL`, so it restores to exactly what was written instead of a real value.
- `restore()`, `stream_restorer()`, and `restore_stream()` take `tolerant=` to override `tolerant_restore` for one call, e.g. `tolerant=False` for text that will be written or run, where only exact placeholders should become real values.
- Live tests (`VEIL_LIVE_CLAUDE=1 uv run pytest -m live`) check, against the real Claude Code CLI, the behavior the gateway relies on and the gateway itself end to end.

### Changed

- `mask()` warns about placeholder-like input only when that text is left unmasked. Text that was masked itself (by `LiteralPlaceholderDetector`, or as part of a registered value) restores as written, so it no longer warns.
- Placeholders have a maximum length: entity types are at most 64 characters (`add_entity` and `custom_patterns` reject longer ones), placeholder numbers at most nine ASCII digits, and a rewritten placeholder is only restored with up to eight spaces or tabs of padding inside its brackets. A vault made by an earlier version with a longer type keeps its values, but their placeholders are no longer recognized, so they aren't restored.
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
