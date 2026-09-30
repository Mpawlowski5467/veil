# Verify a client request and inspect activity

Readiness means a gateway is running and configured. Verification means Veil
observed a particular test request being masked, forwarded, and restored. It
does not guarantee that every request, every value, or other traffic is covered.

Verification is included in the 0.5.0 prerelease. Upgrade Veil and restart an
existing gateway to load it. Finish active requests before restarting. A gateway
from an older version reports that activity/verification support is unavailable.

## Run the test in your intended conversation

For a configured Codex desktop gateway, run this in a terminal:

```sh
veil verify
```

It registers a one-use challenge locally and prints a prompt containing a random
fictional email at `example.com`. Send that exact prompt as a new message in the
Codex or Claude Code conversation you want to check. It asks the model to repeat
the email without using tools. After the reply finishes, use the ID printed by
the first command:

```sh
veil verify --check VERIFICATION_ID
veil status --verification VERIFICATION_ID
```

You can also ask the installed skill using `$veil verify` in Codex or `/veil
verify` in Claude Code. The skill creates the prompt for you to send, then checks
the evidence when you ask afterward. It cannot insert a new user message itself.
Reinstall the skill after upgrading: `veil skill install`.

**Creating and checking a challenge make no model calls. Sending the generated
prompt through your client makes one normal model turn using its account.**
The verification command does not read provider tokens, sign you in, or submit
an independent API request that could be mistaken for your conversation.

### Select the right gateway

Tools inside sessions launched with `veil claude` or `veil codex` inherit the
gateway URL and secret privately. The verification CLI uses that endpoint.
Outside those launchers, it uses the selected Veil provider in Codex's user
configuration. `--config PATH` explicitly selects a different Codex config and
takes precedence over the inherited endpoint.

For a manually started gateway whose secret is saved in a data folder:

```sh
veil --data-dir /path/to/private/veil verify --gateway-url http://127.0.0.1:8484
veil --data-dir /path/to/private/veil verify --gateway-url http://127.0.0.1:8484 --check VERIFICATION_ID
```

Use the same endpoint for creation and checking. There is no secret command-line
argument. Veil proves the gateway's identity before sending its local secret and
refuses remote URLs, redirects, and browser requests. Private launchers use an
ephemeral secret, so check those gateways from inside the launched client rather
than guessing their port or using an unrelated saved data folder.

## Interpret the evidence

| State | Meaning |
| --- | --- |
| `pending` | The latest plain user message containing this challenge has not reached this gateway. It may have been sent elsewhere. |
| `in_progress` | The gateway observed the challenge and is processing its response. |
| `verified` | It masked the fictional email, forwarded that request, observed the placeholder restored in response text, and completed delivery of a successful response. |
| `incomplete` | One or more checks failed: for example no echo, a failed provider call, a truncated response, or no email masking. Diagnose the individual fields and create a new challenge. |
| `expired` | The challenge is over ten minutes old. Create another. |
| `unknown` | The ID belongs to another gateway, was evicted, or was lost on restart. Create another. |

Results expose the individual `masked`, `forwarded`, `restored`, and `completed`
checks, a timestamp, and an opaque session reference. The same reference appears
in activity reports. It identifies the client-provided session header within
this gateway instance; it is not a raw conversation ID. Do not assume any other
session in an activity list is your current task.

Only the latest plain user message can claim a challenge. Tool outputs, system
instructions, older messages, and token-count requests cannot. Once claimed, a
challenge cannot verify another session or be reused after an incomplete attempt.
If a client wraps the test in an unsupported form, it may stay pending; this is
not a pass. The test uses the gateway's actual detectors, including overrides;
it does not silently register the fictional email to force masking to work.

Evidence describes one completed exchange observed by Veil. It is not independent
provider attestation, proof that the UI rendered the reply, or a guarantee of
future routing. Clients control their session headers. A recent pass remains
historical evidence even if a later call takes a different route. Start a new
test after changing routing or restarting the gateway.

## Inspect recent activity

```sh
veil status --activity
veil status --activity --json
veil status --verification VERIFICATION_ID --json
```

Each session report has its last request time, request/forwarded/completed/failed
counts, recognized placeholder occurrences by type, and whether a verification
succeeded recently. Counts include repeated conversation history; they are not
unique people, newly detected values, or proof that every sensitive value was
found. Custom entity type names are grouped under `CUSTOM` rather than printed.

Only requests that successfully pass masking are tracked; refusals before that
stage, model catalogs, and token-count requests do not inflate these counters.
`forwarded` means the upstream HTTP request was written; failed provider calls
can still count as forwarded. `completed` requires a successful terminal response
and a finished relay. An in-flight request can have no completion count yet.

Normal `veil status` includes activity in its JSON report when supported, while
retaining its existing readiness meaning. `status --service` distinguishes worker
states such as stopped/crashed independently. A reachable gateway is ready; a
selected successful test supplies verification evidence; absent or expired test
evidence leaves that conversation unverified. Gateway unavailability is an error,
not a verified result. Activity checks do not edit routing or start workers.

Activity metadata stays in memory: at most 128 sessions active within the last
hour and 64 challenge records. Challenges/recent-verification indicators expire
after ten minutes. Restarting the gateway clears everything and rotates the salt
used for session references. No prompts, original values, raw session IDs,
headers, credentials, or custom type names are added to an activity log on disk.
The existing vault/ledger and client transcript storage remain separate; the
fictional test is processed as an ordinary conversation request there.

Creating a challenge exits 0. Checking a verified challenge exits 0; pending,
incomplete, expired, or unknown evidence exits 1. Command, configuration, and
connection errors exit 2. `--json` is available for successful local reports.

## Validation

Tests use local scripted provider replies to cover JSON and streaming responses,
real masking/restoration, failed or truncated replies, replay, concurrent claims,
session separation, expiration, bounded retention, and endpoint authentication.
Optional installed-client tests make no model calls:

```sh
VEIL_LOCAL_CODEX=1 VEIL_LOCAL_CLAUDE=1 uv run pytest -q tests/test_codex_local.py tests/test_codex_desktop.py tests/test_claude_local.py -m 'not live'
```
