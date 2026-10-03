# Security and privacy

Veil is pre-1.0 software. Its promise is limited to supported text in requests
that actually pass through its local masking boundary. It is not a sandbox,
a network firewall, an anonymizer, or a guarantee that every private value is
recognized. Read the [threat model](docs/threat-model.md) before relying on it.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/Mpawlowski5467/veil/security/advisories/new),
enabled for this repository on 2026-10-03. If private reporting is unavailable, open an
issue requesting a private contact channel without including exploit details or
sensitive data. There is no published response-time commitment yet.

Include the Veil version, OS, client/version, affected command or request shape,
and a minimal reproduction using invented values. Never attach authentication
files, gateway secrets, original vaults, transcripts, or raw model request logs.
An issue title such as “Potential masking bypass; private contact requested” is
enough to initiate contact. Do not send another person's data to demonstrate a bug.

## Supported fixes

Security fixes currently target the latest checkout and latest prerelease.
There is no LTS/backport promise. Report a possible leak within the advertised
boundary even if another detector happens to catch your sample.

## Review status

The repository contains regression tests and a maintainer-led privacy review.
Neither substitutes for an independent security assessment. Independent review
and resolution of release-blocking findings remain required before 1.0.
The [review handoff](docs/security-review.md) provides the pinned baseline,
reproduction commands, review map, known gaps, and finding/retest requirements.
