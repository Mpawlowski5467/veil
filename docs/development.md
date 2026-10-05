# Developing Veil

Run these commands from the repository root with Python 3.10 or newer and `uv`.

## Setup and local checks

```bash
uv sync --locked
uv run pytest -m "not live"
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
uv run pyright src
```

The test command excludes live provider tests. It covers the README and Python
guide examples, masking and restoration, streaming, overlaps, gateway requests
against local fixtures, and randomized inputs. It makes no model calls.
Dependency installation may download packages.

Live tests are skipped by default unless their opt-in environment variables are
set. Use `-m "not live"` for routine development, even if your shell has live-test
variables set. CI also checks native workflows and installed wheels; see
[the workflow](../.github/workflows/ci.yml).

## Optional live integration tests

Live tests use real clients and providers. They require the relevant installed,
authenticated client and consume its normal quota or API charges. Use fictional
data. The Claude harness uses a temporary workspace and isolated test settings;
its current launcher helpers target macOS/Linux.

To opt into Claude Code tests:

```bash
VEIL_LIVE_CLAUDE=1 uv run pytest -m live
```

The Claude tests use Haiku by default. Set `VEIL_LIVE_MODEL` to select another
model, or add pytest's `-k` option to select a smaller set of tests. Repeated
resume and file/resume exercises additionally require `VEIL_LIVE_SUSTAINED=1`
and `VEIL_LIVE_FILE_RESUME=1`, respectively.

Codex app-server live tests have a separate `VEIL_LIVE_CODEX_APP=1` opt-in; see
[their test file](../tests/test_codex_desktop.py). The `VEIL_LOCAL_CODEX=1`
app-server test uses a local fixture instead of a provider. Neither is a human
desktop UI journey. Record the client version and authentication route when
reporting live results; the [compatibility matrix](compatibility.md) separates
verified routes from work still pending.

## Claude Code census after a client update

The [census](../tests/live/census.py) compares what Claude Code sends with
[`tests/gateway_payloads/`](../tests/gateway_payloads/). It checks new request
shapes and failures such as gateway refusals, unmasked fixture values leaving
the gateway, or dropped reply fields. Harness failures, such as timeouts or
unexpected dialogs, are reported separately.

This is a separate live command: **it does not require `VEIL_LIVE_CLAUDE=1`**.
It needs an authenticated `claude` CLI and GNU screen at `/usr/bin/screen` on
macOS/Linux. A default run covers Haiku, Sonnet, Opus, and Fable, one scenario
at a time, and typically makes about **80–90 real model calls**. This is an
estimate, not a request or spending cap.

```bash
# Inspect a small plan without making model calls.
uv run python -m tests.live.census --models haiku --scenarios print --dry-run

# Run that one-model, one-scenario check with real model calls.
uv run python -m tests.live.census --models haiku --scenarios print

# Run the complete census; report without changing tracked fixtures.
uv run python -m tests.live.census

# Run and refresh tracked census data after a run without failures.
uv run python -m tests.live.census --update
```

`--models` selects models for the core `print`, `compact`, and `interactive`
scenarios. Extra scenarios have separate `--extras-on` and `--auto-on` model
selections, so `--models haiku` alone does not restrict the whole run to Haiku.
Use `--help` for the current options.

`--update` refreshes census files and eligible protocol words in
`src/veil/gateway/vocab.py`. Newly observed shapes need `--accept-new`; use it
only after reviewing how the gateway handles them, then run
`uv run pytest tests/test_gateway_request.py`. The tested Claude Code version
in `src/veil/gateway/compat.py` and the README advances only when `print`,
`compact`, and `interactive` are clean on all four models, with no unaccepted
new shapes. Review the generated diff before committing.

| Exit code | Meaning |
| --- | --- |
| `0` | Clean, with nothing new |
| `1` | New request or response shapes observed |
| `2` | Gateway or harness failure |
| `3` | Setup problem |
| `130` | Interrupted |

The run folder contains private captures, including full bodies, account
metadata, the gateway secret, and screen captures. Do not commit or attach it
to a public issue. A clean fresh run deletes it unless `--keep` is set; failed,
interrupted, resumed, and reanalyzed runs retain it.

Use `--from DIR` to reanalyze a retained run without new model calls.
`--resume DIR` makes live calls for scenarios that are missing or had harness
failures; it does not rerun every recorded gateway failure. Share a minimal
fictional reproduction or use [private security reporting](../SECURITY.md).
