# Changelog

## Unreleased

### Performance

- Resolving overlapping matches takes linear time. 200,000 detected values used to take about 3.5 s.

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
