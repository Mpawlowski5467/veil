# Local PDF text import (unreleased)

PDF text import is implemented in this checkout for the 1.0 plan. It is not in
the published 0.6.0b2 package. It extracts a local PDF's selectable text for the
existing masking workflow. It makes no provider call and leaves the source PDF
unchanged.

## Desktop preview

```bash
python -m pip install '.[desktop,pdf]'
veil desktop
```

Open **Masking preview → Import file… → PDF text…** and select a local PDF. Review
the extracted text, then choose **Preview masking**. The preview uses your
configured patterns, exact registrations, Git identity setting, and optional
local name model. Extraction runs in a separate process while the logo loader
animates. Cancelling the picker preserves the current preview; extraction errors
preserve the previous input and clear the masked output.

Preview text and mappings stay in memory. Clear removes the text and its undo
history. These temporary placeholders are for inspection; use a saved CLI
session below when you need to restore a model's reply later.

## Mask PDF text for a conversation

```bash
python -m pip install '.[pdf]'
veil mask --session invoice-notes --pdf invoice.pdf > masked-notes.txt
veil restore --session invoice-notes < copied-reply.txt > restored-reply.txt
```

Send only the reviewed masked text to your chat. Keep the same session label
when restoring replies. `--pdf` replaces stdin as input for `mask` and cannot be
combined with `--clipboard`. `--review` and configured secret review work through
the existing local terminal review flow. Extraction summaries go to stderr;
stdout contains only the successfully masked text. Errors produce no text on
stdout. As with other shell pipelines, redirection can still create or truncate
an output file before Veil runs; never redirect over your source file.

Saved sessions keep detected original values in the existing owner-only,
plaintext local vault. Use the same global `--data-dir PATH` as your registrations
and `veil forget --session invoice-notes` to delete that session's mappings.
The PDF itself is not copied into the vault.

## Supported content and limits

- Selectable page text, in the parser's reading order, with two newlines between
  pages. Multi-column layouts, tables, unusual fonts, and OCR text layers can
  extract inaccurately; review the text before masking or sharing it.
- Up to 20 MiB, 100 pages, 32768 extracted characters, and 2 MiB of decoded page
  content per page. Extraction has a 20-second timeout. Limits refuse the whole
  import rather than silently truncating it. These are input/operation limits,
  not a security sandbox or a universal memory ceiling for the parser.
- Encrypted PDFs are refused, including those with an empty password. Unlock a
  trusted document locally and choose an unencrypted copy. Passwords are never
  requested or put in command arguments.
- A document with no extractable text is refused with an OCR explanation. Mixed
  documents report the number of pages with no text; those may be blank or
  scanned. A page with a little selectable text may still contain unread images.
  Use [local OCR](ocr.md) through **Import file… → Scan PDF or image…** or `veil mask --ocr FILE`
  to recognize page pixels, including scanned content in mixed documents.
- Images, handwriting, embedded files, metadata, comments, interactive form
  values, and visual content outside the page text layer are excluded. Links and
  embedded scripts are not opened or executed by this import workflow.

The output is plain masked text. This feature does not produce a redacted PDF
or make the original PDF safe to upload. Attaching a PDF directly to Claude,
Codex, or another chat does not invoke this importer. Existing gateway attachment
rules are unchanged; see the [compatibility matrix](compatibility.md).

Extraction uses the optional [pypdf parser](https://pypdf.readthedocs.io/en/stable/user/extract-text.html).
It has no OCR engine and cannot guarantee visual reading order. The base Veil
install remains dependency-free; normal text commands do not load the parser.

## Validation

Fictional PDF regressions cover multi-page text, accents, metadata exclusion,
unchanged sources, configured masking and persistent reply restoration, missing
dependencies, encrypted/malformed/empty documents, extraction limits, and private
errors. Native UI checks cover import completion, errors, picker cancellation,
and clearing undo history. Representative user documents, cross-platform desktop
journeys, independent OCR quality evaluation, and PDF redaction remain separate work.
On macOS, a fictional two-page PDF was also selected through the native file
picker, imported, and masked in the visible desktop preview. The email became
`[EMAIL_1]`; the unregistered fictional name stayed visible with recognition off.
