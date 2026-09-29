# Register private values

Names, organizations, addresses, and other values that patterns cannot recognize
can be registered locally. This feature is unreleased; from the checkout use
`uv run veil` in place of `veil`.

## Hidden terminal input

Run these in your own terminal:

```sh
veil entities add PERSON
veil entities add ORGANIZATION
veil entities list
veil entities list PERSON --json
veil entities remove PERSON
```

Add and remove prompt for one value with terminal echo disabled. The value is
not a command argument or shell-history entry. Veil refuses an input fallback
that would echo it. Ctrl-C cancels the prompt. List shows counts by type only;
it does not show values, git identity, automatic detections, or vault mappings.
The type must use upper-case letters, digits, and underscores, starting with a
letter; `LITERAL` is reserved.

## From a private file

For automation or an assistant, pass a local file containing one value directly
through stdin. Do not read its contents into the assistant conversation first.

```sh
veil entities add PERSON --stdin < /path/to/private-name.txt
veil entities remove PERSON --stdin < /path/to/private-name.txt
```

Use a non-sensitive filename. Keep the input file private yourself. Veil accepts
one non-blank UTF-8 value, at most 16,384 characters, optionally followed by a
newline. It rejects multiple lines and control characters. It preserves leading
and trailing spaces in the value. Do not put actual private values in a shell
literal, heredoc, command argument, or chat prompt.

## Matching and changes

Values match exact, case-sensitive spellings at token boundaries: registering
`Jan` does not mask `January` or `jan`. Register alternate spellings separately.
Scripts without spaces follow the library's [manual matching rules](python-guide.md#names-and-other-values-patterns-cant-find).
Adding the same value under the same type is a no-op. Adding it under a different
type is refused; remove its original registration before reclassifying it.
Remove uses the same exact spelling; a missing registration is a no-op.

Use the **same data directory as your gateway or mask command**:

```sh
veil --data-dir /path/to/private-data entities add PERSON
veil --data-dir /path/to/private-data entities list
```

The default is `~/.veil/config.json`. Edits preserve other settings, including
custom patterns, git identity, retention, and metadata, but reformat the JSON.
New folders are mode 0700 and successful edits write mode 0600 files. Files
contain the originals in **plaintext**, not encrypted storage.

Restart an already-running gateway or relaunch `veil codex` / `veil claude`
after changes. New mask commands load the new settings. Python applications use
`Shield.add_entity` explicitly; constructing a plain Shield does not read this
CLI config. Removing a registration does **not** delete stored mappings or client
history, disable built-in detection, or remove a matching git identity entry.

Edits refuse invalid settings, linked config files, shared data directories, or
an overlapping registration edit. Failed writes preserve the previous config.
If a process is killed during an edit, confirm that no entity edit is running
before removing `.veil-entities.lock` in the data directory. An interrupted edit
may also leave an owner-only `.veil-config-*` temporary file containing private
settings; inspect or remove it locally. Avoid editing config.json in another
program at the same time.

