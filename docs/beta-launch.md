# Veil beta invitation kit

These are drafts to post from your own account. Choose communities where you
participate, read their current self-promotion rules, and adapt the post to the
audience. No posts or direct messages have been sent as part of this kit.

## Community post

**Title:** I built Veil to mask detected personal data before AI calls — looking for beta testers

I'm building **Veil**, an open-source Python tool that replaces detected private
values with placeholders before an AI model call and restores the originals in
the reply locally.

For example, `jane.doe@example.com` becomes `[EMAIL_1]`. Names can be registered
explicitly. It includes a Claude Code gateway, an experimental Codex integration,
and Python/clipboard workflows for other providers.

Version **0.6.0b2 is a prerelease**, and I'm looking for **5–10 people** to try it on
macOS, Linux, or native Windows. The first exercise needs no API key and uses
fictional data. Its demo reply is simulated; there's a separate verification
step for a real client request.

I'd like to know: did installation work, was the protection scope clear, and
what would stop you from using it again? Happy to help troubleshoot setup here.

Detection has limits, persistent mappings are stored locally in plaintext with
private permissions, and the project still needs an independent security review.
Please use fictional data while testing.

- [First five minutes](https://github.com/Mpawlowski5467/veil/blob/main/docs/first-five-minutes.md)
- [Self-serve beta pack](https://github.com/Mpawlowski5467/veil/releases/tag/v0.6.0b2), with fictional files, local checks, recovery exercises, and a feedback worksheet
- [36-second demo video](https://github.com/Mpawlowski5467/veil/blob/main/docs/assets/veil-demo.mp4)
- [Repository and source](https://github.com/Mpawlowski5467/veil)
- [Feedback form](https://github.com/Mpawlowski5467/veil/issues/new?template=beta-feedback.yml)

## Short post for LinkedIn or a developer feed

I'm looking for a few beta testers for **Veil**, my open-source tool for masking
detected personal information before AI calls and restoring it locally afterward.

It works with Python and Claude Code, with experimental Codex support. Try a
local round trip in about five minutes, then optionally verify a real client
request. macOS, Linux, and Windows testers welcome.

0.6.0b2 is a prerelease: use fictional data, expect detection limits, and send me
the confusing parts of setup.

Start here: https://github.com/Mpawlowski5467/veil/blob/main/docs/first-five-minutes.md

Attach `docs/assets/veil-demo.mp4`. Its captions identify the reply as simulated.

## Personal invitation

Hey — I'm testing Veil, a tool I built to mask detected private text before AI
calls. Since you use Claude Code/Codex, would you be willing to try a five-minute
local demo with fictional data and tell me where setup is confusing? No API key
is needed for the first exercise. It's an early prerelease, and honest feedback
would help shape the next version.

https://github.com/Mpawlowski5467/veil/blob/main/docs/first-five-minutes.md

## A small first round

1. Invite 3–5 people individually, then share one relevant community post.
2. Aim for at least one macOS, Linux, and native Windows participant, with both
   Claude Code and Codex represented. CI results are not participant reports.
3. Ask participants to use the feedback form. A successful install and a real
   `verified` request are separate milestones.
4. Fix the most common setup friction before widening the invitation.
5. Record aggregate counts and anonymized findings in the beta checklist. Get
   permission before quoting a person or sharing their report elsewhere.

Good initial measures: invitations accepted, installs completed, local round
trips completed, real requests verified, time to first success, and blockers.
Stars and impressions are secondary. Use the [full beta protocol](release-checklist.md)
for deeper journeys after this first exercise.

The [beta results log](beta-results.md) starts with zero external participants.
The pack is ready for volunteers; preparing it and running it automatically are
not completed participant journeys. No invitations have been sent by this work.
