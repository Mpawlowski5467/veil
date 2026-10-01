# Compatibility and validation

The tables retain live-client evidence recorded 2026-09-29 for 0.5.0.
The 0.6.0b1 candidate adds the scripted checks described below; it does not
claim new live-provider or external beta evidence. **All three target
platforms must pass before 1.0**, including native Windows. “CI passed” means
scripted checks on a hosted runner; it does not mean a new user completed a
real client journey. See [beta gates](release-checklist.md).

## Operating systems

| Surface | macOS | Linux | Native Windows |
| --- | --- | --- | --- |
| Python masking/restoration and request/response regressions | Native CI, Python 3.10/3.14 | Full CI, Python 3.10–3.14; native journeys 3.10/3.14 | Native CI, Python 3.10/3.14 |
| Private data folder, persistent mappings, registration, forget | Tested | Tested | Native ACL and Unicode checks tested; hosted Windows runner uses an administrative account |
| Wheel + desktop extra, skill install/remove, setup/undo | Tested | Tested | Tested |
| Clipboard text, Unicode, empty text | Native `pbcopy`/`pbpaste` tested | Native X11 `xclip` tested; Wayland not yet validated | Native Windows PowerShell tested |
| Detached gateway `start`/`stop`/`restart` | Supported and tested | Supported and tested | Unsupported; use foreground `veil gateway` |
| Full installed-client/UI beta journey | Still required | Still required | Still required, including an ordinary non-administrative account |

The workflow source is [.github/workflows/ci.yml](../.github/workflows/ci.yml).
Native Windows tests cover over 5,000 selected protocol/core cases plus public
CLI/storage/wheel journeys. Unix process-management tests remain in the full
Linux suite. Skipped cases are reported; a skip is never evidence of support.

## Clients and authentication

| Client or interface | Checked version / evidence | Supported scope and open work |
| --- | --- | --- |
| Claude Code | 2.1.283; live Haiku masking, file-edit, tool-boundary, and resume checks; recorded request census/golden fixtures | Supported text via `veil claude`; new client releases need the census and a fresh verification. Image/PDF contents pass through unmasked. |
| Codex CLI, ChatGPT sign-in | 0.156.1; live fictional email round trip with an outbound-body assertion | Experimental Responses adapter; text and supported local tools. |
| Codex app-server runtime | 0.156.1 executable; local scripted fixture plus three live turns with outbound masking and verification | Exercises the runtime used by rich clients. This is not a manual desktop UI test or proof that every installed app build matches this executable. |
| OpenAI API-key route | Local request/response, header, error, and streaming regressions | **Live validation pending**; no API key was available for the readiness run. |
| Python callable integration | Clean installed-wheel round trip | Caller chooses the provider; async/structured flows require explicit mask/restore boundaries. |
| Ordinary ChatGPT app/browser or other providers | Local clipboard/stdin workflows | Explicit copied text only; no automatic interception or attachment protection. |

The [OpenAI integration guide](openai-integration.md) describes refusals and tool
limits. OpenAI hosted tools, media, and remote compaction remain refused.
Claude compaction and replay have recorded/golden coverage; long natural sessions
and updates still need the beta scenarios. Do not translate fixture coverage
into “every client feature works.” The app-server lifecycle follows
[official OpenAI documentation](https://learn.chatgpt.com/docs/app-server).

## 0.6.0b1 scripted recovery and local interface checks

Both adapters now run 36-turn histories across three gateway instances, forced
session-cache eviction, 32 concurrent sessions, cancelled clients, rate-limit
retry, incomplete-stream recovery, and review decisions after restart. The
stress tests exposed and fixed premature session eviction and silent Anthropic
stream termination. These use loopback fictional providers, not a live account.
Native CI runs them on macOS/Linux/Windows with Python 3.10 and 3.14.

Preview and support-report tests cover authenticated local access, unsafe-origin
refusal, bounded input, strict output allowlists, exact restoration, and recovery.
The shipped browser scripts have dependency-free Node checks. A manual macOS
in-app-browser check exercised the fictional preview, classification, and exact
restoration. This is a local preview UI check, not a Codex desktop routing test.

## Reproduce

```bash
uv sync --locked
uv run pytest -q -m 'not live'
VEIL_LOCAL_CODEX=1 VEIL_LOCAL_CLAUDE=1 uv run pytest -q -m 'not live'
VEIL_LIVE_CODEX_CHATGPT=1 VEIL_LIVE_CODEX_APP=1 VEIL_LIVE_CLAUDE=1 uv run pytest -q tests/test_codex_live.py tests/test_codex_desktop.py tests/test_claude_live_masking.py
```

These POSIX examples use installed, authenticated clients. On PowerShell set the
same flags with `$env:FLAG = '1'` before running Python/uv. Live tests use provider
quota. Set `VEIL_LIVE_OPENAI=1` and provide `OPENAI_API_KEY` privately to include
the API-key test; never paste a key into an issue or command argument. The native
clipboard CI flag is for isolated CI desktops because it replaces the clipboard.

For updates, run the request census described in the README, compare its new
fields and privacy failures, and rerun verification and relevant live checks.
Keep client/version/model/auth/OS details with results. For upgrade/rollback use
[the installed-wheel procedure](upgrading.md), not a checkout-only import test.
