# Mask coding secrets

**Included in the 0.6.0b1 beta; the older 0.5.0 wheel lacks these rules.**
Upgrade Veil and restart existing
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

Before: apiKey := "fictional-orchard-42"
After:  apiKey := "[API_KEY_1]"
```

## What is recognized

| Type | Supported evidence |
| --- | --- |
| `API_KEY` | Supported key prefixes for OpenAI/Anthropic (`sk-` family), Google (`AIza`), AWS access-key IDs (`AKIA`, `ASIA`), and Stripe secret/restricted keys. Explicit fields such as `API_KEY`, `apiKey`, `x-api-key`, `AWS_SECRET_ACCESS_KEY`, `client_secret`, and `signing_secret`, and run-together names such as `OPENAIAPIKEY` and `awssecretkey`. |
| `TOKEN` | Supported GitHub, GitLab, Slack, npm, and Hugging Face prefixes; three-part JWT-shaped strings with a JSON header containing `alg`; Bearer values and valid Basic user/password encodings in text; explicit token fields such as `access_token`, `refreshToken`, and `session_token`; run-together names with a credential qualifier such as `APITOKEN`, `GITHUBTOKEN`, and `authtoken` (an unqualified ending such as `nexttoken` or `ERRORTOKEN` is not a credential); supported auth/session cookie values in pasted HTTP headers and signature query parameters. |
| `PASSWORD` | Explicit fields whose last word is `password`, `passwd`, `passphrase`, `pwd`, or `pass`, including `DB_PASSWORD`, `dbPass`, and `MYSQL_PWD`, and run-together names ending in `password` or `passphrase`, such as `PGPASSWORD` and `dbpassword`. Run-together endings that also finish ordinary words (`bypass`, `OLDPWD`, `htpasswd`) need a separator, so `adminpass` and `dbpasswd` are not recognized. |
| `PRIVATE_KEY` | PEM-style PRIVATE KEY blocks, including RSA, EC, DSA, OpenSSH, and encrypted private keys; explicit private-key fields, including run-together `sshprivatekey`. An unfinished block is masked through the end of the input. |
| `CREDENTIAL` | The `username:password` portion of supported PostgreSQL, MySQL/MariaDB, MongoDB, Redis, AMQP, and HTTP(S) URLs. Percent-encoded spelling is preserved. |

Prefix checks require plausible lengths and character sets. They do not validate
issuance, permissions, expiry, JWT signatures, or cryptographic key material.
Public keys and certificates are not private-key matches.

Credential assignments support `.env`-style text, quoted JSON/YAML keys, and
common code assignment syntax. This includes Go `:=` and `apiKey string = …`,
and type annotations such as Python `API_KEY: Final = …` and `password: str = …`,
TypeScript `const token: string = …`, Kotlin/Swift `val apiKey: String = …`,
Rust `let api_key: &str = …`, and Pascal `Password : String := …`. The type
stays visible; only the value becomes a placeholder. A declaration without a
value, such as `password: str`, is unchanged. Quoted values retain spaces,
Unicode, newlines, and escapes. **Quote complex passwords.** An unquoted value
after `:` or a spaced `=` (YAML, INI, or a label such as `Password: …`) is
masked through the end of its clause: the end of the line, an inline ` #` or
` //` comment, or `,` `;` `.` `!` `?` followed by a space. When the label opens
a quoted string, as in `curl -H "X-Api-Key: …" https://…`, the value also ends
at that string's closing quote. A label at the end of a prompt string, such as
`input("Password: ")` or `read -p "Password: " pw`, has no value and stays
readable. A compact `NAME=value` reads one shell word, as
in command prefixes like `DB_PASSWORD=… ./run`. Quote multiword `.env` values.
This is not a general programming-language or YAML parser. Bare identifiers can
be ambiguous: an assignment such as `password=value` is treated as a credential.

PHP, Ruby, and Perl `=>` pairs are recognized when the value is a quoted
literal, such as `'password' => '…'`, `:api_key => "…"`, `(password => '…')`,
and Perl named arguments such as `-password => '…'`. A bare or dash-prefixed
Perl key also needs the literal to end the item (a comma, closing bracket,
comment, or line end). An unfinished quoted value is masked through the end
of the input, as for other quoted assignments. Variables, calls, and constants
after `=>` (`$password`, `env('DB_PASSWORD')`, `ENV['DB_PASSWORD']`) stay
readable, and so do JavaScript arrow functions such as `token => token.trim()`.

Stringified JSON, meaning a JSON document stored inside a JSON string, is also
recognized. Examples are an API Gateway/Lambda event `body`, a HAR
`postData.text`, or a logged `json.dumps` payload. The key and a string value
must use matching escaped quotes (`{\"password\": \"…\"}`), at one to four
levels of escaping. Only the value's escaped source spelling is replaced and
restored, so the outer JSON stays valid. Numeric or bare values inside
stringified JSON, and other encodings such as base64 or form-encoded bodies,
are outside this rule.

Additional automatic rules recognize:

- Explicit prose labels such as `My password is "fictional meadow phrase"`,
  `API key equals "…"`, and `refresh token was "…"`. Complete quotes delimit
  multiword values. Unquoted values require a token with digits or supported
  separators and a clear clause ending; ordinary explanations remain uncertain.
- The specific Polish `hasło/haslo to/jest …` and Spanish `contraseña/contrasena
  es …` password phrases, with the same value-boundary checks. This is not
  general multilingual understanding.
- `recovery code:` and `backup code:` followed by a quoted value or a delimited
  token, using `CREDENTIAL` placeholders.
- Supported long command options such as `--password`, `--api-key`,
  `--access-token`, and `--client-secret`, with a space or `=`. One shell word
  includes quotes, escaped spaces, and adjacent quoted fragments. Environment
  references and standalone command substitutions remain references; nothing is
  executed. Incomplete words still need review.
- Credential fields using indented YAML `|` or `>` block values, including
  chomping/indent indicators and header comments. The whole raw block, including
  its content indentation, becomes one placeholder; sibling fields stay visible.
  This protects text and restores its exact spelling, but the masked block is
  not necessarily executable YAML. Do not run masked configuration as code.

Removed diff lines are included. Simple quoted concatenations such as
`password = "first part" + "second part"` mask each literal separately without
evaluating code. JSON field names may use Unicode escapes, such as
`"pass\u0077ord"`; the original key spelling stays intact.

In HTTP(S)/`otpauth` URL query strings, credential names are percent-decoded once
for classification. Values retain their original encoding and neighboring
parameters stay visible. Signature names include `sig`, `signature`,
`X-Amz-Signature`, and `X-Goog-Signature`. This recognizes syntax, not whether
a link grants access. Unknown/doubly encoded names and other URL components
are outside this rule.

Pasted `Cookie:` / `Set-Cookie:` headers recognize `sid`, `session`, `sessionid`,
`session_id`, `session-id`, `connect.sid`, `JSESSIONID`, `PHPSESSID`, `auth`,
`auth_token`, `access_token`, `refresh_token`, `csrftoken`, `xsrf-token`, and
`csrf_token`, including `__Host-` / `__Secure-` prefixes. Other cookie names and
attributes are unchanged. These rules inspect text supplied to the model; they
do not read a browser cookie store or rewrite actual transport headers.

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

Enable [local secret review](secret-review.md) to review ambiguous prose credentials,
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
Unlabelled passwords, unsupported providers/encodings, interpolated or computed
expressions, and fragments without a recognized context cue can still be missed.
Optional review covers additional [specific context cues](secret-review.md), but
does not infer every secret. Use explicit registrations outside supported rules.
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
prefixes and labels, code/reference negatives,
quoted/escaped/stringified-JSON/multiline values, private-key blocks, URL
userinfo, streamed restoration, custom overrides, persistent sessions, parsed
tool arguments, and real local HTTP gateway round trips with scripted provider
replies. Assertions check the bytes received by the
fake provider and the restored response; no live keys or external calls are used.
The [automatic syntax tests](../tests/test_secret_automatic.py) add prose and
language variants, shell quoting, YAML boundaries, references, custom overrides,
and HTTP masking/restoration with review both enabled and disabled. The
[declaration tests](../tests/test_secret_declarations.py) cover typed Python,
TypeScript, Go, Kotlin, Swift, Rust, and Pascal declarations, parameter lists
that must stay readable, and clause-bounded unquoted values.

These authored cases establish regression coverage, not a general accuracy
percentage. The [30-document detection baseline](detection-results.md) records
the earlier published 0.5.0 behavior; it does not measure these new secret rules.
The [52-document workflow evaluation](leak-evaluation.md) measures current automatic
masking and review separately and records their remaining limitations.

## Code references and uncertain prose

Unquoted credential properties rooted at `settings`, `config`, `self`, or `this`
are treated as code references, for example `password = settings.database_password`.
Quoted lookalikes such as `password = "settings.database_password"` are still
masked, as are weak bare values such as `PASSWORD=letmein`. This is a syntax
heuristic; explicitly register a real credential that looks like a code reference.
Parsed JSON credential strings remain protected regardless of spelling.
Ordinary instructions such as “Store API keys in a password manager” stay readable.
“The password is stored in the operating system keychain” is ambiguous and still
requires review when review is enabled; it could be a literal passphrase.
