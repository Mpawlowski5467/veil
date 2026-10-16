# 1.0.0rc1 feature freeze and release evidence

As of 2026-10-15, **1.0.0rc1** is a local working-tree candidate. No stable
release, tag, registry publication, or external beta outcome is implied.
The published release remains 0.6.0b2. Freeze new features and provider adapters;
accept only fixes, packaging, documentation, and validation until the 1.0 decision.

## Frozen scope

- Existing masking/restoration, private registration, local verification, and
  supported Claude Code/Codex gateway routes.
- Desktop/menu-bar controls, optional login startup/recovery, and optional local
  spaCy name recognition.
- Explicit PDF text import, English OCR of scanned PDFs/images, and local English
  voice file/microphone workflows with review before sharing.
- OpenAI/Anthropic provider controls and the currently documented text routes.

Limits and unsupported shapes in the feature guides remain part of the scope.
No automatic attachment redaction, ordinary browser-chat interception, new
providers, extra languages, or new capture workflows enter this candidate.

## Candidate artifacts and local checks

The [Mac app guide](macos-app.md) describes build, installation, removal, and
signing limitations. Generated artifacts and sanitized test evidence live in
`dist/macos`; wheel/source distributions live in `dist` and are not published.
The manifest's source digest identifies packaged Veil contents, including the
pre-existing uncommitted implementation. Final publication must use an exact
reviewed commit and successful CI for that commit.

The first local RC pass covers Apple Silicon macOS 26.5.1, Python 3.14.6:

- Full non-live suite: **8,006 passed, 168 skipped, 72 live tests deselected**.
  Skips include opt-in/platform-specific and human/live checks; they are not passes.
- Native Tk and menu-bar regression suite: **47 passed**, including navigation,
  quiet polling, animation cancellation, resizing, and close behavior.
- Strict mypy and pyright: passed. Ruff lint and formatting: passed.
- Clean base-wheel install, optional desktop install, setup/undo, pinned skill,
  and packaged fictional beta checks: passed outside the checkout.
- Upgrade/rollback/return from repository tags 0.4.1, 0.5.0, and 0.6.0b1:
  passed with fictional persistent mappings, including historical beta secrets.
- Relocated standalone app: **11 checks passed** for internal links/signature,
  execution without system Python or the checkout, Unicode registration and
  restoration, PDF text, native Apple Vision image/scanned-PDF OCR, disposable
  Codex setup/undo, installed skill pinning/removal, gateway restart/stop,
  offline fictional speech, and direct restoration after removing/reinstalling
  the app. See `dist/macos/clean-install-report.json`.
- Disk image: compressed image verification, read-only mounting, Applications
  link, copy to `/Applications/Veil.app`, installed signature/CLI, and ejection
  passed. See `dist/macos/installation-report.json` and `SHA256SUMS`.
- The installed app opens with its native menus and branded window. Overview
  and preview rendering/navigation were inspected; automated native tests cover
  widget behavior. Screen-reader access and complete interactive human desktop
  journeys remain open below. No microphone, live provider, or login/reboot
  outcome is inferred from these checks.

### Native automatic-startup pass

The installed app passed **six real launchd checks** on this Mac, using a
temporary user LaunchAgent, an isolated port, and disposable fictional mappings:

- Register the login service and start the bundled gateway with no shell Python.
- Recover the gateway after crashing its verified live child process.
- Recover the supervisor through launchd without creating a duplicate gateway.
- Keep an explicit Stop in effect across supervisor relaunch.
- Resume with an explicit Start.
- Disable startup, remove the registered job/receipt, stop the gateway, and
  restore existing fictional placeholders successfully.

The temporary registration was removed and the normal startup setting remains
disabled. See `dist/macos/startup-report.json`. Run the same test with:

```bash
python packaging/macos/startup_smoke.py /Applications/Veil.app \
  --report dist/macos/startup-report.json
```

The separate startup/microphone/runtime regression selection passed **35 tests**;
its microphone tests use simulated devices and do not open a real input.
**Actual logout/reboot/login remains pending**. LaunchAgent bootstrapping and
crash recovery do not establish that the app survives a new user login session.
Microphone capture was deferred at the user's request during this pass.

The packaging pass fixed omitted audio codec modules and a speech-activity model.
A repeated relocated launch under load hit the old 10-second helper startup
limit; frozen helpers now have a bounded 30-second startup allowance. Existing
unbundled timeout cleanup and the new delayed-start regression both pass
(**47 service/runtime tests**). Final relocated checks pass with this fix.
Failed attempts were corrected; they are not counted as successful checks.

## Next human desktop pass

Use fictional material in the installed candidate. Record OS/display setup,
steps, and sanitized outcomes; do not send private text or vault contents.

1. Open from Applications, navigate all three views with mouse and keyboard,
   resize the window, toggle Reduce motion, close, and reopen.
2. Use the fictional preview example, mask it, edit the source, and verify stale
   output clears. Import a selectable PDF, a scan/image, and an audio file;
   confirm review text, a responsive loader, and clear failures for invalid files.
3. Exercise microphone permission denial, explicit recording, stop/transcribe,
   discard, and closing during capture. Confirm capture ends and nothing is sent.
4. Open menu-bar controls, start/stop/restart a disposable gateway, then quit
   the menu/window. Confirm the gateway remains governed by explicit controls.
5. Complete a real login/reboot/startup journey and a separate routed client
   verification, then disable startup and test documented removal/reinstallation.

These are pending human checks, separate from the completed automated suite.

## Remaining gates before public 1.0

- Developer ID signing, notarization/stapling, downloaded-file Gatekeeper journey,
  and third-party binary/license review. No signing identity is installed here.
- Real microphone capture/permission/deny/discard journeys; native file pickers,
  accessibility/VoiceOver, scaling, and extended desktop use by a human tester.
- Real logout/reboot and startup/recovery journeys on advertised platforms.
- Live OpenAI/Anthropic account tests and supported installed-client journeys,
  long sessions, updates, and failure recovery. Local fixtures do not cover these.
- Independent recognition-quality measurements, private-storage/security review,
  and new-user beta journeys across macOS, Linux, and native Windows.
- Advertise only tested Mac architectures/OS versions. Validate Intel/older macOS
  separately before extending the standalone Mac download's support claim.
- Review and commit the frozen scope, run cross-platform CI on that exact commit,
  fix release blockers, and obtain maintainer sign-off on immutable artifacts.

The [full release checklist](release-checklist.md) remains authoritative for
cross-platform and human gates. This first clean-install pass does not close them.
