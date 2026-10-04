# Invite someone to try Veil

Copy the invitation below and send it yourself. No invitations have been sent
as part of preparing these materials.

## Ready-to-send invitation

Hi — would you try **Veil**, a tool I'm building to mask detected private text
before AI calls and restore it locally in the reply?

Start with a **10-minute local check** using fictional data. It needs Python
3.10 or newer, makes no model calls, and needs no API key. macOS, Linux, and
Windows are welcome.

If you have time, try the optional Claude Code or Codex exercise afterward.
That uses your signed-in client and its normal quota or API charges. Allow
another 20–30 minutes; the local check alone is useful feedback.

**[Start with the beta guide](https://github.com/Mpawlowski5467/veil/blob/main/beta/README.md)**
— it walks you through the
[0.6.0b2 release](https://github.com/Mpawlowski5467/veil/releases/tag/v0.6.0b2).
This is a prerelease with detection limits; please use fictional data only.
Independent security review is still pending.

Tell me what passed, what failed, what you didn't try, and whether you needed
help. You can use the
[short feedback form](https://github.com/Mpawlowski5467/veil/issues/new?template=beta-feedback.yml)
or send back your completed `feedback.md`. Setup problems and successful checks
are both useful.

Share only your feedback and, optionally, **`beta-check.json`**. Don't send the
used pack or workspace, `beta-data`, configuration files, vaults, credentials,
or transcripts. Report a possible security problem through the
[private security form](https://github.com/Mpawlowski5467/veil/security/advisories/new),
not a public issue.

## Short version

I'm looking for a few people to try **Veil 0.6.0b2**, a prerelease tool that
masks detected private text before AI calls and restores it locally afterward.

The first check takes about 10 minutes, uses fictional data, and makes no model
calls. An optional Claude Code or Codex exercise uses your normal client quota
or API charges. I'd like to hear what worked and where you got stuck.

[Beta guide](https://github.com/Mpawlowski5467/veil/blob/main/beta/README.md)
· [Pinned release](https://github.com/Mpawlowski5467/veil/releases/tag/v0.6.0b2)
· [Feedback](https://github.com/Mpawlowski5467/veil/issues/new?template=beta-feedback.yml)

Share only feedback and optional `beta-check.json`, never your used pack,
`beta-data`, configuration, vaults, credentials, or transcripts. Use the
[private form](https://github.com/Mpawlowski5467/veil/security/advisories/new)
for security findings.

## For the maintainer

- Send the revised guide pack if you have it, or link the current guide above.
  The original ZIP on the 0.6.0b2 release page still contains the original guide;
  updated instructions do not replace published release assets.
- Start with a few volunteers across macOS, Linux, and native Windows. Ask each
  person to try one client, if they choose the optional exercise.
- Record installation, local-check, and verified-client results separately in
  the [beta results log](beta-results.md). Automated checks are not participant
  reports; count only feedback actually received.
- Record time and help needed. Fix repeated setup problems before inviting more
  people. Get permission before quoting a tester or sharing their feedback.
- Keep longer recovery, upgrade, and removal exercises optional. Use the
  [release checklist](release-checklist.md) to track evidence still needed.
