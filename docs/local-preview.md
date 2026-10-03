# Local masking preview

Install the [0.6.0b2 beta](first-five-minutes.md), then run:

```bash
veil preview
```

Windows users can run `.\.venv\Scripts\veil.exe preview` from the installation
folder. In a source checkout, `uv run veil preview` works too. Use `--no-browser`
to print a one-time private URL instead of opening it automatically, then open
it once. Keep that terminal running; Ctrl-C closes the server, which otherwise
expires in ten minutes.

1. Click **Use fictional example**, or enter text and click **Preview masks**.
2. Inspect the masked output and each replacement's explanation.
3. Classify uncertain values under **Values to review**. They remain visible
   until classified. **Leave visible** is an explicit choice for this preview.
4. Expand **See exact local restoration** to check the round trip.
5. Clear the text when finished. Editing input resets all decisions and removes
   old results. Failed requests also clear old results.

The page sends input only to its authenticated server on `127.0.0.1`. It uses
the package's built-in detectors, local review heuristics, and a fresh memory
vault. No external classifier, model request, personal configuration, saved
registration, or persistent vault is involved. Input is limited to 32,768
characters. Neither the browser nor OS is controlled by Veil; keep the page and
its private URL to yourself.

This is a **preview**, not a protected route to a provider. It does not change
Codex, Claude Code, or ChatGPT settings, verify the current conversation, approve
held gateway requests, or persist learned mappings. The preview deliberately
uses built-in rules only, so a gateway with custom registrations can produce
different results. Detection can miss private values even with no review findings.
Use [request verification](verification.md) for routing evidence and
[`veil review`](secret-review.md) to decide actual held gateway requests.

If the page cannot connect, keep the terminal running or reopen `veil preview`
and use its new URL. The URL works once: the page exchanges it for a private
session when it starts, so reloading the page or opening the same URL again needs
a new `veil preview`. On Linux the page answers only connections from your own
OS account. Opening `preview.html` or `review.html` as a file cannot run
detection or fetch findings; the appropriate command must start the
authenticated local server.
