# OpenAI and Codex integration (experimental)

Veil can serve a supported subset of the OpenAI Responses API through a local gateway. The first integration target is **Codex CLI with an OpenAI API key**. It is an experimental manual setup, available from this checkout; there is no `veil codex` launcher yet.

The request path is:

```text
Codex CLI / your API client
        ↓ text with real values
Veil on 127.0.0.1 → mask → OpenAI /v1/responses
        ↑ restore              ↓ placeholders in replies
        └──────────────────────┘
```

The Python library remains independent of the provider. This adapter is specifically for Responses, not Chat Completions.

## Start the gateway

Install the checkout with `python -m pip install .`, then run:

```bash
veil gateway --api openai --port 8485
```

Veil listens only on loopback. It prints a provider table containing the gateway URL and a local access secret. Merge that table into your **user-level** `~/.codex/config.toml`; replace an existing `model_providers.veil` table rather than adding a duplicate. Keep the file private. Veil does not edit your Codex configuration automatically.

The printed table looks like this, with a real gateway secret in place of the example:

```toml
[model_providers.veil]
name = "Veil (experimental)"
base_url = "http://127.0.0.1:8485/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
supports_websockets = false
http_headers = { "x-gateway-secret" = "COPY_THE_PRINTED_SECRET" }
```

Make your API key available as `OPENAI_API_KEY` in the shell that launches Codex. Keep it out of repository files. The gateway forwards the client's bearer credential to `api.openai.com`; it does not read your ChatGPT login or choose an account.

Keep the gateway running and start a **new** Codex conversation in another terminal:

```bash
codex --no-daemon -c 'model_provider="veil"' -c 'web_search="disabled"'
```

Choose a model available to your API account with Codex's `--model` option if needed. The explicit provider override selects Veil for this invocation. Standalone HTTP/SSE is required; WebSockets and hosted web search are unsupported. The gateway refuses unsupported capabilities instead of forwarding their request bodies.

See OpenAI's [custom provider configuration](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers) and [provider authentication](https://learn.chatgpt.com/docs/auth#alternative-model-providers) for Codex settings. Do not add `requires_openai_auth = true` to this setup: that selects a different authentication path, which this integration has not implemented.

## Other Responses API clients

Use `http://127.0.0.1:8485/v1` as the client's base URL and send your API bearer token as usual. Add `x-gateway-secret` with the printed secret and a `thread-id` header containing a unique, stable ID for that conversation. Codex supplies `thread-id` automatically; Python scripts and other clients must provide it themselves. Use a different ID for each conversation so their mappings stay separate.

Send `store: false` and replay the complete local conversation in `input`. Both JSON and SSE replies are supported. SDK operations targeting other endpoints, including Chat Completions, are not supported.

## What is handled

| Surface | Behavior |
| --- | --- |
| Instructions and text messages | Mask detected and registered values before forwarding. |
| Local function/custom tool input and output | Mask outbound text; restore complete returned tool input with exact placeholders. Function JSON is parsed and encoded again so restored quotes remain valid JSON. |
| Visible reasoning summaries and refusals | Restore returned text and reuse the masked version in subsequent history. |
| JSON and SSE responses | Restore visible text, including placeholders split across network chunks. Tool deltas are buffered until the complete input is available. |
| Conversation replay | Reuse the model's original masked text and tool inputs from the local ledger. |
| Encrypted reasoning | Replay only opaque values previously received through the same Veil session; reject unseen values from other sessions. The ciphertext itself is not inspected. |
| Request metadata | Mask text and object keys; hash cache, safety, and user identifiers. |
| Headers | Forward a limited set of authentication and protocol headers. Remove local thread IDs, gateway credentials, client paths, and other client metadata headers. |
| Storage | Require `store` to be false or omitted; always forward `store: false`. This is an API request setting, not a guarantee about provider retention policy. |

The adapter accepts Codex's `additional_tools` input item and namespaces containing local function/custom tools. Tool definitions, descriptions, schemas, grammars, model names, protocol identifiers, and authentication headers are configuration, and **are not scrubbed**. Keep personal data out of those surfaces.

Use the same [entity and detector configuration](../README.md#register-names-and-other-private-values) as the Anthropic gateway. Names still need registration. The `allow_mcp_tools` setting controls Claude's hook guard and has no effect on this Codex setup.

## Boundaries

- Images, file attachments, audio, hosted tools, structured output formats, unknown fields, and unknown input item types are refused. Text read from a file by a local tool can be masked when that tool result enters a supported request.
- Only `POST /v1/responses` is forwarded. Query parameters, `/responses/compact`, file uploads, and other endpoints are refused. Long sessions that request remote compaction will stop with an error.
- `previous_response_id` and server-managed `conversation` are refused. Clients must replay history locally. Start a new session through Veil; imported unprotected history may contain opaque state that Veil cannot verify.
- This does **not** inspect or block local tool execution, shell network access, MCP traffic, app traffic, telemetry, or traffic sent directly to a different provider. Restored tool calls contain real values. The routing and tool guards used by `veil claude` have not been ported to Codex.
- ChatGPT subscription authentication, Codex desktop/cloud workflows, and the ChatGPT website/app are not integrated or validated by this setup. This is an API-key CLI integration. A custom provider does not turn the ChatGPT chat interface into a client of this gateway.
- Detection and restoration have the same [limits as the rest of Veil](../README.md#understand-the-boundaries). Unknown placeholders can remain in output; masking does not prove that every secret was detected.

The vault and ledger use `~/.veil` by default, with owner-only permissions and plaintext original values in the vault. Codex can also store restored values in its own local history. `veil forget --session ID` or `veil forget --all` removes Veil's mappings and ledger entries, not Codex history. Replaying encrypted reasoning after forgetting a session is refused.

## Validation and next steps

Unit and local HTTP tests cover request rejection, masked history, persistent session isolation, JSON tool arguments containing quotes, chunked streaming, incomplete streams, and header filtering. An opt-in smoke test runs **Codex CLI 0.156.1** against loopback fixtures, checking that a synthetic email is absent upstream and restored in Codex's output:

```bash
VEIL_LOCAL_CODEX=1 uv run pytest -q tests/test_codex_local.py
```

That test uses fictional credentials and scripted replies. It makes no live model call and does not establish compatibility with every model, tool, Codex release, or OpenAI backend. Live API validation remains outstanding.

The next integration milestones are a managed `veil codex` launcher with routing checks, an appropriate Codex tool execution guard, and separate investigation of ChatGPT-authenticated routing. ChatGPT browser use would need its own explicit masking/restoration workflow or a separately designed client integration.
