---
stamp: preliminary
phase: 1
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

## What listen-only means on the wire {#on-the-wire}

In passive mode the PCAN-USB is in hardware listen-only mode, confirmed before recording and rechecked while it records ([the rules](/safety/#listen-only)). Its CAN controller then never drives the bus. It doesn't even send the acknowledge bit every other module sends. Fig. 1 shows one frame from the engine computer, bit by bit.

```figure ack-slot 7E8#04410C0AF0000000
```

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

The guard is built and tested. An independent review of it is scheduled before Phase 3, the first phase where MX+ code touches hardware, and two things have to happen by then:

- The review covers the guard's path matching, the test firewall's separate guard, and the structural rule. Some paths that can reach a port aren't recognized today, like a USB device path, or a port name written right after a drive letter.
- Today the guard goes in when the safety core first loads, so code that runs before that isn't covered. Phase 2 installs it at the start of every hardware command.

Nothing in Lasto opens a serial port before Phase 3.

::: side
Enforced in {{ code_file("serial_guard.py") }}.
:::

## The audit log {#audit}

Every frame Lasto sends is written to the audit log with the time and the reason, before it goes out. If the log can't be written, nothing is sent.

Every refusal is logged too, at the spot where it happens. Anything in the safety core that refuses a request, frame, setting, or command goes through one function that records it and then raises it, and a structural test checks that the safety core never raises a refusal error directly. Kill-switch trips go to every open log, or are held like refusals.

If no audit log is open yet, like for a bad request built before a session starts, the record is held and written to the next log that opens. Up to 10,000 are held; past that, the oldest are dropped and only counted. Anything still held when Lasto exits is printed on screen. Held records live in memory for now, so a crash could lose them. Phase 2 adds a file for them.

Known gaps: closing a polled session or an MX+ link twice can cut off a log that another session shares, and until Phase 2 a session on real hardware accepts any audit log, even one that keeps nothing.

::: side
Enforced in {{ code_file("audit.py") }}, {{ code_file("pcan_active.py") }}, and {{ code_file("killswitch.py") }}.
:::

## How the tests check the rules {#tests}

- Unit tests cover every allow and deny decision, for both adapters.
- Property-based tests (Hypothesis) throw random IDs, payloads, and adapter commands at the send path, and check that nothing outside the allowlists reaches the fake driver.
- Structural tests read the source: only the write function calls the PCAN transmit function, only the polled session and the gate get hold of the write function, only one file imports the serial library and no code names a serial port, the passive code can reach neither the write function nor that file, the safety core never raises a refusal error directly, and no code changes the safety core by any of the routes the scanner knows.
- A simulated truck fails the whole test run if it ever receives something forbidden, even when the code under test caught the error.

The test command fails below 100 percent branch coverage on the safety core.
