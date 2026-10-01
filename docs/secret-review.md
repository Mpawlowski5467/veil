# Review uncertain secrets locally

**Source checkout feature after 0.5.0. The published 0.5.0 wheel does not include it.**

Veil can ask you about unfamiliar values before a model request leaves your
computer. Clear matches still use the normal [coding-secret masking](coding-secrets.md).
The extra review pass uses local context and token heuristics, with your decision
for ambiguous cases. It makes no model calls and never tests credentials against
a provider.

## Enable it for your gateway

Install the updated checkout, merge this setting into your existing private
`config.json` in the gateway's data folder (normally `~/.veil`), then restart
that gateway or relaunch `veil codex` / `veil claude`:

```json
{
  "secret_review": true
}
```

Keep your other settings. If you use `--data-dir`, edit that folder's config.
Review is off by default. Both the Anthropic and OpenAI adapters support it;
it applies only to requests actually routed through the updated gateway.

Try a **fictional** prompt in your client:

```text
Use "fictional four word phrase" to sign in. Help me describe this configuration.
```

An uncertain request returns a local 403 refusal with a review ID. **That request
has not been sent upstream.** In your own terminal, run:

```bash
veil review
```

This opens a private page on `127.0.0.1`. The default command finds your selected
Codex provider or an inherited Veil launcher environment. For a standalone
gateway, specify its address and data folder:

```bash
veil --data-dir /path/to/private/veil-data review --gateway-url http://127.0.0.1:8485
```

Use the address where your gateway actually listens (the Anthropic default is
8484). `--config /path/to/codex/config.toml` selects a non-default Codex config.
The command proves gateway identity before sending its local secret. It does
not print that secret or put it into the review page.

Open the page through `veil review`, not by opening `src/veil/review.html` from
disk: the HTML file alone has no server to fetch findings from. From a source
checkout, use `uv run veil review`. Keep the terminal running while reviewing.
Use **Refresh findings** inside the page; reloading the browser loses its
in-memory access token. If you reload, close the terminal command, or reach the
ten-minute limit, run the command again to open a new private page. If it reports
that gateway identity cannot be verified, check `veil status --json` and restore
your existing gateway using its original data folder and port first.

1. Reveal a value locally if needed. Values start collapsed.
2. Choose a credential or personal-data classification: **API key**, **Password or
   passphrase**, **Access token**, **Private key**, **Other credential**, **Person's
   name**, **Street address**, **Date of birth**, **Passport number**, or **Driver's
   license number**. Choose **Not private — allow this request** only to allow it.
3. Save every choice, then retry the original request in your client.

The page does not submit a model request. Leave your original text in the client;
Veil applies confirmed masks when you retry, even if the client changes metadata
in that session. If the client changes the request body, an earlier “Not private” choice does not authorize the changed request.
You may need to review it again. A terminal-only interface is also available:

```bash
veil review --terminal
```

Run review yourself, outside an assistant tool call. Neither a prompt saying
“ignore this value” nor a model's answer counts as a review decision. The terminal
command rejects redirected input/output. This is not a security boundary against
software already running as your OS user or holding your gateway credential.

## Review a text file before pasting into any chat

```bash
veil mask --session draft --review < prompt.txt > prompt.masked.txt
veil restore --session draft < reply.txt > reply.restored.txt
```

The prompt is read from the file; questions and values appear on your controlling
terminal, never in `prompt.masked.txt`. Blank input cancels. There is no output
or clipboard replacement if review is cancelled or fails. Your shell may still
create/truncate a redirected output file before Veil runs; use a new output path.
`veil mask --session draft --clipboard --review` works on clipboard text too.
Setting `secret_review: true` also requires review for local `veil mask` calls.
Use the same session when restoring a reply. For unattended scripts, use explicit
registrations and automatic rules, or handle the gateway's review refusal.

## What the extra pass checks

- Ambiguous prose such as `password is four fictional words here`. Clear quoted
  labels and delimited credential-like tokens now mask automatically.
- Credential command options and URL parameters whose values are not already
  fully covered by the automatic rules.
- Unquoted credential assignments with trailing words and unsupported block
  forms. Supported indented YAML credential blocks now mask automatically.
  An ambiguous unquoted phrase conservatively covers the remaining
  line; quote values to make their boundary clear.
- Long tokens with varied characters, including unknown provider formats. A
  random-looking hash can also trigger review; randomness does not prove secrecy.
- Sign-in instructions such as `Use "…" to sign in`, recovery/backup-code labels,
  and the explicit Polish `hasło to/jest …` and Spanish `contraseña es …` phrases
  when their values do not satisfy the automatic boundary checks.
- Labelled webhook endpoints and JSON payloads explicitly marked as base64 or
  base64url. The whole original endpoint or encoded value is offered for review;
  no decoded content is sent anywhere. Benign encoded data can also be flagged.
- Short token-like lines/fields when a supported field in the same request
  describes splitting a credential into fragments. This context is not reused
  for unrelated requests and is not a general code or language parser.
- Specific personal-data labels: title-cased multiword names after customer,
  patient, employee, or full-name labels; numeric street addresses after home,
  street, postal, or “lives at” labels; dates after birth-date labels; and
  alphanumeric numbers after passport or driver's-license labels. The suggested
  type is a question, not a verified identity or document-number check.

Both gateways now learn detected values across supported fields before their
final masking pass. A value mentioned early without a label can therefore be
masked when a later supported field identifies it as a credential. Review checks
original candidate boundaries, so masking a PII-looking substring inside a key
cannot silently dismiss the rest of that candidate.

## Decisions, storage, and limits

Unresolved findings prevent that request from being forwarded. Confirmed values
are masked and the request continues on retry. Confirmed mappings use the existing
session vault and can restore in supported replies. They are not registrations
for unrelated sessions; use [private registration](entities.md) for that.

Reviews and “Not private” decisions are held only in gateway memory and expire
after ten minutes; expired entries are discarded on the next queue operation.
Ignore decisions are scoped to the exact request bytes and session. The queue keeps at most 64 reviews,
100 distinct findings per request, and bounded value sizes. Expiry, eviction,
restart, or queue limits never authorize a request: resend and review again.
The review page closes access after ten minutes or when you stop its command.
Confirmed vault mappings have the normal retention rules; persistent vaults are
plaintext protected by filesystem permissions, not encryption.

The review page has no external scripts, fonts, analytics, or model connection.
A short-lived browser capability, host/origin checks, no-store responses, and a
restrictive content policy protect its local endpoints. It shows private values
only when you reveal them, but those values are present in browser memory. Keep
the page and its local URL private. Closing it does not revoke choices already
saved in the gateway.

**This cannot guarantee detection of every secret.** Short values without one of
the supported cues, unlabelled PII, computed/split values in other forms, unsupported
encodings and languages can still be missed. Quote ambiguous prose values to make
their boundary explicit, or register exact values. Review is off by default.
False positives are expected; this is a local heuristic review system, not a
semantic model that understands every sentence. Existing attachment, tool,
authentication-header and network-traffic boundaries are unchanged. See the
[threat model](threat-model.md).

[Regression tests](../tests/test_secret_review.py) cover request withholding,
full-value masking on retry, restoration, partial-mask cases, expiry, request
and session isolation, cancellation, and local web access checks using fictional
values and a loopback provider. These checks do not establish a universal recall
or precision score.
The [workflow evaluation](leak-evaluation.md) reports automatic coverage and
review holds separately, including false positives and simulated confirmation.
