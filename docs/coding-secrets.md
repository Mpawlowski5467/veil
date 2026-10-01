# Mask coding secrets

**Available in the source checkout after 0.5.0; the published 0.5.0 wheel does
not include these rules.** Install the updated checkout and restart existing
gateways or relaunch clients to use them.

Veil replaces supported coding secrets with placeholders and continues the
request. It restores them locally in supported replies using the same session's
mappings. No credential is tested against a provider, and no remote secret
scanner is used. The base package still has no runtime dependencies.

```text
Before: API_KEY="fictional-example-key"
After:  API_KEY="[API_KEY_1]"

Before: password: "fictional example passphrase"
After:  password: "[PASSWORD_1]"
```

## What is recognized

| Type | Supported evidence |
| --- | --- |
| `API_KEY` | Supported key prefixes for OpenAI/Anthropic (`sk-` family), Google (`AIza`), AWS access-key IDs (`AKIA`, `ASIA`), and Stripe secret/restricted keys. Explicit fields such as `API_KEY`, `apiKey`, `x-api-key`, `AWS_SECRET_ACCESS_KEY`, `client_secret`, and `signing_secret`. |
| `TOKEN` | Supported GitHub, GitLab, Slack, npm, and Hugging Face prefixes; three-part JWT-shaped strings with a JSON header containing `alg`; Bearer values and valid Basic user/password encodings in text; explicit token fields such as `access_token`, `refreshToken`, and `session_token`. |
| `PASSWORD` | Explicit fields ending in `password`, `passwd`, `passphrase`, `pwd`, or `pass`, including `DB_PASSWORD` and `dbPass`. |
| `PRIVATE_KEY` | PEM-style PRIVATE KEY blocks, including RSA, EC, DSA, OpenSSH, and encrypted private keys; explicit private-key fields. An unfinished block is masked through the end of the input. |
| `CREDENTIAL` | The `username:password` portion of supported PostgreSQL, MySQL/MariaDB, MongoDB, Redis, AMQP, and HTTP(S) URLs. Percent-encoded spelling is preserved. |

Prefix checks require plausible lengths and character sets. They do not validate
issuance, permissions, expiry, JWT signatures, or cryptographic key material.
Public keys and certificates are not private-key matches.

Credential assignments support `.env`-style text, quoted JSON/YAML keys, and
common code assignment syntax. Quoted values retain spaces, Unicode, newlines,
and escapes. **Quote complex passwords.** Unquoted detection reads a single
token, not a general programming-language or YAML parser. Bare identifiers can
be ambiguous: an assignment such as `password=value` is treated as a credential.

Environment references such as `${API_KEY}`, `$API_KEY`, `%API_KEY%`, and common
`os.environ`/`process.env` expressions are left as references. Identifiers such
as `max_tokens`, `token_count`, `password_length`, and `api_key_file` are not
credential fields. Arbitrary hashes and long random-looking strings are not
masked just for having high entropy.

## Where it works

The default rules apply to `Shield`, `veil mask`, and supported model-bound text
in both gateways, including source/configuration text returned by tools. Parsed
tool-input and data fields keep their credential-name context. The normal
gateway restrictions on opaque data and returned tool calls still apply.

In Python, retain a parsed string's field name explicitly:

```python
from veil import Shield

shield = Shield(redact_warnings=True)
masked = shield.mask("fictional weak password", field_name="password")
assert masked.text == "[PASSWORD_1]"
assert not masked.warnings
assert shield.restore(masked.text).text == "fictional weak password"
```

Use one shield/session for masking and restoration. Ordinary `mask(text)` finds
the labels inside text itself. `field_name` helps when your own JSON parser has
already separated the name from its value. Custom detectors that only implement
`detect(text)` continue to work.

A numeric credential in a parsed tool/data object cannot become a string
placeholder without changing its type. The gateways refuse such inputs rather
than forwarding the number or silently changing the tool schema. A number inside
an ordinary text assignment, such as `password=1234`, can be masked normally.

## Ask about uncertain values

Enable [local secret review](secret-review.md) to review prose credentials,
ambiguous multiword values, and unfamiliar token-like strings before sending.
The gateway holds uncertain requests until you classify each finding locally.
Automatic masks still continue normally; no remote classifier receives the input.

## Unknown formats and custom rules

Register an exact private value locally with `veil entities add TOKEN` or
`veil entities add API_KEY`. Type it into the hidden terminal prompt; do not
paste it into an assistant conversation. See [registration](entities.md).

Custom patterns using one of the new type names replace that type's built-in
rules, including field-name detection. `RegexDetector(include_builtins=False)`
keeps all built-ins disabled. Review overrides carefully. Existing vault
placeholders keep their type; upgrading does not rewrite historical mappings.

## Limits and storage

This is format-based detection, not a guarantee that every secret is found.
Unlabelled passwords, unsupported providers, obfuscated/encoded values,
concatenated/interpolated expressions, and fragments across separate text blocks
can be missed. Use explicit registrations for values outside the supported rules.
Matches still reveal their type and repeated use; surrounding code can be sensitive.

**Restoration requires the original secret.** Persistent Veil vaults store it in
plaintext with private filesystem permissions/Windows ACLs, just like other
mapped values. Local replies, client transcripts, tool outputs, and backups can
contain restored secrets. Temporary launcher sessions and forgetting have the
limits described in the [threat model](threat-model.md).

These rules mask credentials appearing in supported request content. They do
not alter the provider's real authentication headers, scan every local file,
intercept arbitrary tool network traffic, revoke a leaked key, or expand the
gateway's attachment/tool-definition coverage. See [boundaries](../README.md#understand-the-boundaries).

## Validation

[The fictional regression fixtures](../tests/test_secrets.py) exercise supported
prefixes and labels, code/reference negatives, quoted/escaped/multiline values,
private-key blocks, URL userinfo, streamed restoration, custom overrides,
persistent sessions, parsed tool arguments, and real local HTTP gateway round
trips with scripted provider replies. Assertions check the bytes received by the
fake provider and the restored response; no live keys or external calls are used.

These authored cases establish regression coverage, not a general accuracy
percentage. The [30-document detection baseline](detection-results.md) records
the earlier published 0.5.0 behavior; it does not measure these new secret rules.
