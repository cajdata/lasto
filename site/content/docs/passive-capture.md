---
stamp: preliminary
phase: 2
order: 2.7
toc: true
crumb: Passive capture
title: "Passive capture: recording the GX470's CAN bus | Lasto"
h1: "Passive capture: recording the truck's CAN bus"
description: "How lasto drive records a 2006 GX470's CAN bus in listen-only mode, what it stores, how lasto log reads it back, and what the first runs at the truck found."
lede: "Passive capture is built, approved with Phase 2, and has run at the truck. Lasto opens the PCAN-USB in listen-only mode, records every frame on the truck's CAN bus, and sends nothing."
page_type: TechArticle
about_app: true
sources: [docs/architecture.md, src/lasto/storage/segments.py, src/lasto/storage/root.py, src/lasto/capture/recovery.py, src/lasto/services/sessions.py]
---

## The short version {#summary}

- `lasto drive` records the high-speed CAN bus in armed mode: the first frame starts a session, and {{ capture.silence|num }} seconds without one ends it. With the key off the bus is silent, so a capture left running adds no data while the truck is parked, though it keeps the laptop awake.
- Every second of traffic goes to a compressed candump file and is synced to disk as it completes, and a SQLite database indexes it. A crash costs at most the second being written, and the next capture indexes every complete second that reached the disk.
- `lasto log` lists the sessions and shows each one's events, its audit log, statistics for every CAN ID, and one ID's raw frames. It only reads.
- In Phase 2's runs at the truck, the bus carried 17 IDs at about 520 to 540 frames a second, and listen-only held on every run.

::: side
Most of the capture's timings and limits on this page are read from Lasto's source each time the site is built. The step D figures are what the truck showed. The 0x025 layout is a provisional reading of one session.
:::

## Starting a capture {#drive}

At the truck, give the adapter's channel every time. There's no saved default:

```
lasto drive --live --channel PCAN_USBBUS1
```

- `--channel` takes `PCAN_USBBUS1` to `PCAN_USBBUS16`, and only with `--live`. Without `--live`, `lasto drive` uses the simulator, for {{ capture.simulated_seconds|num }} simulated seconds unless `--seconds` says otherwise.
- At the truck `--seconds` is an optional time limit. Without it, a capture runs until you stop it.
- `--data` picks another data folder (below).
- `--port` (the OBDLink MX+) and `--profile` (polled logging) are refused for now. They arrive in later phases.

Before the channel opens, `lasto drive` takes the capture lock, so only one capture at a time writes the capture database. It recovers anything an interrupted capture left, starts the run's own audit log, and records the run with a snapshot of the safety configuration it runs under. Then it prints the free disk space and opens the PCAN-USB, setting listen-only mode before the controller starts and reading it back ([the rules](/safety/#listen-only)). It prints the adapter and PCAN-Basic version the channel reported, then waits for traffic.

During a capture at the truck, Lasto keeps Windows from sleeping when idle. That's all it changes: the display may still turn off, and closing the lid still does what your Windows power settings say.

## Armed mode {#armed}

- **A session starts** with the first frame on the bus, and ends after {{ capture.silence|num }} seconds without one, at its last frame. The next frame starts a new session. So one capture left running covers key on, the drive, key off, and the next key on.
- **While it runs,** Lasto reads the channel every {{ capture.poll_ms }} ms. {% if capture.tick == 1 %}Once a second{% else %}Every {{ capture.tick|num }} seconds{% endif %} it updates the database's copy of the audit log, writes a status feed other tools can read, and checks for a stop request. Every {{ capture.progress|num }} seconds it prints a progress line with the session's frame count and rate.
- **Listen-only is read again** every {{ facts.status_interval|num }} seconds, and right away if the driver reports the adapter rejoining the bus. If it ever reads anything but on, the safety core closes the channel and runs the whole opening sequence again, up to {{ facts.reopen_attempts }} tries and at most {{ facts.max_reopens }} reopens in one capture, however many sessions it records. If that fails, the capture ends and the log says why.
- **Bus status** while no session is open is kept as an event of the run, so it never starts a session.

## Stopping a capture {#stopping}

Press Ctrl+C or Ctrl+Break, or create a file named `capture.stop` in the data folder from another window. Lasto looks for it every {{ capture.tick|num }} s. A time limit, a storage failure, and the safety core giving up on the channel stop it too. Every stop takes the same path: read the channel once more if it's still open, close the session at its last frame, close the channel, finish the audit log's index, and end the run with the reason. After a storage failure, a session that can't be finished is left for the next capture to recover.

`lasto drive` exits with 1 if another capture holds the data folder, if the safety core refuses the channel, if the channel is lost, if storage fails, or if the data folder names a device. It exits with 2 if the command line is refused, like `--port` or `--profile`. Any other stop exits with 0.

## What it stores {#storage}

The data folder is `--data` if you give one, then the `LASTO_DATA` environment variable, then `%LOCALAPPDATA%\lasto`. A name that points at a device, like a serial port, is refused: SQLite opens its files itself, where the serial guard can't see them.

Table: The data folder

| Name | What it holds |
|---|---|
| `sessions/` | Raw traffic: `sessions/<date>/<session ID>/seg-0001.candump.zst` and on |
| `capture.sqlite` | Runs, sessions, events, the audit index, and each ID's statistics for every second |
| `workbench.sqlite` | Vehicles, notes, and tags. A capture adds only the default 2006 GX470, the first time |
| `live.sqlite` | The status of a capture in progress, {% if capture.tick == 1 %}once a second{% else %}every {{ capture.tick|num }} seconds{% endif %} |
| `audit/` | Each run's own audit log, and `held.jsonl` |
| `capture.lock` | Held by the capture that's running |

- **Raw traffic** is candump text, one compressed zstd frame per second with its checksum on, synced to disk before the database records it. A new file starts with every session and every {{ capture.segment_minutes }} minutes into one, and the recorder never adds to a file that already exists. `zstd -d` turns a segment back into plain candump text that `can-utils` reads.
- **The capture database** commits once a second, and only after that second is on disk, so it never points at data that isn't there. It keeps each ID's count, timing, and which data bits changed for every second, so `lasto log` can describe a session without reading its raw files. Every frame's time is the adapter's timestamp, set against the laptop's clock when the session's first frame arrived, and an anchor every {{ capture.anchor_every|num }} seconds records how far the two clocks drift.
- **After a crash,** the next capture reads each unfinished session's files, indexes each complete second in order, and moves what's left, like a torn end, into a `.torn` file beside the segment, so no byte that reached the disk is thrown away.
- **The audit log** is a JSON Lines file per run, each record synced to disk as it's written. On real hardware the safety core refuses any other kind of log, judged by type for now ([a known gap](/safety/core/#audit)). Anything held while no audit log is open also goes to `audit/held.jsonl`, synced as it's written.

Storage measured at the truck: raw files take 13 to 16 MB per hour of traffic, and the database about 4 MB an hour, so a driving hour costs about 17 to 20 MB.

Nothing is deleted through Phase 5: early drives are the reference for mapping and decoding. After that, the plan is a {{ capture.budget_gb }} GB budget for the data folder, with the oldest raw files going first, never the newest {{ capture.newest_kept_days }} days, and never a session that's open, tagged, has notes, or is used by mapping work. Deleting will only happen by command or in the GUI, never automatically and never during a capture, and isn't built yet. As it starts, each `lasto drive` prints the free space and the folder's use of its budget, and warns below {{ capture.low_free_gb }} GB free or over budget. A warning never stops a capture.

## Reading it back {#log}

```
lasto log
lasto log last
lasto log last --bus
lasto log last --id 025
lasto log last --id 025 --from 10 --to 20
```

- `lasto log` lists every session, newest first: when it started, how long it ran, how it ended, its frames, how many IDs, and its size on disk.
- `lasto log last`, or a session's ID or its first few characters, shows that session: the run it belongs to, the adapter and PCAN-Basic version, its events, and the run's audit log, one record per line.
- `--bus` adds a table of every CAN ID in the session: frames, rate, period, the shortest and longest gaps, data length, and which bits of each byte ever changed. It comes from the database alone.
- `--id` prints one ID's raw frames with their times, from the session's first frame and in UTC, optionally from `--from` to `--to` seconds in. Give `--data` if the capture used another folder.

`lasto log` opens the databases read-only and never changes them, though SQLite may leave its `-wal` and `-shm` files beside a database it reads. If a capture is running, it says so first, with its frame count and rate.

## What the first runs at the truck found {#bus}

Step D, Phase 2's test at the truck, ran from 2026-09-30 to 10-01 with PCAN-Basic 5.1.0.1194: key off, key on with the engine off, and a drive, four runs in all.

- **Listen-only held on every run,** with no error frames, overruns, or status events. The driver never reported the adapter rejoining the bus, and the {{ capture.silence|num }}-second silence end closed a session on its own.
- **Key off, the bus is silent.** Key on, 17 IDs at about 520 to 540 frames a second, roughly 11 to 12 percent of the 500 kbit/s bus.
- **None of the 17 is a diagnostic ID.** The IDs Lasto may send on, `0x{{ facts.functional_id|hex3 }}` and the engine computer's, weren't among them.
- **Vehicle speed** isn't on the IDs other Toyotas use for it. Until a broadcast speed is found and checked, Phase 4's parked-only interlock asks for speed with a standard OBD-II request.

Table: The truck's broadcast IDs with the key on. What each carries is provisional until Phase 3 mapping and Phase 5 decoding confirm it.

| ID | Rate | Bytes | What step D saw |
|---|---|---|---|
| `020` | 75 to 78 Hz | 3 | Never changes |
| `022`, `023` | 75 to 78 Hz | 8, 7 | Two 10-bit fields in bytes 0 to 3 change while driving, fewer bits with the engine off |
| `025` | 75 to 78 Hz | 8 | The steering angle sensor ([below](#steering)) |
| `223`, `224` | 38 to 39 Hz | 8 | A few bits, and `224`'s bytes 4 and 5 |
| `2C1`, `2D0` | 31.5 Hz | 8 | Static with the engine off, changing with it running |
| `2C4` | 42.4 Hz | 8 | Static with the engine off, changing with it running. Bytes 0 and 1 fit other Toyotas' engine speed |
| `2D2` | 31.5 Hz | 1 | Never changes |
| `3D0` | 4 Hz | 1 | The low 6 bits change while driving |
| `420`, `423`, `4C1`, `4C3`, `4C6`, `4C7` | about 1 Hz | 8 or 1 | Never change, probably status or keep-alive frames |

::: side
IDs with identical frame counts probably share a sender: `020`, `022`, `023`, and `025`; `223` and `224`; `2C1`, `2D0`, and `2D2`.

Before the first polled test at the truck, all 17 go into the gate's list of IDs Lasto never sends on, as a reviewed safety core change of its own (Phase 4). Step D didn't cover 4WD low, cruise, reverse, or the lights, so later captures may add more.
:::

## 0x025, the steering angle (provisional) {#steering}

This layout is provisional. It comes from one session, which the truck's owner read with `lasto log SESSION --id 025` and decoded with the layout other Toyotas use for their steering angle sensor. Phase 3 checks it against what the stability control and KDSS report through the scan tool, and Phase 5 writes the confirmed definition. Until then, treat every row below as a working hypothesis.

Table: 0x025, provisional pending Phases 3 and 5

| Bytes | What they seem to carry |
|---|---|
| Byte 0 low 4 bits, byte 1 | The angle: a signed 12-bit number, its top 4 bits from byte 0 and its low 8 from byte 1, 1.5° per step, positive to the left |
| Byte 0, bit 4 | Set only for about 0.14 s after power-up, and for the last 25 ms before power-down |
| Bytes 2 and 3 | Always `0F F9` |
| Bytes 4 to 6 | Near `80` at rest, moving only while the wheel turns |
| Byte 7 | Changes on nearly every frame, likely a checksum |

Read that way, the angle followed the owner's timeline of the session exactly: +555° at full left lock, -565.5° at full right, and about -9° with the wheels straight. At power-up the sensor sends a burst of about 65 frames, 0.25 ms apart. So the sensor itself broadcasts a live, sensible angle, which matters for the steering-related trouble code C1777 Phase 3 looks into.

## What isn't proven yet {#not-proven}

- What each broadcast ID carries, the steering angle included. It's a hypothesis until Phases 3 and 5.
- Two bench tests, with the PCAN-USB and the MX+ checking each other. The PCAN-USB's listen-only test was optional for Phase 2. The MX+ silent mode test has to pass before the MX+ monitors the truck, in Phase 3.
- Whether the 17 IDs are all of them. Step D didn't cover 4WD low, cruise, reverse, or the lights.
- The serial guard's independent review. It has to happen before any MX+ code touches hardware, in Phase 3, and has three known gaps to close ([the list](/safety/core/#serial-guard)).
- Deleting old raw data, which comes after Phase 5.
