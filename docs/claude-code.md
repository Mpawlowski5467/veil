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

New text fields and block types are masked generically. Keys, types, numbers,
unrecognized file-byte fields, and opaque values that cannot safely be masked
are refused. Known image/PDF attachment contents pass through unmasked.
Model routes accept no query or exactly `?beta=true`, the query used by the
recorded client. Other queries, fragments, and absolute-form request targets are
refused before reading or forwarding the body. A refusal does not echo the
unsupported target. This route restriction is a post-b2 checkout improvement.
Provider-origin blocks can be replayed unchanged, unless they contain registered
private text. Hooks check routing
before each prompt and inspect tool arguments for known real values. Shell calls
containing those values request approval; other outbound tools containing them
are refused unless explicitly allowed. These checks do not inspect every action
a shell command or external tool could perform.

Claude Code arguments are forwarded, except `--settings`, `--bare`, and
`--safe-mode`, which Veil refuses because they can bypass its configuration. An
existing `ANTHROPIC_BASE_URL` also prevents startup. `veil gateway` is available
for manually configured clients; `veil claude` manages its own gateway lifecycle.

The recorded request census and golden fixtures cover Claude Code 2.1.283.
Selected checks against newer clients are listed separately in the
[compatibility matrix](compatibility.md). Re-run live tests after client updates; this document describes the
current checkout. Another version produces a one-line startup warning once per
client/Veil version pair; it does not prevent startup.

The installed [Veil skill](assistant-skills.md) can guide setup and verify a test
request from inside the launched session. Use the same [data folder and detector
settings](configuration.md) when registering values. Exit and relaunch after
upgrading Veil or changing settings. Session mappings survive relaunches.

From another terminal, `veil review`, `veil verify` and `veil status --activity`
find this launch; use the same `--data-dir` if you passed one.

## When a request is refused

A request the gateway cannot mask is not sent. For example:

```
API Error: 400 veil: can't mask this request, so nothing was sent. This is Claude Code 2.1.290, and veil 0.6.0b2 was tested with 2.1.283: update veil. If it happens on every prompt, it is in the conversation: /rewind to before the prompt that brought it in, or start a new one. Not handled: messages[4].content[1].attestation.signature (opaque data that can't be masked)
```

Update Veil when Claude Code is newer than the tested version. If a refused
block is already in history, use `/rewind` to return before it or start a new
conversation. A tool definition or setting sent with every request needs a
Veil update; rewinding cannot remove it. Background refusals are summarized
when Claude Code exits without printing request contents.

Privacy refusals and gateway failures are final. Features Claude can drop,
such as an effort setting or auto-mode safety check, may be retried without that
feature. Temporary storage contention and unreachable upstream APIs can retry.
On models that retain earlier thinking, adding a registration before resuming
may cost one rejected request; Claude then drops that thinking and continues
without its prompt cache for that turn.
