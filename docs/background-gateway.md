# Run Veil in the background

On macOS and Linux, Veil can run its local gateway independently of a terminal.
This is useful for local Codex desktop tasks. The worker stays running when the
launching terminal closes. In the published 0.6.0b2 it does **not** start after
login or restart itself after a crash. The unreleased checkout adds opt-in
[login startup and recovery](#automatic-login-startup-unreleased). Windows users can continue using
`veil gateway` in a terminal; background controls are not supported there yet.

## Start from Codex setup

From the checkout, install the desktop extra and configure Codex once:

```bash
python -m pip install '.[desktop]'
veil setup codex
veil start
veil status
```

`start` uses the data folder recorded by setup and the port/auth mode in the
saved Codex provider. It verifies that the worker is serving before reporting
success. Restart Codex after its initial setup and start a fresh local task.
Subsequent gateway restarts do not require changing the provider settings.

For a custom Codex config, use `--config PATH` on setup, start, stop, restart,
and status. For an older manual setup, specify its data folder and matching
port/auth mode explicitly; Veil cannot infer that folder from the provider URL.

## Everyday controls

```bash
veil start                 # an identical running worker is reused
veil restart               # reload detector settings and the installed Veil code
veil stop                  # stop the managed worker
veil status --service      # inspect just the worker, without parsing Codex TOML
veil status --service --json
```

Finish active model calls before stop/restart: these commands can interrupt
requests in progress. A restart validates detector settings before stopping the
old worker. If the replacement cannot start, it reports failure and remains
stopped; it never silently routes requests around Veil.

`status` and `doctor` now include the worker state alongside the existing saved
routing and gateway checks. `--service` reports only the worker, with exit code
0 for a running, responding worker and 1 otherwise. It distinguishes running,
starting, stopping, stopped, crashed, failed, unresponsive, unknown, and unmanaged
states. `unmanaged` means there is no saved worker here; a foreground gateway may
still be running. None of these states proves that a particular task used Veil.

## Explicit settings and other API clients

The base package can manage a background worker without the desktop extra when
no Codex setup needs to be parsed:

```bash
veil --data-dir /absolute/private/veil-folder start --api openai --auth api-key --port 8485
veil --data-dir /absolute/private/veil-folder status --service
veil --data-dir /absolute/private/veil-folder restart
veil --data-dir /absolute/private/veil-folder stop
```

The data folder's parent must exist. The folder itself must be owner-only, as
with the foreground gateway. For Anthropic clients, select `--api anthropic`
(the default port for a first Anthropic start is 8484). Existing Claude client
settings/hooks still need to be configured as described in the
[Claude Code guide](claude-code.md);
`veil claude` continues to manage its own foreground gateway automatically.

Explicit port/API/auth options take precedence. Otherwise, a matching Codex
setup supplies the provider settings, followed by the last saved worker options,
then OpenAI/ChatGPT on port 8485. The working folder used for git identity is
saved across restarts; `--cwd PATH` changes it. This does not change your detector
registrations, Codex settings, sign-in, or provider credentials.

One worker is managed per data folder. A first OpenAI start also writes the
private `codex-provider.toml` fragment, as the foreground gateway does.

## Recovery, upgrades, and removal

- **Occupied port:** startup fails without stopping the process that owns it.
  Stop your existing foreground gateway yourself, or choose another port and
  update the client configuration to match. Veil never chooses a different port
  silently.
- **Crash or reboot without login startup:** `status --service` reports the stale running record as
  crashed. Run `veil start` or `veil restart`. A kernel lock, not a saved process
  ID, determines whether the old worker is still alive.
- **Unresponsive worker:** `stop` places a request for that specific worker. If it
  cannot finish within ten seconds, the request remains pending and the command
  fails. Veil does not kill a process based on its saved PID.
- **Upgrade:** finish active turns, upgrade Veil in its installed environment,
  then run `veil restart` from that environment. The existing worker keeps its
  loaded code until restarted. Keep an editable checkout and its virtual
  environment available while using it as the installed gateway.
- **Removal:** if enabled, run `veil startup disable` first. `veil stop --remove`
  stops the worker and removes saved lifecycle state. It does not unregister a
  login service. Inert lock files remain to keep
  concurrent commands safe. Mappings, secrets, and client configuration are kept.
  Use `veil undo codex` separately to reverse setup and `veil forget` separately
  to remove mappings; stopping the gateway alone leaves Codex pointing at it.

Control/state files are owner-only. They store lifecycle metadata such as the
port, working folder, process ID, and generation, not prompts or mappings.
Worker stdout/stderr are discarded; fixed failure reasons are reported through
status rather than retaining raw request logs. The existing vault and client
transcripts have the storage limits documented in the integration guide.

These controls are covered by real subprocess tests on macOS and Linux CI,
including detached launch, repeated/concurrent starts, stop/restart, crashes,
port conflicts, invalid state, and interrupted startup. No model calls are
needed for these checks. [Request verification and activity
counts](verification.md) now provide bounded evidence for individual client tests.

## Automatic login startup (unreleased)

Install this checkout into a persistent Python environment, complete setup or
start a gateway with your intended options, then opt in:

```bash
veil startup enable
veil startup status --json
veil status --service
veil stop                  # stays stopped, even while the supervisor runs
veil start                 # resumes; crash recovery is active again
veil startup disable       # unregisters startup and stops the gateway
```

Use the same global `--data-dir` and/or `--config PATH` as your gateway. One user
service is registered per data folder, under a name derived from that folder.
macOS uses `~/Library/LaunchAgents/org.veil.gateway.<id>.plist`; Linux uses
`~/.config/systemd/user/org.veil.gateway.<id>.service` and needs a running systemd
user manager. These start at user login, not before login; no administrator
service or Linux lingering is installed. Windows startup remains unsupported.

The OS keeps the supervisor running. It checks the existing kernel worker lease,
starts missing workers with saved port/API/auth/working-folder options, and
backs off up to 60 seconds after failures or short-lived crashes. It never kills
a process using a saved PID, silently changes a port, or bypasses masking.
The supervisor does not currently restart an unresponsive but still alive worker;
inspect diagnostics and use an explicit restart for that case.

An explicit Stop is remembered in the private `service-desired.json` file,
including across logins and supervisor restarts. `start`, `restart`, or explicitly
enabling startup requests running again. `stop --remove` keeps this stop intent;
only `startup disable` removes the login registration. Disabling startup also
stops the gateway; client settings, mappings, and registrations remain.

The private `startup.json` receipt records the exact registration hash. Removal
refuses changed files rather than deleting later user edits. Registration errors
retain recovery files; fix the user service manager and retry `startup disable`.
The pinned interpreter and checkout must remain available. Before moving or
removing an environment, disable startup there, install the replacement, and
enable it from the replacement. No credentials are copied into service files,
and worker output is discarded. Login services do not inherit your interactive
shell environment.

Real subprocess tests cover crash recovery and explicit stop/resume. A temporary
native macOS launchd bootstrap, ready gateway, intentional stop, and unload passed
locally. Actual logout/reboot, native Linux manager, and new-user journeys remain
release validation work. Enabling startup is separate from starting the menu icon;
run `veil tray` explicitly for menu-bar controls.
