# Changelog

## 0.2.0

### Added

- New built-in detectors: `IPV6`, `CREDIT_CARD` (issuer prefix and length, printed layout, and Luhn check), and `IBAN` (mod-97 checksum).
- Tolerant restore: placeholders the model rewrote, such as `[person 1]`, `[ PERSON_1 ]`, `[PERSON_01]`, `【PERSON_1】`, or a Markdown-escaped `\[PERSON_1\]`, are restored when their normalized form is in the vault. They are listed in the new `RestoreResult.repaired`. Turn this off with `Shield(tolerant_restore=False)`.
- `Shield.wrap(llm, strict=True)` raises the new `ShieldError` instead of warning. It raises before calling the model if masking warned, and after the call if restoring warned.
- `Shield(redact_warnings=True)` describes leaked values by type, placeholder, and length instead of quoting them, so warnings and errors are safe to log.
- CI on Python 3.10 to 3.14, a seeded fuzz test, and mypy and pyright in the dev dependencies.

### Changed

- `RegexDetector.entity_types` now includes `IPV6`, `CREDIT_CARD`, and `IBAN`, so text containing them is masked by default.
- A `(0)` trunk prefix (`+49 (0)711 ...`) no longer counts toward the 15-digit limit for phone numbers, so a German or Austrian extension is masked along with the number.
- `[PERSON_01]` is restored as `[PERSON_1]` when that placeholder exists. Before, it was reported as unknown.

## 0.1.0

First release: regex detection of emails, phone numbers, and IPv4 addresses; custom patterns; manually registered entities; an in-memory vault; exact restoring; and the `Shield` API.
