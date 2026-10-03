# OpenAI and Codex integration (experimental)

Veil has a Responses gateway, a managed Codex CLI launcher, and configuration for local Codex desktop tasks. The ordinary ChatGPT chat interface uses a separate [explicit text/clipboard workflow](chat-workflow.md).

| Interface | Implemented and verified |
| --- | --- |
| Codex CLI | Managed launcher; local fixtures and a live round trip using ChatGPT login passed. |
| Codex desktop | Backed-up setup/undo and local readiness checks. The bundled app-server passed isolated local/live tests; a basic text round trip was also manually verified in the desktop UI. Broader desktop workflows remain unverified. |
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

Veil starts a loopback gateway on a free port, runs standalone Codex with its provider pinned to the gateway, forwards the exit status, and closes the gateway on exit. It does not edit your Codex configuration. The local gateway secret is passed through an environment header setting, not exposed in process arguments. From another terminal, `veil review`, `veil verify` and `veil status --activity` find this launch; use the same `--data-dir` if you passed one.

The default `--auth chatgpt` uses Codex's existing sign-in. Veil does not read the credential file itself. Codex supplies authentication; the gateway forwards subscription requests to the Codex backend and API-key requests to `api.openai.com`. A mismatch between the client credential mode and gateway mode is refused.

Pass Codex options after `--`; choose a model with `--model` if needed. Provider/config/profile overrides, remote connections, hosted search, and feature overrides are refused by the launcher. It disables hosted web search, apps, multi-agent tools, and analytics for this invocation. Existing local tools and hooks can still have their own traffic outside the model request path.

OpenAI documents [custom providers](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers) and [using existing OpenAI authentication for a proxy](https://learn.chatgpt.com/docs/auth#alternative-model-providers).

## Codex desktop: setup and recovery

Install the optional desktop tools from the checkout:

```bash
python -m pip install '.[desktop]'
veil setup codex
```

The base library still has no runtime dependencies. The `desktop` extra adds
`tomlkit` to parse and edit TOML without discarding unrelated settings/comments,
including on Python 3.10. Development checkouts installed with `uv sync` also
include it; use `uv run veil ...` for the commands below.

Setup chooses `~/.codex/config.toml`, or `$CODEX_HOME/config.toml` when set.
`--config PATH` selects another configuration for setup, undo, status, or doctor.
`veil --data-dir PATH setup codex` selects the Veil storage/configuration folder.
Use the same data folder for the gateway. Defaults are port 8485 and ChatGPT
sign-in; `--port` and `--auth api-key` are available on setup.

Setup merges the provider and supported feature settings, creates owner-only
backup/receipt files next to the Codex configuration, then replaces the
configuration atomically. It does not start the gateway, restart Codex, change
your login, or modify detector registrations. A selected Codex profile is
refused because it could override the saved route. Repeated setup preserves
the original backup. Change an existing managed setup by undoing it first.

Start the background gateway with the command printed by setup (`veil start`
on macOS/Linux), restart Codex, and start a new local task. Then check readiness:

```bash
veil status
veil doctor
veil doctor --json
veil status --service
```

Use `veil stop` and `veil restart` to control the background worker. It stays
running after the terminal closes; login/reboot startup and supervised crash
restart are not installed. Lifecycle state now appears in status/doctor, without
implying active task coverage. See [background controls and recovery](background-gateway.md)
for port conflicts, explicit data folders, upgrades, and removal. On Windows,
continue using the foreground gateway shown below.

Status checks saved provider/feature settings, the gateway's cryptographic
identity proof, and authenticated API/auth-mode metadata. It probes only
`http://127.0.0.1` endpoints. Doctor adds private-storage checks, a local fictional
email round trip, data-folder/secret consistency, and CLI login or API-key
environment checks. It neither sends a model request nor prints credentials,
registered values, mappings, or raw client logs. An older gateway without the
metadata endpoint produces a restart warning, not a full readiness pass.

Exit status is 0 when every check passes, 1 when readiness needs attention, and
2 for command/setup errors. JSON output includes individual checks and remedies.
The setup receipt supplies the data folder automatically unless `--data-dir` is
explicit. For an older manual setup, provide its data folder to doctor.

These are **readiness checks**, not proof of an active task's route. Existing
tasks, CLI overrides, profiles, cloud tasks, and other client traffic may use a
different route. CLI login/key checks describe this shell; the desktop process
may have a different environment. The base URL and a reachable port alone are
not reported as proof of protection.

To collect evidence for a particular conversation, use `veil verify`, send its
fictional prompt in that conversation, then check the returned ID after the
reply. `veil status --activity` shows bounded metadata from the current gateway.
See [request verification and activity](verification.md) for scope and limits.

To undo setup:

```bash
veil undo codex
```

Undo restores only the fields managed by setup. Unrelated later edits are kept;
a conflicting edit to a managed field stops undo with an explanation. Backups
are retained privately. Restart Codex afterward. Undo restores the state just
before **that setup command**; it cannot reverse earlier manual configuration
changes, stop a separately running gateway, or erase session mappings. Preserve
any older manual backup if you want to return to a pre-Veil configuration.

If a crash leaves `config.toml.veil-setup.lock`, confirm no setup/undo process is
running before removing that lock. Keep the private backup and receipt. For an
interrupted setup, undo can restore the original settings before trying again.

### Manual configuration alternative

Run a persistent gateway in a terminal:

```bash
veil gateway --api openai --auth chatgpt --port 8485
```

The command writes an owner-only configuration fragment to `~/.veil/codex-provider.toml` (or the selected `--data-dir`) and prints the provider details. It contains the local access secret, so keep it private.

Merge the fragment into **user-level** `~/.codex/config.toml`, preserving unrelated settings. Root keys such as `model_provider` must appear before any table header. Merge existing `[features]` and `[model_providers.veil]` tables rather than adding duplicates. The fragment selects Veil, disables hosted search/apps/subagents, and configures HTTP/SSE with `requires_openai_auth = true`.

Restart the desktop app when ready, keep the gateway running, and start a fresh **local Codex** task using the Veil provider. This configuration is not applied to cloud tasks or ordinary ChatGPT chats. It affects shared local Codex settings. The gateway command only writes the fragment; use `veil setup codex` to opt into a backed-up merge. Neither command restarts the app.

The bundled runtime was exercised via its [app-server interface](https://learn.chatgpt.com/docs/app-server) in a temporary session. A subsequent manual desktop text test was matched to Veil's mappings and masked response ledger. These validate those text paths, not every desktop UI feature, plugin, attachment, or existing task.

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
- Only Responses and the model catalog are forwarded. Uploads, `/responses/compact`, and other endpoints are refused. A remote-compaction request receives a final HTTP 400 with `x-should-retry: false` and instructions to start a new protected chat. Its history is not forwarded.
- Stored conversation references and `previous_response_id` are refused. Replay history locally and start new conversations through Veil. Unseen encrypted state from imported conversations is refused.
- Local execution, MCP/app traffic, hooks, telemetry from other processes, cloud tasks, and clients selecting a different provider are outside the gateway.
- Detection and restoration have the [same limits as the rest of Veil](../README.md#understand-the-boundaries). Unknown placeholders can remain in gateway replies. No detection system proves that every secret was found.

Mappings and the ledger live in `~/.veil` by default. Original values are plaintext in the owner-only vault. Codex can also store restored values in local history. `veil forget --session ID` or `veil forget --all` removes Veil's mappings and ledger entries, not Codex history. After forgetting a session, its old encrypted reasoning is refused.

## Recover when a long chat needs compaction

Codex 0.160.0 with the Veil custom provider can summarize through ordinary
`/responses` calls. A scripted installed app-server test covers an initial
turn, this summary, and a continuation that replays the summary with private
values masked. That does not establish live long-session reliability or support
for the separate remote-compaction endpoint; routing can change with client or
provider configuration.

If Codex reports `veil: remote compaction is not supported`, keep the gateway
running and start a fresh chat through the same Veil provider. In the CLI use
`/new`; in the desktop app use **New chat** and keep Veil selected. A new chat
gets a separate conversation ID and fresh mappings. Files already edited in
your workspace remain available.

Continue with a short handoff you review locally: the task, decisions, changed
files, checks completed, and the next step. Enter it as ordinary text in the new
protected chat so Veil can mask it. Carry over only the context needed for that
step. Retrying `/compact`, resuming, or forking the full old history does not
remove this limit. The refusal does not delete your old Veil mappings or Codex
transcript.

Veil does not generate a replacement summary or synthesize encrypted context.
OpenAI's [compaction contract](https://developers.openai.com/api/docs/guides/compaction)
returns an opaque encrypted item and a canonical replacement context window;
safe handling of that lifecycle is still unsupported. This recovery starts a
new conversation, so it does not preserve every detail of the previous chat.
The CLI's [`/new` command](https://learn.chatgpt.com/docs/developer-commands#start-a-new-chat-with-new)
resets chat context while keeping the CLI session open.

## Validation

Local tests cover rejected requests, replay, session isolation, quoted JSON arguments, chunked streams, tool blocking, credential routing, model catalogs, configuration output, and remote-compaction refusal followed by a fresh protected conversation for both authentication routes. The opt-in app-server check covers the current custom-provider summary and continuation through ordinary Responses calls. These installed-client checks use fictional credentials and scripted loopback replies:

```bash
VEIL_LOCAL_CODEX=1 uv run pytest -q tests/test_codex_local.py tests/test_codex_desktop.py tests/test_compaction_recovery.py -m "not live"
```

Live checks are separate and make real model calls:

```bash
VEIL_LIVE_CODEX_CHATGPT=1 uv run pytest -q tests/test_codex_live.py -k chatgpt
VEIL_LIVE_OPENAI=1 uv run pytest -q tests/test_codex_live.py -k api-key
VEIL_LIVE_CODEX_APP=1 uv run pytest -q tests/test_codex_desktop.py -m live
```

`VEIL_CODEX_APP_BINARY` selects the desktop app's bundled `codex` executable; otherwise the app-server test uses `codex` from PATH. On 2026-10-03, CLI 0.160.0 and that executable's app-server runtime passed the ChatGPT-authenticated live tests with `gpt-6-luna`, including three app-server turns. API-key live testing is pending because no key was available. This is runtime evidence, not a manual desktop UI check or proof that a bundled desktop runtime matches the CLI. These checks do not establish compatibility with every model, client version, or desktop feature.
