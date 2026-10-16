# Local PDF and image OCR (unreleased)

This checkout adds an explicit local OCR workflow to the 1.0 plan. It is not in
the published 0.6.0b2 package. Scan an image or a PDF, review the recognized text,
then mask it with your existing rules. The source file remains unchanged.

## Setup

From the checkout, install the optional dependencies:

```bash
python -m pip install '.[desktop,ocr]'
veil desktop
```

On macOS, OCR uses Apple's on-device Vision framework through PyObjC. On Linux
and Windows, install [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html)
with English language data and make `tesseract` available on `PATH`. For example,
Debian/Ubuntu users can install `tesseract-ocr tesseract-ocr-eng`. No OCR package,
language model, or provider is downloaded when a document is scanned. Installing
dependencies or the system engine is a separate setup step.

The base Veil package still has no runtime dependencies. Ordinary text masking
does not import Pillow, PDFium, pypdf, or Vision.

## Desktop

Open **Masking preview → Import file… → Scan PDF or image…** and select a local file. The logo
loader runs while recognition happens in a disposable process. All PDF pages
are rendered, so a selectable header does not hide scanned content underneath.
Image orientation metadata is applied; transparency is composited on white.

Correct misread text in **Your original text**, then choose **Preview masking**.
OCR can miss whole regions or alter names, email addresses, numbers, and secrets.
Review both the recognized text and the masked result before sharing. A page
with no recognized text is reported; a wholly unreadable document is refused.
Veil cannot distinguish a blank page from a failed recognition of that page.

Cancelling the picker preserves the current preview. Failed imports preserve
the previous input and clear the output. Successful imports replace the input
and clear its undo history. Preview text and temporary mappings stay in memory;
**Clear** removes them. Use a saved CLI session when you need later restoration.
The **Import file… → PDF text…** option reads the selectable text layer without OCR;
see [PDF text import](pdf-text.md).

## CLI with saved mappings

For CLI-only installation, use `python -m pip install '.[ocr]'`.

```bash
veil mask --session scanned-notes --ocr scan.pdf > masked-notes.txt
veil mask --session photo-notes --ocr photo.png > masked-photo.txt
veil restore --session scanned-notes < copied-reply.txt > restored-reply.txt
```

`--ocr` replaces stdin for `mask` and cannot be combined with `--pdf` or
`--clipboard`. Configured detections, private registrations, local names, and
`--review` work through the existing masking workflow. stdout contains only
successfully masked text; counts and the OCR review reminder go to stderr.
Failures return no text, including when a later page fails. Shell redirection
can still create or truncate an output file before Veil starts; choose a new
output path.

Use one session label per conversation. The existing owner-only plaintext vault
stores detected original values for restoration; it does not store the source
image/PDF or a full OCR transcript. `veil forget --session scanned-notes` removes
that session's mappings. The global `--data-dir PATH` selects your usual private
settings and storage.

## Supported inputs and limits

- PDF, PNG, JPEG, TIFF, WebP, and BMP, identified by content. Multipage TIFFs and
  other supported multiframe images are scanned frame by frame.
- English printed text initially. Handwriting, unusual fonts, low resolution,
  skew, columns, tables, and complex layouts can be inaccurate or incomplete.
- Up to 20 MiB, 25 pages/frames, 20 megapixels per image or rendered page, and
  32768 recognized characters. PDFs render at 200 DPI. Exceeding a limit refuses
  the import instead of silently truncating it.
- A 90-second work budget with a 120-second outer process timeout. Tesseract has
  a maximum 10-second recognition timeout per page. These operation limits are
  not a parser security sandbox or a universal memory ceiling.
- Encrypted PDFs, including those with an empty password, are refused. Unlock a
  trusted copy locally first. Embedded files, metadata, and invisible content
  are not OCR inputs. No PDF scripts, links, or actions are executed.

The output is **plain text**, not a redacted image or PDF. This does not make
the original attachment safe to upload or automatically intercept attachments
sent through a gateway. [Local voice transcription](voice.md) is a separate
explicit input workflow. Visual PDF redaction remains roadmap work.

Rendering uses [pypdfium2](https://pypdfium2.readthedocs.io/en/stable/python_api.html);
recognition uses [Apple Vision](https://developer.apple.com/documentation/vision/vnrecognizetextrequest)
or [Tesseract](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html).
Fictional real-engine macOS tests cover PNG, scanned PDF, multipage TIFF, masking,
and exact restoration of the recognized text. Parser/preparation and refusal
tests also run without a native OCR engine. CI includes real-engine macOS and
Linux checks; native Windows OCR and independent recognition-quality evaluation
remain validation work.
The macOS file picker was also exercised with a fictional scanned PDF in the
desktop window: recognition populated editable text, and Preview masking
replaced its email with `[EMAIL_1]`. Local name recognition was off, so the
unregistered fictional name remained visible as expected.
