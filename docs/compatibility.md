# Compatibility and validation

The tables include macOS live-client checks repeated on 2026-10-03 for the
published 0.6.0b2 baseline and the scripted checks described below. The bounded
live continuity checks were added and run in the unreleased source checkout
after b2; their results are identified separately. These checks do not claim
external beta evidence. **All three target
platforms must pass before 1.0**, including native Windows. “CI passed” means
scripted checks on a hosted runner; it does not mean a new user completed a
real client journey. See [beta gates](release-checklist.md).

## Operating systems

| Surface | macOS | Linux | Native Windows |
| --- | --- | --- | --- |
| Python masking/restoration and request/response regressions | Native CI, Python 3.10/3.14 | Full CI, Python 3.10–3.14; native journeys 3.10/3.14 | Native CI, Python 3.10/3.14 |
| Private data folder, persistent mappings, registration, forget | Tested | Tested | Native ACL and Unicode checks tested; hosted Windows runner uses an administrative account |
| `veil claude` / `veil codex` launchers and `--forget-after-run` | Native CI with a stub client | Native CI with a stub client | Native CI with a stub `.cmd` client; a real installed client journey is still required |
| Wheel + desktop extra, skill install/remove, setup/undo | Tested | Tested | Tested |
| Clipboard text, Unicode, empty text | Native `pbcopy`/`pbpaste` tested | Native X11 `xclip` tested; Wayland not yet validated | Native Windows PowerShell tested |
| Detached gateway `start`/`stop`/`restart` | Supported and tested | Supported and tested | Unsupported; use foreground `veil gateway` |
| Full installed-client/UI beta journey | Still required | Still required | Still required, including an ordinary non-administrative account |

The workflow source is [.github/workflows/ci.yml](../.github/workflows/ci.yml).
Native Windows tests cover over 5,000 selected protocol/core cases plus public
CLI/storage/wheel journeys. The launchers' SIGTERM handling is tested natively
on all three; SIGHUP and other Unix process-management tests remain in the full
Linux suite. Skipped cases are reported; a skip is never evidence of support.

## Clients and authentication

| Client or interface | Checked version / evidence | Supported scope and open work |
| --- | --- | --- |
| Claude Code | 2.1.286; 9 live Haiku masking, file-edit, tool-boundary, and resume checks passed on 2026-10-03, plus the five-run continuity exercise below; 1 model-specific effort check skipped. Recorded request census/golden fixtures remain from 2.1.283. | Supported text via `veil claude`; new client releases need the census and a fresh verification. Image/PDF contents pass through unmasked. |
| Codex CLI, ChatGPT sign-in | 0.160.0; live fictional email round trip with an outbound-body assertion passed on 2026-10-03 using `gpt-6-luna` | Experimental Responses adapter; text and supported local tools. |
| Codex app-server runtime | 0.160.0 executable; local scripted fixture, three live `gpt-6-luna` verification turns, and the six-turn continuity exercise below passed on 2026-10-03 | Exercises the runtime used by rich clients. This is not a manual desktop UI test or proof that every installed app build matches this executable. |
| OpenAI API-key route | Local request/response, header, error, and streaming regressions | **Live validation pending**; no API key was available for the readiness run. |
| Python callable integration | Clean installed-wheel round trip | Caller chooses the provider; async/structured flows require explicit mask/restore boundaries. |
| Ordinary ChatGPT app/browser or other providers | Local clipboard/stdin workflows | Explicit copied text only; no automatic interception or attachment protection. |

The [OpenAI integration guide](openai-integration.md) describes refusals and tool
limits. OpenAI hosted tools, media, and remote compaction remain refused. Remote
compaction gives a final error with a [fresh-chat recovery procedure](openai-integration.md#recover-when-a-long-chat-needs-compaction);
both authentication routes have scripted gateway recovery coverage.
Opt-in scripted and bounded live Codex 0.160.0 app-server checks cover its
custom-provider summary through ordinary Responses calls and subsequent masked
replay. This is distinct from remote-compaction support or a natural long session.
Claude compaction and replay have recorded/golden coverage; long natural sessions
and updates still need the beta scenarios. Do not translate fixture coverage
into “every client feature works.” The app-server lifecycle follows
[official OpenAI documentation](https://learn.chatgpt.com/docs/app-server).

## Unreleased: bounded live continuity checks, 2026-10-03

These automated macOS exercises used authenticated installed clients and real
provider replies, with fictional addresses. They are separately opt-in because
they consume more quota than a single round trip. They passed once on the
versions above; they are not human beta feedback, a desktop UI journey, a
context-window exhaustion test, or evidence for Linux/Windows installed clients.

- **Codex / ChatGPT sign-in / `gpt-6-luna`:** three turns recalled an address
  introduced only in the first prompt. Codex then generated its summary through
  the Veil provider. A new gateway instance reopened the existing SQLite vault
  and ledger, and three further turns recalled the same address without adding
  it to the prompts again. Every outbound model request was checked for masking.
  One scenario passed in 16.34 seconds.
- **Claude Code / Haiku / existing client sign-in:** five separate process runs
  continued one conversation through four resumes and five gateway instances.
  The prompts added, recalled, replaced, and recalled fictional contacts. The
  checks verified restored replies, unchanged masked history prefixes, and
  absence of the addresses in outbound requests. Tools were explicitly disabled
  for this text-only exercise. One scenario passed in 13.45 seconds.

API-key live validation remains pending: neither `OPENAI_API_KEY` nor
`ANTHROPIC_API_KEY` was available. Natural multi-hour work, interruption during
real tool use, manual desktop routing, and external testers still need separate
evidence.

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
VEIL_LIVE_SUSTAINED=1 VEIL_LIVE_CODEX_APP=1 VEIL_LIVE_CLAUDE=1 uv run pytest -q tests/test_codex_desktop.py tests/test_claude_live_masking.py -m live -k 'continuity or repeated_live_resumes'
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
