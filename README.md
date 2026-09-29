# lasto

A read-only data logger for a 2006 Lexus GX470. It's pre-alpha; the [roadmap](https://lasto.dev/roadmap/) shows what's built.

Lasto is being built to record what the truck's modules say on the OBD-II port and, from Phase 4, to ask read-only diagnostic questions for data the truck may not broadcast. Passive mode puts a PEAK PCAN-USB interface in hardware listen-only mode and sends nothing. Polled mode will send only read-only requests, through allowlists, rate limits, and a kill switch. It never clears codes, resets modules, or writes to the truck.

- Site: https://lasto.dev
- What it will and won't send: https://lasto.dev/safety/
- Security problems: see [SECURITY.md](SECURITY.md)

Free software under GPL-3.0-or-later, with no warranty.
