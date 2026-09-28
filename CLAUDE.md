# lasto

Local, read-only data logger for a 2006 Lexus GX470. It records CAN traffic from the truck's OBD-II port, polls for data that isn't broadcast, and decodes it into signals, reports, and exports. It never changes anything on the truck. Safety is the first requirement, not a feature.

- Full project spec: `PROMPT.md` (local only, gitignored). Read it at the start of a session.
- Architecture and file layout: `docs/architecture.md`.

## How we work

- Work in the phases below. At the end of each phase, stop, summarize what you built, show test results, and wait for my approval before starting the next phase.
- You never run anything against the real vehicle, a real CAN interface, or a real serial port. All development and testing uses the simulator. When a phase needs a live test, give me the exact command. I run it myself at the truck and paste the output back.
- I run you in normal permission mode.
- After Phase 1 is approved, ask me before changing anything in the safety core, and explain why the change is needed.
- Don't delegate safety core work, or anything that could touch live hardware, to subagents. They don't inherit this project's hooks, permission rules, or CLAUDE.md.
- Use git. Commit at the end of each phase. Safety core changes get their own commits with a clear message.
- If a requirement conflicts with making something work, the safety rule wins. Tell me and propose an alternative.
- Ask clarifying questions in Phase 0 instead of guessing. I install drivers and hardware myself.

## Safety core (non-negotiable)

The safety core is every code path that can put bytes on the wire, for both the PCAN and STN transports. It is the only code allowed to call a transmit function, and it enforces these rules in code:

1. **Passive mode cannot transmit.** Open the PCAN channel in hardware listen-only mode and read the setting back through the API to confirm it before capturing. If it cannot be confirmed, refuse to run. The passive code path contains no transmit call at all; enforce that structurally and with a test.
2. **CAN ID allowlist.** In polled mode, transmit only ISO-TP diagnostic frames on diagnostic request IDs: the functional ID 0x7DF and physical request IDs confirmed from Creader captures or approved discovery results. Never transmit on any other ID, and never on any ID seen carrying broadcast traffic. ISO-TP flow control frames are allowed only on the request ID, only in response to an ECU's first frame.
3. **Service allowlist, default deny.** Parse the reassembled ISO-TP payload (accounting for extended addressing, where the service byte follows the address byte) and check the service against an explicit read-only allowlist. Anything else is rejected before it reaches the interface, logged, and raised as an error.
   - Allowed OBD-II services: 0x01, 0x02, 0x03, 0x06, 0x07, 0x09, 0x0A.
   - Allowed manufacturer read services: 0x21 and 0x22 (read data by identifier), 0x1A (read ECU identification), and read-DTC services (0x13, 0x17, 0x18, 0x19). Trim this to what Creader captures show Toyota actually uses on this truck.
   - Never allowed, even behind a flag added later: clear DTCs (0x04, 0x14), control on-board systems (0x08), session control (0x10), ECU reset (0x11), security access (0x27), communication control (0x28), any write, I/O control, or routine service (0x2C, 0x2E, 0x2F, 0x30, 0x31, 0x3B, 0x3D), any memory access or upload/download (0x23, 0x34 to 0x38), tester present (0x3E), and control DTC setting (0x85).
4. **STN adapter commands are allowlisted too.** Configuration and read commands only. No programmable-parameter writes, no firmware commands, no frame transmission that bypasses the service check. STN monitoring must use silent mode, verified before capture.
5. **Typed request builders.** The rest of the app requests data through typed functions (read_pid, read_dtcs, read_local_id, and so on). Nothing accepts arbitrary bytes or arbitrary CAN IDs bound for the bus. No raw console against real hardware; a raw console may exist only against the simulator.
6. **Rate limiting** with a hard ceiling in the safety core. Conservative defaults: 20 requests per second while logging, 5 per second during discovery. Back off on busy or response-pending negative responses (NRC 0x21, 0x78).
7. **Kill switch.** Stop all transmission immediately on error frames, error-passive or bus-off state, repeated negative responses, interface disconnect, or my hotkey. Fail safe: stop, save, report. Never retry in a tight loop. Passive capture keeps running through a polled-mode kill unless the interface itself fails.
8. **Motion interlock.** Snapshots and discovery run only when vehicle speed reads 0. While driving, the only transmissions allowed are the polls of a pre-selected logging profile. No interaction needed while driving.
9. **Battery guard.** With the engine off, stop snapshots and discovery if control module voltage drops below 12.0V.
10. **Sensitive ECUs.** Airbag (SRS) and immobilizer ECUs get DTC reads only. Exclude them from identifier sweeps.
11. **Audit log.** Every transmitted frame and every rejected request is logged with a timestamp and reason.

Tests must prove the rules:

- Unit tests for every allowlist decision, for both transports.
- Property-based tests (Hypothesis) that fuzz random CAN IDs and payloads through the transmit path and assert that nothing outside the allowlists ever reaches the interface.
- A simulator (mock CAN bus with realistic broadcast traffic, mock ECUs with ISO-TP, and a mock STN adapter) that fails the test run if it ever receives a denied service, a frame on a non-diagnostic ID, or any frame at all during passive mode.
- 100 percent branch coverage on the safety core, enforced by the test command.

## Threat model

The safety core defends against accidental or convenient bypasses by code in this repository, including code a future Claude Code session writes. It does not claim to stop code in the same process that deliberately sets out to subvert it; Python can't prevent that. The structural scanner exists to make any such route stand out and fail the suite. Reviews rate severity against this model.

## Phases

0. **Plan:** ask me questions, propose the architecture and file layout, set up the repo and the guardrails above. Stop for approval.
1. **Safety core, both transports, and the simulator,** with the full test suite. Stop.
2. **Passive drive capture and storage.** Live tests (I run them): key on engine off, then a short drive. Report what the bus looks like. Stop.
3. **Creader-assisted mapping:** conversation capture, reference template, solver, definitions output. Live test: I capture a Creader engine data stream, then a KDSS data stream. Stop.
4. **Polled reads:** identify, health snapshot, reports, logging profiles, live view, alarms. This is the first phase where our app transmits. Live test: parked first, then a drive with the grades profile. Stop.
5. **Broadcast decoding** and DBC output. Stop.
6. **Discovery** for anything the Creader never requests. Stop.
7. **STN transport** for K-line modules (if Phase 2 or 3 shows any) and the crank profile. Stop.
8. **Analysis, reports, and export packs.** Stop.

## In this repo

- **Safety core location:** everything under `src/lasto/safety/`. Nothing outside that package may import a transmit-capable binding (the PCAN DLL's `CAN_Write`, or a writable serial port). Structural tests enforce this.
- **Edit the safety core only with the Edit/Write tools**, never with shell commands, so the ask rule in `.claude/settings.json` always fires. The hardware guard hook asks before any shell command that names a safety core path.
- **Guardrails:** `.claude/settings.json` denies any shell command containing `--live`, asks before edits to the safety core and to the guardrails themselves (`.claude/`, `CLAUDE.md`), and allows the test suite and simulator. `.claude/hooks/hardware_guard.py` is the second layer: it blocks shell commands naming `--live`, PCAN channels, COM ports, serial devices, or inline PCAN/serial opens, and asks before any subagent or workflow whose instructions mention hardware or the safety core. Never weaken, bypass, or work around either; if one blocks something legitimate, tell the user.
- **Never run code that opens hardware**, including scratch scripts, inline `python -c`, or tests. The test suite's hardware firewall makes any PCAN DLL load or serial port open fail.
- **Simulator is the default.** Real hardware needs `--live` plus an explicit channel or port. The CLI parser disables option abbreviation so nothing shorter than `--live` can enable it.
- **Commits:** no Claude attribution of any kind (no Co-Authored-By trailer, no "Generated with Claude Code" line). The repo has no git identity configured; commit as the repo owner with `git -c user.name="Chris Johnson" -c user.email="56413569+cajdata@users.noreply.github.com" commit ...`.
- **Units:** store SI and raw bytes; display imperial (°F, psi, mph, miles).
- **Tests:** `.venv/Scripts/python -m pytest`. It fails below 100% branch coverage on `lasto.safety`, and it fails any test that sends the simulator forbidden traffic, even if the code under test caught the error. For many more property-test examples, run `HYPOTHESIS_PROFILE=thorough .venv/Scripts/python -m pytest tests/safety/test_properties.py --no-cov`.
- **Dev environment:** until uv is installed, `.venv` holds the package plus the pinned dev group (`pip install -e . --group dev`).
