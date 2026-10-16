# OpenAI and Anthropic providers (unreleased additions)

Veil's gateway uses your client's existing provider credentials. This checkout
adds a desktop provider selector and a text-only OpenAI Chat Completions route.
The published package remains 0.6.0b2. No provider SDK is required by Veil itself.

## Choose a mode

In **Settings → AI provider**, choose a mode and press **Apply & restart gateway**.
Changing the dropdown alone does nothing. Applying starts/restarts the managed
gateway and can interrupt requests. The card shows its client base URL. Configure
your client separately and start a fresh conversation when switching providers.
Only one managed provider mode runs per data folder. Separate data folders and
ports can run concurrently.

| Mode | Client and authentication | Model routes |
| --- | --- | --- |
| OpenAI · ChatGPT sign-in (Codex) | Codex supplies its existing ChatGPT sign-in | `/v1/responses` |
| OpenAI · API key | Your client supplies an OpenAI API key | `/v1/responses`, `/v1/chat/completions` |
| Anthropic · API key / Claude Code | Your API client supplies its Anthropic key, or launch Claude Code with its own credentials | `/v1/messages`, `/v1/messages/count_tokens` |

Disable login startup before changing modes, then enable it again to register the
new mode. Ordinary Start/Restart retains the saved Anthropic selection even if
this data folder has a Codex setup receipt. Existing OpenAI setup receipts still
provide their saved port and authentication defaults. Client configuration is
unchanged by the provider selector; gateway readiness does not prove chat routing.
Native Windows supports the foreground gateway; background controls remain
unsupported there.

Foreground terminal equivalents:

```bash
veil gateway --api openai --auth api-key --port 8485
veil gateway --api anthropic --port 8484
```

Run one at a time, or use separate data folders and ports. Long-lived gateways
require the local secret from `<data-dir>/gateway-secret` in the
`x-gateway-secret` header, in addition to your provider credential. Keep that
file private. API clients also need a stable conversation header: `thread-id`
for OpenAI, `x-claude-code-session-id` for Anthropic. Use a fresh, non-private
random ID for each conversation; never reuse an ID across unrelated chats.

For managed terminal sessions, use `veil codex --auth api-key`, `veil codex` for
ChatGPT sign-in, or `veil claude`. Their launchers configure these headers for
their own client. See [Codex](openai-integration.md) and [Claude Code](claude-code.md).

## API-client examples

Install the SDK you use separately (`python -m pip install openai` or
`python -m pip install anthropic`). Set your API key in your own launching shell.
The examples read Veil's secret locally and do not print or save keys. Replace
the model placeholder with a text model enabled for your account; Veil does not
choose a model or check account access.

OpenAI, with the API-key gateway running on port 8485:

```python
import os
import uuid
from pathlib import Path
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url="http://127.0.0.1:8485/v1",
    default_headers={
        "x-gateway-secret": (Path.home() / ".veil/gateway-secret").read_text().strip(),
        "thread-id": str(uuid.uuid4()),
    },
)
reply = client.chat.completions.create(
    model="YOUR_OPENAI_TEXT_MODEL",
    messages=[{"role": "user", "content": "Repeat jane@example.com exactly."}],
)
print(reply.choices[0].message.content)
```

Anthropic, with its gateway running on port 8484:

```python
import os
import uuid
from pathlib import Path
from anthropic import Anthropic

client = Anthropic(
    api_key=os.environ["ANTHROPIC_API_KEY"],
    base_url="http://127.0.0.1:8484",
    default_headers={
        "x-gateway-secret": (Path.home() / ".veil/gateway-secret").read_text().strip(),
        "x-claude-code-session-id": str(uuid.uuid4()),
    },
)
reply = client.messages.create(
    model="YOUR_ANTHROPIC_TEXT_MODEL",
    max_tokens=128,
    messages=[{"role": "user", "content": "Repeat jane@example.com exactly."}],
)
print(reply.content[0].text)
```

Keep the same client/conversation header while replaying a conversation's full
history. Replies contain restored values; Veil records their masked originals
locally so history returns to the provider with the same placeholders. The
owner-only plaintext vault and retention rules in [configuration](configuration.md)
still apply. Use `veil verify --gateway-url URL` and the same data folder to
create/check request-specific evidence; a provider reply alone is not evidence.

## Chat Completions scope

The new route follows the official [request API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
and [streaming format](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events).
It supports system/developer/user/assistant text, text-part arrays, refusal
replay, JSON or SSE replies, and one choice. Ordinary sampling/token limits and
text response format are accepted. Messages, stop text, and metadata are masked;
participant names and user/cache identifiers receive stable hash pseudonyms.
Storage is forced off. OpenAI authentication and protocol headers are allowlisted;
cookies, gateway secrets, and local conversation metadata are not forwarded.

Tools/function calls, hosted search, images/files/audio, log probabilities,
structured JSON output, unknown request fields, and non-text reply shapes are
refused. Use the existing Responses adapter for its supported local tool flows.
Imported PDF/OCR/voice text can be reviewed and masked separately; raw attachments
do not become protected by this route. ChatGPT sign-in mode refuses Chat
Completions before forwarding. Veil does not intercept ordinary ChatGPT or Claude
websites/apps.

SSE restoration handles placeholders split across chunks, supports usage trailers,
and requires a supported finish reason and `[DONE]`. Truncated/malformed replies
produce an error and no successful verification or completed-stream replay record.
Already delivered text cannot be withdrawn after a late error. Chat reply limits
are 16 MiB of JSON or 16,777,216 decoded SSE characters, with 8,388,608 text/event
characters. Provider-specific model/parameter validation remains upstream.

## Validation

Fictional local provider tests cover JSON/SSE restoration, refusal replay, private
header removal, authentication-mode refusal, review gating, session persistence,
and request evidence. Optional contract tests exercise synchronous, asynchronous,
and streaming clients from OpenAI SDK 3.31.0 and Anthropic SDK 1.15.0 against
loopback providers. Native Tk checks cover explicit selection and pending edits.
These are contract checks, not live API-key account validation; live account,
model-specific behavior, and full client journeys remain release work.

Anthropic's upstream request format is documented in its official
[Messages reference](https://platform.claude.com/docs/en/api/messages/create).
