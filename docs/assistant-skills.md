# Use Veil from Codex and Claude Code

Veil includes a local assistant skill that calls the installed Python package.
It can check readiness, guide setup, and mask or restore a text file. Installing
the skill does **not** enable gateway routing or protect the prompt invoking it.

For the first run, follow the [README's install → setup → verify walkthrough](../README.md#start-with-the-veil-skill).
This guide covers installation options and everyday file workflows in more detail.

## Install

From a checkout containing this feature:

```sh
python -m pip install '.[desktop]'
veil skill install
```

Or use `uv run veil skill install` in the checkout. The `desktop` extra is needed
for Codex configuration editing; skill installation and mask/restore work with
the base package. This feature is unreleased; the published version may not
include it yet.

The installer creates a personal `veil` skill in `~/.agents/skills` for Codex and
`~/.claude/skills` for Claude Code. Install just one with `veil skill install
codex` or `veil skill install claude`. For a custom client configuration or a
project-scoped installation, choose its skill directory explicitly:

```sh
veil skill install claude --skills-dir /path/to/project/.claude/skills
veil skill install codex --skills-dir /path/to/project/.agents/skills
```

The default locations are personal directories; `CODEX_HOME` and custom Claude
configuration directories do not change those defaults. Use `--skills-dir` for
those layouts. The installed runtime reference records the absolute Python
interpreter from installation, so the skill works outside the Veil checkout
without `veil` on PATH. Keep that environment available. A checkout installation
still depends on that checkout and its virtual environment.

## Invoke

In **Claude Code**, try:

```text
/veil status
/veil help me set up Veil for Claude Code
/veil mask /path/to/draft.txt into /path/to/draft.masked.txt using session email-demo
```

In **Codex desktop**, select **Veil** from the skills picker, then send:

```text
Help me set up Veil for Codex desktop.
```

In **Codex CLI**, use a skill mention:

```text
$veil status
$veil help me start a Codex CLI session through Veil
$veil mask /path/to/draft.txt into /path/to/draft.masked.txt using session email-demo
```

Codex CLI also exposes `/skills`; `/veil` is the Claude Code form. Send these
phrases in the client's chat input, not your shell. They are assistant requests,
not a rigid command parser. The assistant
uses the skill's instructions to select Veil's CLI commands. If the skill does
not appear, start a fresh client session. See the official
[Codex skill documentation](https://learn.chatgpt.com/docs/build-skills#how-codex-uses-skills) and
[Claude Code skill documentation](https://code.claude.com/docs/en/skills).

For Codex desktop, the skill can run backed-up setup, start the gateway, and
check readiness. You still need to restart Codex and open a fresh local task
after changing its configuration. For Claude Code or Codex CLI, it gives you the
`veil claude` or `veil codex` launch command for a fresh terminal session. It
cannot switch the route of the current conversation. See the
[Codex integration guide](openai-integration.md) and
[background gateway controls](background-gateway.md).

`status` checks relevant readiness and recent activity. Neither alone proves
that this conversation used Veil. Select Veil in Codex desktop and ask it to verify,
or use `$veil verify` in Codex CLI or `/veil verify` in Claude Code to create
a fictional test prompt, send it in the intended conversation, then ask the
skill to check its verification ID. A pass supplies evidence for that particular
request and opaque session reference. See [verification and activity](verification.md).

## Keep file contents local

Give the skill **file paths**, not private text pasted after `/veil` or `$veil`.
It directs the source file into Veil locally without first reading it into the
conversation. The output is a new file, and existing outputs are preserved.
Use the same data folder and session label to restore a reply later:

```text
/veil restore /path/to/reply.masked.txt into /path/to/reply.txt using session email-demo
```

Restored contents stay in the output file; open it yourself in your local editor.
The skill does not print them into tool results. Clipboard operations are also
available when explicitly requested.

These are instructions an assistant follows, not a deterministic execution
boundary. The gateway is the mechanism for automatic masking of supported
model requests. Email, phone, and supported US SSN detection are built in; names
need registration. Compact or space-separated SSNs need an explicit label, and
impossible SSN ranges are rejected. Undetected values can remain in output. Neither
the skill nor the gateway is a sandbox for every local tool or network action.

## Register private values

Ask the skill to register a value **from a local file path**, or ask for the
`veil entities add PERSON` command to run yourself with a hidden terminal prompt.
Do not paste the value into chat. `veil entities list --json` reports counts
without exposing the values. Removal uses `veil entities remove PERSON` with
the same private input options. Changes apply after restarting the relevant
gateway or relaunching the client; removal keeps existing conversation mappings.
See [registration and storage](entities.md).

## Update or remove

Rerun `veil skill install` after upgrading or moving the installed Python
environment. Unchanged installations are reused; Veil updates its own unmodified
files. It refuses to replace an existing custom skill, symlink, edited file, or
directory containing additional files. Preserve those changes and move the
folder aside before installing again.

```sh
veil skill uninstall          # both clients
veil skill uninstall claude   # just Claude Code
```

For a custom location, pass the same client and `--skills-dir` used to install.
Removal only deletes an unchanged Veil skill. It does not change client routing,
stop a gateway, uninstall the Python package, or delete mappings.

Install/update uses a directory swap and a lock in the skill's parent directory.
An ordinary failed swap restores the old skill. If a process is killed during
an update, preserve any `.veil-skill-previous-*` folder; it may contain the prior
installation. Confirm no installer is still running before removing a stale
`.veil-skill.lock` or recovering the previous directory.
