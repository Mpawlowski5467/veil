# Use Veil with ChatGPT and other chat apps

Use this explicit workflow when the chat app does not route its requests through Veil. It works with copied text from ChatGPT's app or website and other chat interfaces. It does not intercept the app, attachments, voice, or messages sent without masking first.

## Clipboard workflow

1. Register names and other private values in [Veil's configuration](../README.md#register-names-and-other-private-values). Pattern detection does not discover every name or secret.
2. Copy the prompt text. Choose one session label for this conversation and run `veil mask --session chatgpt-notes --clipboard`.
3. Paste the masked text into the chat and review it before sending. Ask the model to keep bracketed placeholders exactly as written.
4. Copy the reply and run `veil restore --session chatgpt-notes --clipboard` with the same label.
5. Paste the restored reply where you need it locally. Mask it again before including real values in another model prompt.

Each label has independent mappings. Use a fresh label for each conversation. The vault persists between invocations. The global `--data-dir` option selects an alternate private storage folder.

Warnings cause a nonzero exit and no output or clipboard replacement. An unknown placeholder or a placeholder absent from the selected session is held back. Veil cannot recognize a wrong session if it happens to contain the same placeholder; keep labels consistent.

Clipboard mode uses macOS's `pbpaste`/`pbcopy`, Windows PowerShell, or Linux `wl-paste`/`wl-copy` or `xclip`. Veil does not manage OS clipboard history or synchronization. Clipboard dispatch is tested with mocked subprocesses; native Windows/Linux clipboard behavior has not been validated here.

## Files and pipelines

Without `--clipboard`, the commands read stdin and write stdout, preserving trailing newlines:

```bash
veil mask --session chatgpt-notes < prompt.txt > masked-prompt.txt
veil restore --session chatgpt-notes < copied-reply.txt > restored-reply.txt
```

The shell creates or truncates redirected output files even if Veil refuses the input. Do not redirect over the original input file. Use `--exact` on `restore` to restore only exact placeholders when preparing text that will be executed or used as code.

Mappings contain original values and are stored locally in plaintext, with owner-only permissions. Remove one conversation's mappings with:

```bash
veil forget --session chatgpt-notes
```

This does not erase chat history, saved files, or clipboard history. Detection, surrounding-context exposure, and placeholder survival have the [same boundaries as the library](../README.md#understand-the-boundaries).
