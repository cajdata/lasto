# Security policy

Lasto talks to a vehicle's diagnostic port, so a bug that makes it send something it shouldn't is a safety problem. Please report those privately.

## Reporting a problem

Use GitHub's private vulnerability reporting: <https://github.com/cajdata/lasto/security/advisories/new>. Please don't open a public issue for a security problem. The same contact is in <https://lasto.dev/.well-known/security.txt>.

A report is most useful with:

1. The Lasto version or commit.
2. What you ran it against: the simulator or real hardware (which adapter).
3. What it sent or tried to send, and what you expected instead.

Lasto is a one-person project, so a reply may take a few days.

## What counts

- Any way to get Lasto to send something the rules on <https://lasto.dev/safety/> don't allow: a frame with a service on the never-list, a frame on a CAN ID outside the approved list, any frame at all in passive mode, or a command to the OBDLink MX+ outside its allowlist.
- Any way around the rate limits, the kill switch, the motion interlock, or the battery guard, including a polled session that opens or keeps sending after the kill switch trips.
- Any way to send a frame that doesn't pass through the gate and the one write function, or that skips the audit log.
- Any way to change the safety core's rules or timing while Lasto runs, or to open a serial port outside the OBDLink MX+ link, from code the structural tests don't flag.
- Any way to open real hardware without `--live` and an explicit channel or port.
- Problems with lasto.dev itself, like a page loading something from another server.

The Safety page says what the design does and doesn't claim: <https://lasto.dev/safety/#threat-model>. It guards against mistakes and shortcuts in Lasto's own code, and reports are rated against that.

## Supported versions

Lasto is pre-alpha. Only the latest commit on `main` gets fixes.
