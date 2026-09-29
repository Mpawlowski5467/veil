# Claude Code integration

[Back to the README](../README.md#start-with-the-veil-skill)

Install this checkout, and have Claude Code installed and authenticated. From the
project directory you want to work in, run:

```bash
veil claude
```

To resume a conversation through Veil, run `veil claude --resume` instead. The
launcher starts a private gateway on `127.0.0.1`, routes the new process through
it, and stops the gateway when Claude Code exits. Launching ordinary `claude`
separately does not give it this route.

Supported text in prompts, system instructions, file contents, and tool results
is masked. Replies are restored locally, including tool arguments, so file edits
can use real values. See [coverage and limits](../README.md#understand-the-boundaries).

The gateway rejects request fields it does not understand. Hooks check routing
before each prompt and inspect tool arguments for known real values. Shell calls
containing those values request approval; other outbound tools containing them
are refused unless explicitly allowed. These checks do not inspect every action
a shell command or external tool could perform.

Claude Code arguments are forwarded, except `--settings`, `--bare`, and
`--safe-mode`, which Veil refuses because they can bypass its configuration. An
existing `ANTHROPIC_BASE_URL` also prevents startup. `veil gateway` is available
for manually configured clients; `veil claude` manages its own gateway lifecycle.

The repository's live tests record compatibility checks against Claude Code
2.1.283. Re-run live tests after client updates; this document describes the
current checkout rather than pending compatibility changes on other branches.

The installed [Veil skill](assistant-skills.md) can guide setup and verify a test
request from inside the launched session. Use the same [data folder and detector
settings](configuration.md) when registering values. Exit and relaunch after
upgrading Veil or changing settings. Session mappings survive relaunches.
