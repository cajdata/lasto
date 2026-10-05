---
stamp: preliminary
phase: 2
order: 2.5
toc: true
title: "How the safety core enforces the rules | Lasto"
h1: How the safety core enforces the rules
description: "The machinery behind Lasto's safety rules: one write function that rechecks every frame, a sealed core with its own clock, the serial guard, and the audit log."
lede: "The Safety page lists what Lasto will and won't send. This page covers the code that holds it to that, and how the tests check it."
page_type: TechArticle
about_app: true
---

## Real hardware only when you ask {#simulator}

Every command uses the simulator unless you type `--live` plus the adapter's channel or port, every time. Shortened versions of `--live` are rejected, and there's no saved default channel. There's no raw console for real hardware; if one is ever added, it'll only talk to the simulator.

On real hardware, a session also refuses to open unless it runs on the system's own clock (below) and its audit log is the JSON Lines kind that syncs every record to disk, both checked before the PCAN driver loads. The log check reads only types for now, a known gap ([the audit log](#audit)).

## What listen-only means on the wire {#on-the-wire}

In passive mode the PCAN-USB is in hardware listen-only mode, confirmed before recording and rechecked while it records ([the rules](/safety/#listen-only)). Its CAN controller then never drives the bus. It doesn't even send the acknowledge bit every other module sends. Fig. 1 shows one frame from the engine computer, bit by bit.

```figure ack-slot 7E8#04410C0AF0000000
```

## Passive capture at the truck {#passive-capture}

`lasto drive` captures passively in armed mode ([how a capture runs](/docs/passive-capture/)). It opens the channel through the safety core's passive path: listen-only set before the controller starts, read back, and read again every {{ facts.status_interval|num }} seconds, and a channel that changes under it is closed and reopened, or the capture ends. Passive capture has no kill switch, because it has nothing to stop: it can't send.

- **At the truck,** Phase 2's tests with PCAN-Basic 5.1.0.1194 confirmed listen-only on every run, with no error frames or overruns.
- **In the tests,** every simulated drive runs against the simulated truck, which fails the test if anything at all is written to the bus.
- **If storage fails** mid-capture, because the database or the raw files can't be written, the capture stops. Lasto always asks the driver to close the channel, and logs it if the driver refuses. Every frame it read is either written or counted on screen, and in a storage error event when the database can still take one. The next capture recovers what reached the disk.
- **The data folder** refuses a name that points at a device, like a serial port, since SQLite opens its files itself, where the serial guard can't see.

## One write function checks every frame {#writer}

In Lasto's code, only the write function is given the PCAN transmit function. Each polled session creates its own and hands it to the gate, and to nothing else. It doesn't trust the gate, and for every frame it:

1. refuses if its session has closed
2. runs the whole frame check again on the exact bytes it's about to send: length, ID, service, never-list, and airbag and immobilizer limits
3. confirms the frame is the kind the caller says it is, a request or a flow-control frame
4. checks the kill switch
5. holds requests to {{ facts.ceiling_frames }} in any {{ facts.ceiling_window|num }}-second window, counted across the whole program, however many sessions are open
6. writes the audit record, and sends nothing if it can't

Only then does the frame go out, one frame at a time across the whole program. So even code that reached the write function directly could only send frames that pass the policy's check, and requests no faster than the ceiling. Only the gate checks that a flow-control frame answers a first frame (below).

::: side
Enforced in {{ code_file("pcan_active.py") }} and {{ code_link("CEILING_FRAMES", "ratelimit.py") }}.

Structural tests check that only this write function calls the transmit function, and that only the polled session and the gate can get hold of the write function.
:::

## Flow control answers a module's long answer {#flow-control}

When a module starts a long answer, like the VIN, it waits for a flow-control frame before it sends the rest. Lasto sends that frame (`30 00 00`, padded to 8 bytes) only when a request of its own is waiting, only to that approved module's request ID and never to `0x{{ facts.functional_id|hex3 }}`, and at most once per answer. It's checked and logged like a request, and it doesn't count toward the {{ facts.ceiling_frames }} requests.

The gate makes the checks that need to know what's happening: that a first frame is waiting, and that it hasn't been answered yet. The write function checks the frame's exact bytes and ID again, but it doesn't know whether a first frame is waiting.

::: side
Enforced in {{ code_file("gate.py") }}.
:::

## The rules are sealed while it runs {#frozen}

Every file in the safety core seals itself as it finishes loading. After that, ordinary attempts to swap or delete its settings and functions, or to set up its objects a second time, are refused and logged like any other refusal. Its tables are types Python won't let anything edit, so Python refuses those changes itself.

The safety core also keeps its own clock. When it loads, it takes its own copy of the system's timer and sleep functions, so code that swaps out Python's time functions can't speed up or slow down its timing. On real hardware, the listen window, the rate limits, the write function's ceiling, and how old an interlock reading may be are all timed by that clock and nothing else. Only the simulator brings its own clock.

Python can't block every trick at runtime. The structural tests ban a long list of known ones in Lasto's source, like reaching into memory with ctypes, apart from a short list of reviewed exceptions, so other code that uses one of them fails the suite. [What the design does and doesn't claim](/safety/#threat-model) explains where that line is.

::: side
Enforced in {{ code_file("_frozen.py") }} and {{ code_file("clock.py") }}.
:::

## Only the MX+ link opens a serial port {#serial-guard}

On Windows, Python's ordinary file functions can open a COM port by name, and whatever holds the MX+'s port could send the adapter anything. So the first time the safety core loads, it installs a guard for the whole program. Opening a COM port, `AUX`, or a `GLOBALROOT` path by name through Python's own file functions is refused and logged before the device is touched. The MX+ link opens its port a different way, so the guard doesn't get in its way, and a structural test keeps that other way to the MX+ link. Structural tests also fail on any string in Lasto's code that is a serial device path, and the test suite's firewall refuses opening one on its own.

The guard is built and tested. Since Phase 2, the command line installs it before running any command except the GUI, which must never load the safety core. A test runs `drive`, `log`, and `map`, each in a process of its own, to check, and another checks that the GUI never loads it. An independent review of the guard has to happen before any MX+ code touches hardware, in Phase 3. It covers:

- the guard's path matching, the test firewall's separate guard, and the structural rule. Some paths that can reach a port aren't recognized today, like a USB device path, or a port name written right after a drive letter.
- SQLite, which opens its database files where the guard can't see. The data folder's refusal of device names stands in until then.
- the two files the safety core opens at a path it's given, the audit log and the held-records file. The proposed fix refuses either one unless it's a regular file.
- that nothing Lasto runs before the command line installs the guard can open a port, and that the GUI, when it's built in Phase 9, installs its own guard for its process.

Nothing in Lasto opens a serial port before Phase 3.

::: side
Enforced in {{ code_file("serial_guard.py") }}.
:::

## The audit log {#audit}

Every frame Lasto sends is written to the audit log with the time and the reason, before it goes out. If the log can't be written, nothing is sent. On real hardware, a session refuses any audit log but the JSON Lines file that syncs every record to disk, and logs the refusal, before the PCAN driver loads.

Every refusal is logged too, at the spot where it happens. Anything in the safety core that refuses a request, frame, setting, or command goes through one function that records it and then raises it, and a structural test checks that the safety core never raises a refusal error directly. Kill-switch trips go to every open log, or are held like refusals.

If no audit log is open yet, like for a bad request built before a session starts, the record is held and written to the next log that opens. `lasto drive` also writes every held record to `audit/held.jsonl` as it's held, synced to disk, so a crash doesn't lose it. If that write fails, the record stays held in memory, and the exit report counts it. That file is set once per process, and a second attempt is refused and logged. Under other commands, held records live only in memory, so a crash could lose them. In memory, up to 10,000 are held; past that, the oldest leave memory and are counted, and only under `lasto drive` are they still in the held file. Anything still held when Lasto exits is printed on screen. After `lasto drive`, the report also names the held file.

Known gaps: closing a polled session or an MX+ link twice can cut off a log that another session shares. Two more from the Phase 2 review close before the first polled test at the truck: the durable-log check runs after the log is attached, so a refused log gets its own refusal and every record held until then, which then drop off the exit report (L6), and it checks types, so a log written to `os.devnull`, or an auditor with a fake clock, would pass (L7).

::: side
Enforced in {{ code_file("audit.py") }}, {{ code_file("session.py") }}, {{ code_file("pcan_active.py") }}, and {{ code_file("killswitch.py") }}.
:::

## How the tests check the rules {#tests}

- Unit tests cover every allow and deny decision, for both adapters.
- Property-based tests (Hypothesis) throw random IDs, payloads, and adapter commands at the send path, and check that nothing outside the allowlists reaches the fake driver.
- Structural tests read the source: only the write function calls the PCAN transmit function, only the polled session and the gate get hold of the write function, only one file imports the serial library and no code names a serial port, the passive code can reach neither the write function nor that file, the safety core never raises a refusal error directly, the safety core imports nothing from the rest of Lasto, and no code changes the safety core by any of the routes the scanner knows.
- The scanner follows banned modules, and the serial library, through re-exports, so a route like `from lasto.safety.pcan_dll import ctypes` fails the suite.
- Outside the safety core and the test suite's firewall, the app uses ctypes in one place, `operations/keep_awake.py`. It calls only Windows' SetThreadExecutionState, from the main thread, to keep the laptop from idle sleep during a capture. A structural test holds it to that one function, and edits to it ask first, like edits to the safety core.
- A committed snapshot of the safety configuration is proven equal to the safety core by a test, for the settings it lists, and every capture records the snapshot it ran under.
- A simulated truck fails the whole test run if it ever receives something forbidden, even when the code under test caught the error.
- Tests use a temporary data folder in place of the real one, and none reaches a driver: the `--live` paths use stand-ins, and the test firewall refuses the PCAN driver, the serial library, and opening a serial port by any name it recognizes.

The test command fails below 100 percent branch coverage on the safety core, and below 90 percent on capture and storage.
