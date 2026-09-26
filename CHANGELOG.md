# Changelog

## Unreleased

### Changed

- Overlapping matches no longer leave part of a value visible. When the match that lost an overlap sticks out past the winner with a letter or digit, the two are masked together as one placeholder of the winner's type, and the entity's `source` is `"merged"`. This replaces the "Partial mask" warning, so `wrap(strict=True)` no longer raises for such input. The matches inside a merged placeholder get no placeholder of their own, but the leak check keeps knowing them.
- The end of an IP address is no longer read as a US phone number. In `ssh 198.51.100.123 2222`, `100.123 2222` was detected as a phone and overlapped the address; and in `2001:db8::1 555-123-4567` the `1` ending the IPv6 address was taken as the number's country code.

### Performance

- Resolving overlapping matches takes linear time. 200,000 detected values used to take about 3.5 s.
- The leak check searches for every known value in about one pass, with an index kept from call to call, instead of scanning the text once per value. Masking 1.3 MB with 48,000 values went from 4.5 s to 0.6 s.
- Registered values are found in about one pass over the text, however many there are (from 8 on). Detecting 5,000 registered names in 1.4 MB went from 2.2 s to 0.1 s.

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
