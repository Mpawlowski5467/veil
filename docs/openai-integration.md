# OpenAI and Codex integration (experimental)

Veil has a Responses gateway, a managed Codex CLI launcher, and configuration for local Codex desktop tasks. The ordinary ChatGPT chat interface uses a separate [explicit text/clipboard workflow](chat-workflow.md).

| Interface | Implemented and verified |
| --- | --- |
| Codex CLI | Managed launcher; local fixtures and a live round trip using ChatGPT login passed. |
| Codex desktop | Private provider configuration; the bundled app-server passed local and live tests in an isolated ephemeral session. The active desktop UI was not reconfigured or restarted. |
| OpenAI Responses API | JSON/SSE adapter and API-key routing tested against local fixtures. Live API-key validation remains pending. |
| ChatGPT app / website | Explicit local mask/restore commands. No automatic interception of ordinary chats. |

## Codex CLI

Install this checkout with `python -m pip install .`. With Codex installed and signed in using ChatGPT:

```bash
veil codex
veil codex -- exec "Explain this project"
veil codex -- resume
```

For an OpenAI API key, set `OPENAI_API_KEY` in the launching shell, then use:

```bash
veil codex --auth api-key
```

Veil starts a loopback gateway on a free port, runs standalone Codex with its provider pinned to the gateway, forwards the exit status, and closes the gateway on exit. It does not edit your Codex configuration. The local gateway secret is passed through an environment header setting, not exposed in process arguments.

The default `--auth chatgpt` uses Codex's existing sign-in. Veil does not read the credential file itself. Codex supplies authentication; the gateway forwards subscription requests to the Codex backend and API-key requests to `api.openai.com`. A mismatch between the client credential mode and gateway mode is refused.

Pass Codex options after `--`; choose a model with `--model` if needed. Provider/config/profile overrides, remote connections, hosted search, and feature overrides are refused by the launcher. It disables hosted web search, apps, multi-agent tools, and analytics for this invocation. Existing local tools and hooks can still have their own traffic outside the model request path.

OpenAI documents [custom providers](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers) and [using existing OpenAI authentication for a proxy](https://learn.chatgpt.com/docs/auth#alternative-model-providers).

## Codex desktop: manual setup for local tasks

Run a persistent gateway in a terminal:

```bash
veil gateway --api openai --auth chatgpt --port 8485
```

The command writes an owner-only configuration fragment to `~/.veil/codex-provider.toml` (or the selected `--data-dir`) and prints the provider details. It contains the local access secret, so keep it private.

Merge the fragment into **user-level** `~/.codex/config.toml`, preserving unrelated settings. Root keys such as `model_provider` must appear before any table header. Merge existing `[features]` and `[model_providers.veil]` tables rather than adding duplicates. The fragment selects Veil, disables hosted search/apps/subagents, and configures HTTP/SSE with `requires_openai_auth = true`.

Restart the desktop app when ready, keep the gateway running, and start a fresh **local Codex** task using the Veil provider. This configuration is not applied to cloud tasks or ordinary ChatGPT chats. It affects shared local Codex settings, so Veil deliberately does not change them or restart an active app automatically.

The bundled runtime was exercised via its [app-server interface](https://learn.chatgpt.com/docs/app-server) in a temporary session. That validates the request/response path, not every desktop UI feature, plugin, attachment, or existing task. Full UI activation and verification remain a setup step.

For manual CLI use, the persistent gateway also prints an invocation selecting the provider explicitly. For an API-key desktop setup, use `--auth api-key` and ensure `OPENAI_API_KEY` is available to the desktop process, not only an unrelated terminal.

## Other Responses API clients

Start `veil gateway --api openai --auth api-key --port 8485`, then configure the client with:

- Base URL `http://127.0.0.1:8485/v1` and your normal API bearer token.
- `x-gateway-secret` containing the printed secret.
- `thread-id` containing a unique, stable conversation ID. Codex supplies this automatically; other clients must provide it.
- `store: false` and the complete locally replayed history in `input`.

Use a different `thread-id` for every conversation. The library remains independent of the provider; this HTTP adapter supports Responses, not Chat Completions.

## What is handled

| Surface | Behavior |
| --- | --- |
| Instructions, messages, visible reasoning, refusals | Mask outbound supported text; restore returned visible text. |
| Function/custom tool input and output | Mask outbound text. Buffer returned tool input until complete, restore exact placeholders, then check it before delivery. Function JSON is encoded again so restored quotes stay valid. |
| JSON and SSE replies | Restore visible text, including placeholders split across chunks. Malformed/incomplete streams fail with an error. Subscription SSE without a content-type header still goes through the strict parser. |
| History | Reuse the model's original masked text and tool inputs from the local ledger. |
| Encrypted reasoning | Replay only opaque values previously received through the same Veil session. The ciphertext is not inspected. |
| Metadata | Mask text and object keys; hash cache, safety, and user identifiers. |
| Headers | Forward selected authentication/protocol headers. Remove the gateway secret and local context headers. Do not advertise Codex's unsupported binary response format. |
| Model catalog | Allow `GET /v1/models` with an optional validated client-version query. The response is protocol configuration, not conversation text. |
| Storage | Always send `store: false`; this is not a guarantee about provider retention policy. |

Codex's `additional_tools` and namespaces containing local function/custom tools are supported. Tool definitions, descriptions, schemas, grammars, model names, protocol IDs, model catalogs, and authentication fields **are not scrubbed**. Keep personal data out of configuration surfaces.

Use the [normal entity/detector settings](../README.md#register-names-and-other-private-values). Names still need registration. `allow_mcp_tools` controls Claude's hook guard; it does not exempt OpenAI tool calls.

## Returned tool inputs

The OpenAI gateway refuses a returned tool input containing known private values unless the tool is exactly `apply_patch` or `functions.apply_patch`, the direct local patch tools. Shell commands, JavaScript wrappers, and MCP calls containing those values are refused. Ordinary calls without known private values can proceed.

This is a check before model-generated arguments reach the client, not a Codex execution hook, sandbox, routing monitor, or firewall. It cannot inspect a command's later file reads, subprocesses, network traffic, or an independently started tool. A generic code wrapper containing a private value is blocked even if its intended nested operation is a local edit; use a direct patch call for that edit.

## Other boundaries

- Images, files, audio, hosted tools, structured output formats, unknown fields, and unknown input items are refused. File text read by a local tool can be masked when it enters a supported tool-result request.
- Only Responses and the model catalog are forwarded. Uploads, `/responses/compact`, and other endpoints are refused. Long sessions that request remote compaction will stop with an error.
- Stored conversation references and `previous_response_id` are refused. Replay history locally and start new conversations through Veil. Unseen encrypted state from imported conversations is refused.
- Local execution, MCP/app traffic, hooks, telemetry from other processes, cloud tasks, and clients selecting a different provider are outside the gateway.
- Detection and restoration have the [same limits as the rest of Veil](../README.md#understand-the-boundaries). Unknown placeholders can remain in gateway replies. No detection system proves that every secret was found.

Mappings and the ledger live in `~/.veil` by default. Original values are plaintext in the owner-only vault. Codex can also store restored values in local history. `veil forget --session ID` or `veil forget --all` removes Veil's mappings and ledger entries, not Codex history. After forgetting a session, its old encrypted reasoning is refused.

## Validation

Local tests cover rejected requests, replay, session isolation, quoted JSON arguments, chunked streams, tool blocking, credential routing, model catalogs, and configuration output. Opt-in installed-client checks use fictional credentials and scripted loopback replies:

```bash
VEIL_LOCAL_CODEX=1 uv run pytest -q tests/test_codex_local.py tests/test_codex_desktop.py -m "not live"
```

Live checks are separate and make real model calls:

```bash
VEIL_LIVE_CODEX_CHATGPT=1 uv run pytest -q tests/test_codex_live.py -k chatgpt
VEIL_LIVE_OPENAI=1 uv run pytest -q tests/test_codex_live.py -k api-key
VEIL_LIVE_CODEX_APP=1 uv run pytest -q tests/test_codex_desktop.py -m live
```

`VEIL_CODEX_APP_BINARY` selects the desktop app's bundled `codex` executable; otherwise the app-server test uses `codex` from PATH. Verified clients: CLI 0.156.1 and desktop-bundled runtime 0.155.0-alpha.16. ChatGPT-authenticated live tests passed with `gpt-6-luna`. API-key live testing is pending because no key was available. These checks do not establish compatibility with every model, client version, or desktop feature.
