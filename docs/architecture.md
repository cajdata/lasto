# lasto architecture

Status: **Phase 0 approved 2026-09-26. Phase 1 approved 2026-09-28,** with an independent review of the serial guard recorded as a Phase 3 blocker (§13). Decisions are in §0 and override anything below that says *(open)*. Facts about the truck are hypotheses from documentation research until a capture confirms them.

## 0. Decisions (Phase 0, 2026-09-26)

- **Machines:** Claude runs on the desktop, which never has the PCAN or MX+ attached. The truck laptop (i7-8550U with SSE4.2 and AVX2, 16 GB RAM, Windows 11 25H2) is the only machine that touches hardware. It has internet at home for setup and updates and runs offline in the truck.
- **Hardware:**
  - PCAN-USB IPEH-002022 (opto-decoupled, SJA1000 controller), with PCAN-Basic ≥ 4.7.0 and not 5.0.0. Tested at the truck in Phase 2 with PCAN-Basic 5.1.0.1194, from PEAK Device Driver Setup 5.1.3.
  - A 3-way 16-pin splitter so the Creader, PCAN, and MX+ connect at once.
  - No bench bus yet; parts list in §12.
- **Phase order:**
  - MX+ passive K-line monitoring moves into Phase 3, so KDSS, VSC, and suspension conversations can be mapped.
  - K-line polling (StartCommunication 0x81, no 0x3E keep-alives) is decided in Phase 7.
- **STN silent mode:** verified by the §3.7 checks (PP 21, `STCMM 0` → `OK` before every monitor start, fixed protocol, one-time bench test).
- **ECU approval:** new request IDs and K-line addresses are approved only by a safety core code change in its own commit.
- **Motion interlock probes:** polled mode only; passive mode never transmits, including for the interlock. Once broadcast speed is decoded and verified, it replaces polling.
- **Kill switch:** 3 consecutive NRCs on one request, or 10 NRCs within 30 s. Hotkey Ctrl+Alt+K.
- **Vehicle:**
  - The speedometer is calibrated for stock 265/65R17 (not recalibrated). Current tires are 265/70R17, soon LT255/80R17.
  - The axle ratio is stock 3.727. A regear to 4.56 or 4.88 happens only if the data shows the transmission hunting on grades.
- **Alarms:**
  - transmission 220 °F warning, 240 °F critical
  - coolant 225/235 °F
  - voltage below 12.5 V or above 15.0 V while running
- **CLI:** `log` lists sessions and the audit log. `verify` confirms solver candidates into verified definitions.
- **Sessions:** in armed mode, a passive session ends automatically after 60 s of bus silence following key-off, and a new passive session starts automatically when traffic resumes. A polled session is never started automatically.
- **Data:** `%LOCALAPPDATA%\lasto`, with a disk budget of 20 GB (owner's decision, 2026-10-01, after Phase 2 measured storage per driving hour; retention in §4).
- **Tooling:** uv, Python 3.13. Since Phase 1's approval (2026-09-28), uv manages `.venv` from a hashed `uv.lock` (§10).
- **Git:** commit locally; push after you approve each phase.

**Phase 1 review (2026-09-27):**
- **Fixed in their own safety core commits:** every refusal is audited where it's raised; passive capture re-reads listen-only continuously and never trusts a channel that changed under it.
- **Interpretations confirmed:**
  - flow control after a functional request goes to the responder's physical ID
  - a late answer within 1 s is ignored, and logged
  - identify counts as parked-only
  - logging requests must always be in the profile
  - flow control frames aren't rate limited
  - NRC 0x78 extends the current wait
- **Carried forward:** see §13, including the verification review's low findings L1 to L5.

## 1. Shape of the system

```
                       ┌──────────────────────── safety core: src/lasto/safety ────────────────────────┐
 OBD-II ── PCAN-USB ──►│ pcan_passive  listen-only open + readback; read-only DLL binding, no CAN_Write │── frames ──┐
                       │ pcan_active   normal-mode channel; the ONLY CAN_Write binding                  │            │
 OBD-II ── MX+ (BT) ──►│ stn_port      the ONLY serial writer; exact-match STN command allowlist        │            │
                       │ gate          typed request → policy → interlocks → rate limit → audit → write │◄─ requests │
                       │ policy, ecus, requests, isotp, killswitch, ratelimit, interlocks, audit, reader│            │
                       └────────────────────────────────────────────────────────────────────────────────┘            │
                                                                                                                      ▼
  capture/ (fan-out) ──► storage/ (zstd candump + SQLite WAL) ──► decode/ ──► view/  report/  export/  analysis/
        └──► protocol/ (passive ISO-TP + KWP reassembly of Creader conversations) ──► mapping/ (template, solver)
  polling/, snapshot/, identify/, discover/ ──► typed requests into the gate (never bytes, never raw IDs)
  sim/ replaces PCANBasic.dll and the serial port underneath the safety core, so tests run the real code.
```

Three rules shape everything:

1. **Only `src/lasto/safety/` can put bytes on a wire.** Everything else hands the gate typed request objects. Structural tests enforce this (§7).
2. **Capture never depends on anything downstream.** Raw frames reach disk first. Decoding, the live view, and polling are subscribers that can fail without losing data.
3. **The simulator sits below the safety core.** `FakePcanDll` and `FakeStnPort` have the same interface as the real DLL and serial port, so the code under test is the code that runs at the truck.

## 2. What the research says about the truck (hypotheses)

From factory manuals for the J120 platform (Land Cruiser Prado RM1151E, from Aug 2004; 2007 FJ Cruiser) plus the 2005 GX470 feature notes:

- **The CAN bus probably has four nodes:** ECM, skid control (ABS/VSC/TRC) ECU, steering angle sensor, yaw rate/decel sensor. There's no CAN gateway (the 0x750 + address scheme is ~2016+).
- **Only the ECM does diagnostics over CAN:** 0x7E0 → 0x7E8, and very likely the transmission (ECT) as a second logical address at 0x7E1 → 0x7E9.
- **Everything else diagnoses over the K-line (SIL, pin 7, KWP2000 "M-OBD"):** skid control ECU, KDSS stabilizer ECU, suspension (AVS/height) ECU, TPMS, airbag, body. The skid ECU is on CAN but its manual says "DTC communication uses the SIL line".
- **The steering angle sensor is a CAN node from MY2005** and broadcasts to the skid ECU; 0x025 is a plausible ID (unconfirmed). The first passive capture will show whether it's broadcasting.
- **C1777 on this platform is "steering angle sensor circuit" logged by the suspension control ECU (AVS), not KDSS.** KDSS codes are C18xx. The skid ECU data list range is −1152 to +1150.875°, so 1150.875 is the field's maximum. Toyota VSC systems show ~1150° until the zero point is learned by driving straight above ~28 mph. A value stuck there after such a drive points to the sensor, its power, or its CAN link.
- Stock tires 265/65R17 (30.6 in), axle ratio 3.727 front and rear, A750F ratios 3.520/2.042/1.400/1.000/0.716.
- ATF temperature on pre-2010 CAN Toyotas is commonly read with 0x21 local ID 0xD9 at 0x7E0 or 0x7E1. That's inconsistent across trucks, so the Creader capture decides.

**Consequences for the plan:**

- **Phase 3 can map engine and transmission data from CAN captures, but not KDSS.** The Creader will talk to KDSS, VSC, suspension, and TPMS over the K-line, which the PCAN can't see. Mapping those needs the MX+ passively monitoring the K-line while the Creader talks. That moves K-line monitoring (part of Phase 7) ahead of the KDSS mapping. *(open)*
- **Polling K-line ECUs conflicts with the service rules.** A K-line session starts with an init sequence that sends StartCommunication (service 0x81), and adapters keep it alive with tester present (0x3E), which is on the never-list. See §3.8. *(open)*

## 3. Safety core

**Threat model.** The safety core defends against accidental or convenient bypasses by code in this repository, including code a future Claude Code session writes. It does not claim to stop code in the same process that deliberately sets out to subvert it; Python can't prevent that. The structural scanner (§7) exists to make any such route stand out and fail the suite. Reviews rate severity against this model.

### 3.1 PCAN passive (rule 1)

python-can 4.6.1's `PcanBus` sets listen-only before `CAN_Initialize`, but it ignores the `SetValue` result, and its `state` property returns a cached value instead of asking the driver. So if the pre-init set fails, the channel silently comes up active. The safety core binds the DLL itself with ctypes:

- **Loading:** from `PCANBasic.dll` in the Windows system folder, by absolute path. The system folder is asked of Windows once, at import (`GetSystemDirectoryW`), never read from the `SystemRoot` environment variable, so nothing that changes the environment can redirect which DLL loads; if Windows can't say, the DLL isn't loaded. Require a 64-bit Python process, and require a PCAN-Basic API version of at least 4.7.0 other than 5.0.0 (5.0.0 mishandles status messages).
- **`pcan_dll.load_readonly()`** binds only `CAN_Initialize`, `CAN_Uninitialize`, `CAN_GetValue`, `CAN_GetStatus`, `CAN_Read`, and `CAN_GetErrorText`, plus a `CAN_SetValue` wrapper that accepts only these (parameter, value) pairs: listen-only ON, error frames ON, status frames ON. It never looks up `CAN_Write*`. It also never binds `CAN_Reset` (can hard-reset the controller) or `CAN_FilterMessages` (resets the controller).
- **Passive open sequence:**
  1. `PCAN_CHANNEL_CONDITION` must be `AVAILABLE`. If PCAN-View or another app holds the channel, the controller may already be active, so refuse.
  2. `SetValue(LISTEN_ONLY, ON)` on the uninitialized channel; check the result.
  3. `CAN_Initialize` at 500 kbps.
  4. `GetValue(LISTEN_ONLY)` must read ON; otherwise uninitialize and refuse.
  5. Enable error and status frames, then record the hardware name, channel version, and API version.

  There is no fallback that sets listen-only after initializing.
- **Reading:** the capture loop drains `CAN_Read` every few milliseconds (up to 4,096 frames per pass). That needs no Windows receive event, and so one less driver setting. The driver buffers 32,768 frames, and timestamps come from the hardware, so polling costs no accuracy. Every 100 ms the reader polls `CAN_GetStatus` (an unplug shows up there as `ILLHW`, not reliably in `CAN_Read`) and re-reads listen-only. The driver's "controller activated" status message triggers an immediate re-read.
- **Losing trust:** if listen-only reads anything but ON, or the interface reports a failure, the passive session stops trusting the channel. It logs why, then uninitializes the channel, which discards anything the driver resumed on its own; listen-only isn't guaranteed to survive a replug.
- **A failing audit log (a full disk):** if any record of distrusting, reopening, ending, or closing can't be written, the session still closes whatever channel it holds, reopens nothing on top of the failing log, ends, lets the log go, and raises the failure (Phase 3, from the website session's claim check).
- **Reopening:** the session then runs the full `open_passive` sequence again: at most 3 attempts per incident, 2 s apart, and at most 10 reopens per session. If it can't reopen, the session ends and logs why.
- **A channel that won't close:** every close checks `CAN_Uninitialize`'s answer. If the driver refuses, the failure is audited (the channel may still be on the bus), a passive session ends instead of reopening on top of it, and session and channel close records say whether the channel really closed.
- **Limit:** a reconnect the driver reports nowhere, after which listen-only still reads ON, can't be detected. In that case the controller is still listen-only, so nothing can be transmitted.
- In listen-only the SJA1000 controller is forced error-passive, so an error-passive status is expected in passive mode. It's recorded, not treated as a fault.
- **What PEAK doesn't promise:** setting listen-only before init is supported since PCAN-Basic 1.0.6, but PEAK says only that it applies "as fast as possible". It never states there is zero time in active mode at bus-on. Linux drivers set silent mode before bus-on, so it's very likely fine. A bench test would prove it (§12).
- **Timestamps:** microseconds since Windows started, with 42 µs resolution on a PCAN-USB. Each session also records host-clock anchors.

### 3.2 PCAN polled

`pcan_active.py` is the only module that binds `CAN_Write`, and it looks it up once, by name in code. `open_active` returns two objects:

- an `ActiveChannel`, which only reads (and switches to listen-only after a kill). The reader gets this one.
- a `Writer`, the only object that holds `CAN_Write`. The session hands it to the gate and keeps no other reference.

The `Writer` doesn't trust its caller. For every frame it re-runs the policy's full stateless check (`policy.check_frame`: ID allowlist, broadcast IDs, ISO-TP parse, service allowlist and never-list, sensitive ECUs, canonical flow control on an approved ECU's request ID), refuses a frame that isn't the kind the caller named, checks the kill switch, holds request frames to the hard ceiling (§3.3, rule 6), and writes the audit record, all before `CAN_Write`. So even a direct call can't send a denied service, use a non-allowlisted ID, or exceed 20 requests per second. It could still send the canonical flow control frame on an approved request ID with no first frame waiting, uncounted by the ceiling: whether flow control answers a waiting first frame is checked only by the gate, which is why only the gate calls the `Writer` (§7). The checks that need state stay in the gate: flow control only for a first frame that is waiting for it, the interlocks, and the per-purpose rates and back-off.

`session.py` imports the gate and `pcan_active` inside `open_polled_session`, so no public module carries a name that can transmit. Frames are always 8 bytes, padded with a byte confirmed from Creader captures.

### 3.3 The gate (rules 2, 3, 5, 6, 8, 9, 10, 11)

Callers use typed builders: `read_pid(ecu, pids)`, `read_dtcs(ecu, kind)`, `read_freeze_frame(...)`, `read_mode06(...)`, `read_vehicle_info(...)`, `read_local_id(ecu, lid)`, `read_did(ecu, did)`, `read_ecu_id(ecu, option)`. `ecu` is an entry from the approved ECU table, never a raw ID. Each request carries a purpose: `logging`, `snapshot`, `identify`, `discovery`, or `interlock_probe`.

For every request, in order, stopping at the first failure:

1. **Kill switch latched?** Refuse. The latch never resets within a process.
2. **Mode:** polled sessions only. A passive session has no gate object at all.
3. **Interlocks:** motion (§3.5), battery (§3.6), sensitive ECUs (SRS and immobilizer: DTC reads only, excluded from sweeps).
4. **Encode** to exactly one ISO-TP single frame. Anything that would need more than one frame is refused, so the app never transmits first or consecutive frames.
5. **Re-parse the encoded bytes independently** (normal or extended addressing) and check them against the policy:
   - CAN ID is 0x7DF or an approved physical request ID, and not in the observed-broadcast set.
   - The never-list is checked first and can't be overridden. Then default deny against the allowlist.
   - Manufacturer services (0x1A, 0x21, 0x22, 0x13, 0x17, 0x18, 0x19) go only to physical IDs, never to 0x7DF, so one request can't reach every ECU at once.
6. **Rate limit:** a token bucket per purpose (logging 20/s, discovery 5/s), under a hard ceiling constant. One outstanding request per ECU; wait for the response or the P2/P2* timeout.
   - The write functions enforce the ceiling again on their own, process-wide: at most 20 request frames in any 1.0 s window across every `Writer` in the process, with one frame on the wire at a time so concurrent callers can't slip past together.
   - **Closing:** a closed polled session's gate and write function refuse everything from then on (`session_closed`, `writer_closed`), and its reader stops, so an old session can't send or read through a channel a new session reopened.
   - **Clock:** on real hardware every timing rule (this window, the rate slots, the listen window, how old an interlock reading may be) runs on `SystemClock` itself. The sessions and `open_active` refuse any other clock, subclasses included, before the DLL is loaded. Only the simulator brings its own clock. `SystemClock` keeps its own references to `time.monotonic` and `time.sleep`, taken at import, so rebinding them on the `time` module can't steer it (review finding P3). Flow control frames don't count (the gate sends at most one per first frame).
   - So the gate never meets that backstop, it counts the shared spacing from the end of each write and adds a 1 ms margin, making its fastest pace 19.6 requests per second. Pacing at exactly 50 ms would put 21 frames inside one second through float rounding alone.
   - **Reading before sending:** the gate waits for its slot in slices of at most 20 ms and reads the channel after each (the polled reader, with an immediate status check), stopping at once on a kill. The last read comes right before the frame goes out, so every kill trigger that has already arrived is seen first: an error frame, a bus-off shown only in the status, or another tester. Then the interlocks are checked again with that moment's readings.
7. **Audit, write-ahead:** log the frame and purpose. If the audit write fails, don't transmit. On real hardware the sessions and `open_active` refuse, before the DLL is loaded, any audit log but an `Auditor` writing a `JsonlAuditSink`, which fsyncs each record before it returns; a subclass or a look-alike could hold records in memory or drop them (review finding L5). Only the simulator may use another sink.
8. **Write**, then log the result.

Rejections are logged with the reason and raised as `SafetyViolation`.

**Flow control:** the receive side sends `30 00 00` (padded) only when a first frame arrives on the response ID paired with an outstanding request. It goes to that request's physical ID, at most once per first frame. After a functional request, FC goes to the responding ECU's physical request ID, and only if that ID is approved. Otherwise the response is abandoned.

**Approved request IDs and K-line addresses** live in `safety/ecus.py`. It starts with 0x7E0 only. Each entry records the response ID, addressing mode, ECU kind (engine, transmission, abs_vsc, kdss, suspension, tpms, srs, immobilizer, body), and the evidence (which capture). Adding one is a safety core change: it needs your approval, the ask rule fires, and it gets its own commit. An unclassified ID can't be approved. *(open)*

At import, each entry's IDs, extended address, and kind are copied into a route, a tuple. The policy and the gate read only routes, never an entry's fields, so an entry rewritten in place can't move a frame to another ID (review finding P1).

**Foreign tester guard:** a polled session listens for 2 s before its first transmission and keeps watching. On real hardware (no simulator library) a shorter window, or one that isn't a number, is refused before the DLL is loaded; only the simulator may shorten it. Any frame on an approved request ID that we didn't send (the Creader, or broadcast traffic) stops polling. Two testers at once can confuse ECUs.

### 3.4 Kill switch (rule 7)

**Triggers:**
- any error frame
- error-passive or bus-off status in polled mode
- queue overrun, interface errors, or unplug
- the hotkey
- repeated negative responses
- a response to a service we didn't request
- any exception inside the gate

**Repeated NRCs:** 3 consecutive on one request, or 10 within 30 s. NRC 0x78 extends the wait (P2*, 5 s) without resending. NRC 0x21 backs off exponentially from 200 ms to 5 s. In discovery, 0x11/0x12/0x31 mean "not supported" and count against a separate, higher limit. *(open: thresholds)*

**On a kill:**
1. Latch the gate closed and stop the scheduler.
2. Switch the channel to listen-only and read it back. On the SJA1000 this passes briefly through controller reset, which is off-bus and harmless. If the readback fails, the channel is closed (uninitialized), so it can't stay on the bus in normal mode.
3. Flush storage and record the cause.
4. Keep capture running in listen-only, with listen-only re-read on every status check and at once when the driver reports the controller reactivated. If it's ever lost, or the interface fails, the channel is closed, so the driver can't resume it in normal mode after a replug. Nothing retries automatically.

**One kill switch for the process (`KILL_SWITCH`):**
- **It latches until lasto restarts.** After a kill, `open_polled_session` refuses before touching the adapter, audited as `kill_switch`. Passive sessions still open, since they can't transmit.
- **Nothing takes it as a parameter.** The gate, the write function, the polled reader, the NRC monitor and the hotkey all use it directly, so none can be handed a fresh one.
- **Every session hears a kill.** Each open polled session registers a listener that switches its channel to listen-only, and removes it when the session closes. Every listener runs even if one fails.
- **Every trip is audited.** It reaches every open audit log, or is held for the next one if none is open.
- **There is no production reset.** The test suite runs as one process, so the test plugin clears the kill switch before each test with `reset_for_tests()`. That reset refuses (audited) unless the test hardware firewall is installed, which `pcan_dll.hardware_firewall_installed()` checks through a mark only code allowed to use ctypes can set. Every reset is audited.
- **Structural tests hold it to the plugin.** Only the plugin references the reset, nothing in `src/` imports the plugin, and a subprocess test shows the reset refused, with the kill still latched, in a process without the firewall.

**Hotkey:** Ctrl+Alt+K, a global Windows hotkey (`RegisterHotKey` via ctypes, no dependency), so it works when the terminal isn't focused. It's built and tested in Phase 1 (`safety/hotkey.py`) and gets wired into polled sessions with the polled `drive` command in Phase 4. A polled session refuses to start if the hotkey can't be registered.

### 3.5 Motion interlock (rule 8)

- **Speed source:** a mapped broadcast speed fresher than 500 ms, otherwise Mode 01 PID 0x0D fresher than 1 s. Unknown or stale counts as moving.
- **Parked-only requests:** snapshot, identify, and discovery requests are re-checked against speed = 0 before every request, not just at start.
- **While moving:** only the logging profile's request set (fixed when the session starts) and interlock probes pass.
- **Interlock probes:** Mode 01 PIDs 0x0D (speed), 0x0C (RPM), and 0x42 (module voltage), to 0x7DF or 0x7E0, at most 2/s. These are how the interlock learns speed before broadcast speed is mapped. *(open)*

### 3.6 Battery guard (rule 9)

Engine off means RPM reads 0, or RPM is unknown (the conservative choice). With the engine off, snapshot and discovery stop if module voltage is below 12.0 V or unknown.

### 3.7 STN transport (rule 4): what the manual allows

From the OBDLink Family Reference and Programming Manual (Rev F, Aug 2025) and the ELM327 datasheet:

- **The adapter's parser is permissive.** It ignores case, spaces, and control characters, and a backspace edits its buffer. Any line of only hex digits is sent to the vehicle. A bare CR repeats the last command, including OBD requests.
  - So `stn_port` sends only printable ASCII from a fixed character set plus one CR.
  - It normalizes each command (strip spaces, uppercase) and requires a full match against one allowlisted pattern.
  - It never sends an empty line or a hex-only line. Hex lines are built only by the gate from typed requests.
- **Bootloader window:** after `ATZ`/`ATWS` (and after any Bluetooth reconnect or wake, which resets the adapter), send nothing until the banner and `>` arrive. A binary packet in that window enters firmware update.
- **Allowed** (full match only):
  - format: `ATE0`, `ATL0`, `ATS0`, `ATH1`, `ATD1`, `ATCAF0/1`
  - protocol: `STP` from a fixed list (HS-CAN 11-bit 500 k; K-line presets without autoinit for monitoring), `ATM0`
  - status: `STPR`, `STPRS`, `STPBRR`, `ATPPS`, `ATCS`
  - identity: `STI`, `STIX`, `STDI`, `STDIX`, `STMFR`, `STSN`
  - voltage: `STVR`, `STVRX`, `ATRV`
  - silent mode: `STCMM 0`, `STCMM 2`
  - receive filters and monitors: `STM`, `STMA`
  - quieting: `STPC`, `ATSW 00`, `STPPMC`
  - `ATZ`/`ATWS`, only through the reset routine that waits for the prompt
- **Denied:** everything else. That includes:
  - arbitrary frame transmit: `STPX`, `ATRTR`, `STPO`
  - init: `ATSI`, `ATFI`, `STIFI`, `ATBI`
  - automatic protocol search, which sends `01 00` (`ATSP`, `ATTP`)
  - periodic messages and keep-alives: `STPPMA`, `ATWM`, `ATSW` other than 00
  - silent-mode defeat: `STCMM 1`, `ATCSM 0`
  - all NVM/OTP writes: `ATPP`, `ATSD`, `AT@3`, `STSAVCAL`, `STVCAL`, `ATCV`, `STWBR`, `STRSTNVM`, all `STSL*` PowerSave and `STBT*` Bluetooth setters
  - baud changes, sleep, GPIO, batch mode
  - header and flow-control shaping, except the gate's own `ATSH` to an approved address

- **The port never leaves the adapter:** `open_adapter(port, auditor=...)` opens the COM port inside `StnAdapter` and returns only the adapter, so nothing outside the safety core holds a raw, writable port. A reachability test walks every attribute path from the adapter to the port.
- **Nothing else opens the port by name:** on Windows the built-in `open("COM5")` or `os.open(r"\\.\COM5")` reaches the adapter without pyserial. The first import of the safety core installs a process-wide audit hook (`safety/serial_guard.py`) that refuses (audited) opening a serial device path with `open()`, `os.open()`, or `_winapi.CreateFile`: a COM or AUX name anywhere in the path, or GLOBALROOT. pyserial opens its port with `CreateFileW` through ctypes, which raises no such event, so the adapter link is unaffected (review finding P2).
- **Routines:** reset (`ATZ`/`ATWS`) and monitor (`STM`/`STMA`) commands are accepted only inside the routines that enforce their rules. Monitoring is stopped with a backspace: it stops a running monitor, and on an idle adapter it edits an empty line. Confirm this on the real MX+ in Phase 3.

**Rule 4 conflict: silent mode can't be read back.** Neither `STCMM` nor `ATCSM` has a query form. Proposed verification, all required before every monitor start:
1. `ATPPS` shows PP 21 at its factory default (silent) or `FF`.
2. `STCMM 0` returns exactly `OK`, re-sent after every reset or reconnect and immediately before every `STM`/`STMA`.
3. `STPR` shows the fixed protocol, not automatic search.
4. A one-time bench test proves the adapter doesn't ACK (§12).

*(open)*

### 3.8 K-line polling conflict (Phase 7)

A K-line session needs an init sequence. Fast init and 5-baud init both end in StartCommunication (0x81), which isn't on the allowlist. Adapters then send tester present (0x3E) keep-alives, which are on the never-list.

**Proposal** (decided in Phase 7, not now; raised now because it affects the KDSS plan):
- Add 0x81 StartCommunication, and possibly 0x82 StopCommunication, as K-line-only session services.
- Disable keep-alives with `ATSW 00`, and keep requests under the P3 timeout (5 s) so the session stays up without 0x3E.
- K-line passive monitoring of Creader sessions uses a preset with no autoinit, so it transmits nothing.

### 3.9 The policy can't change at runtime

Every safety module ends with `freeze(__name__)` (`safety/_frozen.py`):

- **Classes are sealed.** Every class in the core has a sealing metaclass, including enums, protocols, exceptions, dataclasses and the ctypes structures. Once sealed, none of its attributes can be set or deleted. That closes method swaps, `__eq__`/`__hash__` changes that would move an ECU out of `SENSITIVE_KINDS`, and field swaps on `TPCANMsg` that would write a different ID from the one checked. Enum members can't change either, since a member's name is its hash. `SealedType` is its own metaclass, so the guards can't be swapped out.
- **Modules are frozen.** No name can be rebound or deleted. The package still lets the import system bind each submodule once, and its search path becomes a tuple.
- **Tables are immutable:** frozensets, tuples and `MappingProxyType`, never a list, dict or set.
- **Instances keep their state in private slots.** They have no `__dict__` and no public writable attributes. An `Exchange` is read-only outside the gate.
- **Instances can't be initialized again.** A frozen dataclass sets its fields with `object.__setattr__`, so running its `__init__` again would rewrite it in place. Sealing wraps each slotted class's own `__init__` to refuse an instance that already holds state, so the approved ECU entry, a session's reader, and the kill switch can't be rewritten that way. A frozen module refuses `__init__` too (review finding P1).
- **Refusals are audited:** a refused change is recorded as `safety_core_frozen` (transport `core`).

What pure Python can't block at runtime (`object.__setattr__` or `type.__setattr__` called directly, frames, gc, ctypes, builtins, pickle) is banned in `src/` by the deliberate-routes test (§7). The static change test catches any assignment, deletion, in-place change, `setattr`, monkeypatch or `mock.patch` aimed at a safety module, class or module-level object, from `src/` and from the tests. Tests may change only instances they made.

## 4. Capture and storage

This section describes what Phase 2 built. Later phases add to it where it says so.

- **Reader:** the safety core's reader, owned by the core. In Phase 2, `drive`'s capture loop pumps it on the main thread, with no thread of its own, and hands what it reads to the recorder as plain records (`capture/convert.py`). Later phases add consumers in this order: kill-switch monitor → gate response matcher → ISO-TP/KWP conversation tracker → live decoder.
- **`lasto drive` (Phase 2, passive, `operations/drive.py`):** one run is one process holding one channel open, listen-only, in armed mode.
  - **Sessions:** the first frame starts a session. It ends after 60 s without a frame (key off), at its last frame, and the next frame starts a new one.
  - **Loop:** on the main thread, it reads the channel every 10 ms and hands each frame to the recorder. Once a second it indexes the audit log, copies the channel's audit events (listen-only rechecks, distrust, reopen, refusals) into the events table, writes the live feed, and checks for the stop file. A channel status while no session is open is a run event.
  - **Stopping:** Ctrl+C, Ctrl+Break, the stop file (`capture.stop` in the data folder), the time limit, a storage failure, or the safety core giving up on the channel all take one path: read the channel once more, close the session at its last frame, close the channel, index the rest of the audit log, and end the run with the reason. Ctrl+C and Ctrl+Break are caught as stop requests, and the previous handlers are put back afterwards.
  - **Startup:** the capture lock, then recovery, then the run's own audit file (`audit/<UTC time>-pid<pid>-<random>.jsonl`, always a new file), the run row with the safety configuration snapshot, the disk check (retention, below), and the channel. What the channel reported when it opened (adapter, PCAN-Basic version, firmware) is recorded on the run from its `session_opened` audit record.
  - **Simulator:** the default, on its own clock, as fast as the capture can go: `--seconds` is simulated seconds (60 by default). On the truck, `--seconds` is an optional time limit, and Windows is kept from idle sleep.
  - **Throughput:** the simulator records 2,000 frames a second about 30 times faster than real time on the desktop, simulator overhead included.
- **Raw log (`storage/segments.py`):** candump text (`(1727380000.123456) can0 7E0#0201000000000000`), written as one complete zstd frame (checksum on) per second of traffic, then flush and `fsync`. Error frames are candump error frames (`CAN_ERR_FLAG` with PCAN's error type); channel status goes to the events table instead.
  - **Files:** `sessions/<date>/<session ID>/seg-0001.candump.zst`, a new one at every session start and every hour. A segment file is never opened if it exists, so nothing is ever appended to a file that might end in a torn frame.
  - **Index:** a skippable zstd frame before each second's data frame holds a format tag, the second's number in the session, its first and last hardware timestamps, the frame count, and the data frame's length. `zstd -d` skips it, so a segment decompresses to plain candump text that `can-utils` reads.
  - **Reading:** frame by frame; the first truncated or corrupt frame ends what's trusted. The `seconds` table records each second's place, so one second is read without the rest.
- **Recorder (`capture/recorder.py`):** batches a session's frames by whole seconds of hardware time from its first frame. A second is written when the next one starts, or after 1.5 s of host time if the bus goes quiet. Seconds only move forward (review finding L9): a frame stamped at or before a second already written, as a timestamp that steps back after a replug might be, goes into the next second rather than numbering one twice. Each second goes to its segment and is fsynced, then one transaction records its place, each ID's rollup (`capture/rollups.py`), the running totals, and a clock anchor every minute.
- **Storage failures (review finding L8):** nothing drained is dropped silently.
  - A second stays in memory until it commits. A failed segment write is cut back off the file (or, if even that fails, the file takes nothing more), and a failed commit leaves the second on disk, so either can be tried again.
  - After a failure the recorder stops writing, and `drive` stops the run (end reason `storage_error`). It reads the channel once more, and closing the session is the one more try: each second goes to its segment, and commits go on while they work. Once a commit fails, later seconds go to the segment only, so the database still indexes the start of the file.
  - A session with anything left uncommitted stays open, and the next capture's recovery indexes what reached disk. A `storage_error` run event records the error, the sessions left open, the frames on disk that recovery will index, and the frames and events that couldn't be written. The channel always closes.
- **SQLite:** WAL mode with `synchronous=FULL` and one batched commit per second.
  - Two databases in the data folder (§14.3): `capture.sqlite`, written only by capture processes, and `workbench.sqlite` for what the GUI and the CLI's analysis write (vehicles, notes, tags, and later mapping work). Each carries its own `application_id` and schema version; lasto refuses another kind of database, or a newer one, and readers open read-only.
  - Runs (one per `lasto drive` process), sessions, and vehicles have random UUIDs. Every path in a database is relative to the data folder, with forward slashes.
  - The data folder comes from `--data`, then `LASTO_DATA`, then `%LOCALAPPDATA%\lasto`, and one that names a device (a serial port, the device namespace, or another reserved name) is refused, since SQLite opens its files where the serial guard can't see.
  - **Capture database, schema 1 (built):** `runs` (one per `lasto drive` process: channel, adapter, PCAN-Basic version, the safety configuration snapshot, its audit file), `sessions` (vehicle, mode, state, end reason, time base, totals), `segments`, `seconds` (each second's place in its segment), `anchors`, `events` (channel status, read errors, the channel's audit events, recovery, live feed failures), `audit` (an index of the run's JSON Lines audit log, which stays the record of truth), and `id_seconds` (each ID's rollup for each second: count, first and last seen, smallest and largest gap, DLC range, which data bits changed, last data; per-ID stats for a session are computed from them). Schema 1 stays open to change until the first live capture writes to it; after that, every change is a migration.
  - **Workbench database, schema 1 (built):** `vehicles` (a default 2006 GX470 is created on the first capture), `session_notes`, `session_tags`.
  - **Later phases add:** conversations, signals and samples, DTCs, freeze frames, readiness, Mode 06, vehicle info, definitions and verification history, mapping sessions and reference values, discovery results.
- **Timestamps:** a session's time base is its first frame's hardware timestamp and the host UTC when it arrived, recorded as its first clock anchor (hardware timestamp, host UTC, host monotonic). Another anchor follows every 60 s, for drift. Every frame's time is its hardware timestamp in integer microseconds, and its UTC is derived through the time base, so the two convert back exactly. The laptop is offline, so UTC is only as good as its clock.
- **Recovery:** sessions stay marked open until they close cleanly. On the next start, under the capture lock:
  - Each open session's segments are read from where their index ends. Every complete second found there is indexed, rollups included, as the recorder would have. The database knows each segment before its file exists, so none is missed.
  - A torn tail is moved to a `.torn` file beside its segment, so the segment reads cleanly and no byte that reached the disk is thrown away. A segment shorter than its index is reported and left alone.
  - The session is marked recovered (end reason `interrupted`) at its last indexed frame, with a `recovered` event saying what was found.
  - A run left unended has its audit log indexed to the end, and ends at the last time it's known to have reached.
- **Capture lock:** a capture process holds an OS lock on `capture.lock` in the data folder for its whole life, so only one capture writes the capture database, and only it recovers. Windows releases the lock when the process ends, however it ends. Anyone can ask whether a capture is running (§14.3).
- **Live feed:** once a second the capture process writes its status (run, session, state, frame counts, frames per second, the last bus status, and a heartbeat) to `live.sqlite`, with synchronous NORMAL. A failure there is recorded as a run event when it starts and when it clears, and never stops the capture. Decoded values and alarms join it in Phase 4.
- **Keep awake:** during a live capture, `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` (`operations/keep_awake.py`) prevents idle sleep only. The display may still turn off, and closing the lid still follows the Windows power settings, which stay yours to set. The request lasts only as long as the thread that made it, so only the main thread, which lives for the whole capture, may make it or clear it. It's the one use of ctypes outside the safety core, an approved exemption in the structural tests, and a structural test holds it to that one function (review finding L10): its only imports are the ones it needs, it calls `WinDLL("kernel32")` once and takes only `SetThreadExecutionState` from it, and it takes nothing else from ctypes but `c_uint32`. Edits to it ask first, like edits to the safety core.
- **Storage, measured at the truck (step D, 2026-10-01):** raw segments take 13–16 MB per hour of traffic, about 6.7 bytes a frame with the engine off and 8.7 while driving, when more bits change. The capture database took 434 KB for about 6.5 minutes of capture, about 4 MB an hour. So a driving hour costs about 17–20 MB. At about 1.5 hours of driving a week, that's roughly 30 MB a week and 1.5 GB a year. With the key off the bus is silent, so an armed capture costs nothing while parked.
- **Retention (owner's decisions, 2026-10-01; `storage/retention.py`):**
  - **Through Phase 5:** keep everything. Early drives are the reference for mapping and decoding.
  - **After that:** a budget of 20 GB for the data folder. Over it, raw segments go oldest first, down to 90% of the budget. Never the newest 30 days, and never a session that's open, tagged, has notes, or is referenced by mapping work. The database rows stay, so `lasto log --bus`, events, and the audit index still work for old drives.
  - **Deletion:** only by command or in the GUI, under the capture lock, in small batches, and audited. Never automatically, and never during `drive`. Not built yet (§13).
  - **At `drive` start:** the free space on the data folder's disk and the folder's use of its budget. Below 2 GB free, or over the budget, it adds a warning line and a `disk_warning` run event. A warning never stops a capture: a disk that actually fills is a storage failure, which the capture handles (L8). Sizes are GiB, as Windows shows them.

**The truck's bus, from step D (2026-09-30 to 10-01).** Provisional: what an ID carries is a hypothesis until Phase 3 mapping and Phase 5 decoding confirm it.

- **Key off:** the bus is silent. **Key on:** 17 IDs at about 520–540 frames a second, roughly 11–12% of the 500 kbit/s bus. Listen-only held on every run, with no error frames, overruns, or status events, and the 60-second silence end closed a session on its own (`bus_silent`).
- **None of the 17 IDs is a diagnostic ID** (0x7DF, 0x7E0 to 0x7EF).
- **"Controller activated" messages:** the driver sent none in step D's four runs, so listen-only re-checks stayed quiet.
- **Vehicle speed** isn't on the IDs other Toyotas use (0x0B4, or 0x0AA for wheel speeds). Until a broadcast speed is found and verified, Phase 4's motion interlock polls PID 0x0D.
- IDs with identical frame counts probably share a sender: 020, 022, 023 and 025; 223 and 224; 2C1, 2D0 and 2D2.

| ID | Rate | DLC | What step D saw |
|---|---|---|---|
| 020 | 75–78 Hz | 3 | Never changes |
| 022, 023 | 75–78 Hz | 8, 7 | Two 10-bit fields in bytes 0–3 change while driving; fewer bits with the engine off |
| 025 | 75–78 Hz | 8 | The steering angle sensor (below) |
| 223, 224 | 38–39 Hz | 8 | A few bits; 224's bytes 4–5 |
| 2C1, 2D0 | 31.5 Hz | 8 | Static with the engine off, changing with it running |
| 2C4 | 42.4 Hz | 8 | Static with the engine off, changing with it running. Bytes 0–1 fit other Toyotas' engine RPM |
| 2D2 | 31.5 Hz | 1 | Never changes |
| 3D0 | 4 Hz | 1 | The low 6 bits change while driving |
| 420, 423, 4C1, 4C3, 4C6, 4C7 | about 1 Hz | 8 or 1 | Never change: probably status or keep-alive frames |

**0x025, the steering angle sensor (provisional).** The owner's analysis of session e20aa986, read with `lasto log SESSION --id 025` and decoded with the layout other Toyotas use:
- **Angle:** signed 12 bits, 1.5° per bit, positive to the left. Byte 0's low nibble holds the angle's top 4 bits, and byte 1 its low 8 bits. It followed the owner's timeline exactly: +555° at full left lock, −565.5° at full right, and about −9° with the wheels straight.
- **Byte 0, bit 4:** set only for about 0.14 s after power-up and for the last 25 ms before power-down.
- **Bytes 2–3:** a constant `0F F9`.
- **Bytes 4–6:** near 0x80 at rest; they move only while the wheel turns.
- **Byte 7:** changes on nearly every frame, likely a checksum.
- **At power-up:** a burst of about 65 frames, 0.25 ms apart.
- **So the sensor broadcasts a live, sensible angle** (C1777 in §13).

## 5. Decoding and mapping

- **Definitions:** YAML under `definitions/` (target ECU, request ID or K-line address, service, identifier, byte positions, formula, units, source, verification status and history). Formulas go through a small AST-allowlist evaluator; `eval` is never used. The seed definitions are marked unverified, with sources (for example, ATF temperature via 0x21 0xD9).
- **Broadcast definitions:** DBC under `dbc/` via cantools (Phase 5). Maximum raw codes such as the steering angle's 1150.875° are modeled as invalid markers.
- **Creader mapping (Phase 3):** passive reassembly of ISO-TP conversations on CAN (and KWP conversations on the K-line once the MX+ monitor exists). `lasto map` writes a CSV template of the requested values. The solver tests byte positions, lengths, signedness, endianness, and bit fields against your reference values, fits scale and offset, and ranks the candidates.

## 6. Simulator (`src/lasto/sim/`)

- **`FakePcanDll`** implements the `CAN_*` functions the bindings use: listen-only semantics, channel condition, status codes, error frames, queue overrun, and unplug. Fault injection therefore exercises the real kill switch.
- **Vehicle model:**
  - four CAN nodes broadcasting at realistic rates
  - an ECM with 0x7E0/0x7E1 answering Mode 01/03/06/07/09/0A and 0x21 local IDs, with NRC behavior including 0x78 and 0x21
  - a steering angle stuck at 1150.875°
  - K-line ECUs behind `FakeStnPort`
  - a simulated Creader that holds conversations for mapping tests
- **Scenarios (Phase 2):**
  - a key switch: key off silences every broadcast, and key on resumes them on their original schedule
  - actions scheduled at a bus time among the frames (`SimBus.call_at`): a key switch, an unplug, Ctrl+C, a stop file
  - background traffic at 100 Hz per ID from 0x300 up, never a diagnostic ID, for the 2,000 frames a second check
  - an unplugged adapter hears nothing
- **Labeling:** every broadcast ID and local ID is labeled fictional until a real capture replaces it.
- **Violation recorder:** any frame written while listen-only is on, any frame on an ID other than 0x7DF and the approved 0x7E0 (the oracle is exactly as strict as the policy, kept by hand), and any denied service or STN command is recorded even if the code under test catches the exception. A pytest plugin fails the run at session end if anything was recorded.

## 7. How the tests prove the rules

- **Unit tests** for every allowlist decision: IDs, services, the never-list, addressing modes, FC conditions, STN commands (including normalization tricks: case, spaces, backspace, hex-only lines, empty lines).
- **Hypothesis** fuzzes CAN IDs, payloads, addressing, typed request sequences, and injected faults through the gate against `FakePcanDll`, asserting that every frame reaching `CAN_Write` satisfies the policy. It fuzzes the `Writer` directly too, around the gate. It does the same for strings reaching the fake serial port.
- **Structural tests** read the source:
  - only `safety/pcan_active.py` names `CAN_Write`, and only the gate calls the `Writer`: the session hands the write function from `open_active` to `Gate(...)` and does nothing else with it, the gate calls it once, in `_transmit`, only `Writer._write` calls `CAN_Write`, and no other safety module names a writer. A profile hook confirms it at runtime across the fuzzed simulated sessions.
  - only `safety/stn_port.py` opens or writes serial, and no literal in `src/` names a serial device path
  - nothing outside `lasto.safety` imports the DLL bindings or pyserial
  - the safety core imports nothing from `lasto` outside itself, so settings, storage, and every later layer can never feed it
  - code outside the safety core uses only its public API. Names are resolved through every re-export and attribute chain back to the module that defines them (`tests/scan.py`).
  - no code in `src/` uses a deliberate route around the core's guards (ctypes and `_ctypes`, gc, inspect, importlib, builtins, pickle and other code-rebuilding modules, imported directly or reached through another lasto module's import of them, as `from lasto.safety.pcan_dll import ctypes` or `keep_awake.ctypes` would (review finding L11; `serial` is followed the same way), `sys.modules`, `vars`/`globals`/`setattr`/`delattr` and the like, or any of them taken as a value (`s = setattr`), `getattr` with a private or computed name, `operator.attrgetter` and `methodcaller`, `pkgutil.resolve_name`, attribute-guard dunders and `mro()`, function defaults, frames and tracebacks, trace and import hooks, an explicit `__init__` call other than `super().__init__()`, an assignment, deletion, or in-place change inside an imported module such as `time.monotonic = f`, `os.environ[key] = value`, or `os.environ.update(...)`, or, outside the safety core, another object's private attributes), except an explicit exemption list in `tests/safety/test_structure_reach.py`. Each new exemption is its own commit, approved by the owner.
  - nothing in `src/` or the tests changes a safety module, class or module-level object (§3.9), and the safety core has no `global` statements
  - every refusal is raised by `audit.refuse()`, which records it first (rule 11): any other `raise` in the safety core fails, whatever its type or however it is built, except a bare re-raise and a short owner-approved list of raises that aren't refusals (re-raising an audit log's or a kill listener's own failure, and ISO-TP parse errors, which the policy refuses and the gate turns into a kill). The safety core has no `assert`.
- **Runtime freeze tests** (`tests/safety/test_frozen.py`) try to change every name in every safety module, every attribute of every safety class, and every enum member, and expect each attempt to be refused and audited. They also check that every module-level value is immutable and that every instance keeps its state in private slots.
  - passive modules contain no write reference
  - every argparse parser has `allow_abbrev=False`
- **Reachability tests** walk every attribute path from a live polled session: the raw `CAN_Write` is reachable only inside the `Writer`, and the reader's channel can't write.
- **Hardware firewall** (`lasto.sim.pytest_plugin`, loaded by the test command): loading `PCANBasic.dll` or opening a real serial port raises, through pyserial or by name (its own audit hook, separate from the safety core's).
- **Coverage:** `uv run pytest` measures branch coverage on all of lasto and fails below each package's gate (`tests/coverage_gates.py`): 100 % on the safety core, and 90 % on capture and storage, their truncation and crash-recovery paths included. Everything else, CLI rendering among it, is reported only.
- **Guardrail tests:** a table of commands and tool calls through `.claude/hooks/hardware_guard.py`.

## 8. File layout

```
src/lasto/
  __init__.py  __main__.py  cli.py  config.py
  safety/                    # SAFETY CORE: ask rule, own commits, 100% branch coverage
    __init__.py              # public API: open_passive(), open_polled(), request builders
    policy.py                # service allowlist, never-list, STN command allowlist (frozen)
    ecus.py                  # approved CAN IDs and K-line addresses with kind and evidence
    requests.py  isotp.py  gate.py  ratelimit.py  killswitch.py  interlocks.py  audit.py  errors.py
    pcan_dll.py              # ctypes: load_readonly(), the read-only binding
    pcan_active.py           # open_active() -> (ActiveChannel, Writer); the one CAN_Write lookup
    exchange.py              # Exchange and ExchangeState, the public result of a request
    _frozen.py               # freeze(): sealed classes, frozen modules (§3.9)
    pcan_passive.py  stn_port.py  reader.py
    serial_guard.py          # audit hook: nothing else in the process opens a serial port by name
  records.py                 # plain Frame and BusEvent types; only capture converts from the core's types
  data/safety_config.json    # snapshot of the safety configuration, proved equal to the core by a test
  services/                  # hardware-free application logic; the CLI and the GUI both call it (§14.4)
    safety_config.py  data.py  sessions.py      # the snapshot, the data folder, sessions for lasto log
  operations/                # hardware-facing work; the only code outside the core that opens its sessions
    drive.py                 # lasto drive: passive capture in armed mode
    keep_awake.py            # SetThreadExecutionState: the one ctypes use outside the core (exemption)
  capture/                   # convert.py (core types to records), recorder.py, rollups.py, recovery.py
  storage/                   # root.py (data folder), database.py, capture_db.py, workbench_db.py, ids.py,
                             # segments.py, audit_index.py, capture_lock.py, live_db.py
  protocol/  decode/  mapping/  polling/  snapshot/  identify/  discover/  analysis/  report/  export/  view/
  sim/                       # FakePcanDll, FakeStnPort, vehicle model and scenarios, ECUs, Creader, violations
definitions/  dbc/
tests/  (conftest.py, safety/, sim/, capture/, storage/, services/, operations/, guardrails/, ...)
docs/architecture.md
.claude/settings.json  .claude/hooks/hardware_guard.py  CLAUDE.md
```

## 9. CLI

- **Entry point:** one command, `lasto`, with subcommands `drive`, `map`, `snapshot`, `identify`, `log`, `view`, `discover`, `decode`, `report`, `export`, `verify`.
- **Simulator by default:** real hardware needs `--live` plus `--channel PCAN_USBBUSn` or `--port COMn`, typed every time. Config holds no default channel.
- **No abbreviations:** every parser sets `allow_abbrev=False`, so `--liv` can't mean `--live`.
- **Drive modes:** `drive` alone is passive; `drive --profile NAME` is polled (Phase 4).
- **`lasto drive [--live --channel PCAN_USBBUSn] [--seconds N] [--data DIR]` (Phase 2):** passive capture in armed mode (§4). The simulator runs `--seconds` simulated seconds (60 by default); on the truck `--seconds` is an optional time limit. `--port` and `--profile` are refused with a message until the phases that bring them. Before anything else, it sends held refusals to `audit/held.jsonl` in the data folder (B2). It exits with 1 if another capture is running, if the safety core refuses the channel, if the channel is lost, or if storage fails.
- **`lasto log [SESSION|last] [--bus] [--data DIR]` (Phase 2):** without a session, every session newest first: start (UTC), length, state, end reason, frames, IDs, stored size, and MB per hour. With a session (its ID, its first characters, or `last`), the session's run and adapter, its events, the run's events while no session was open, and the run's audit log. `--bus` adds every CAN ID: frames, rate, mean period, smallest and largest gap, DLC, which data bits changed (8 bytes, hex), and first and last seen. `--id HEX [--from S] [--to S]` (added after step D, to read 0x025 for C1777) prints one ID's raw frames over time instead: seconds after the session's first frame by the adapter's clock, UTC, DLC, and data bytes, read from the segments for only the seconds the per-second rollups say hold that ID. Error frames are left out, and a second that can't be read is named. It says so when a capture is running, from the capture lock and the live feed.
- **Data folder:** `--data`, then `LASTO_DATA`, then `%LOCALAPPDATA%\lasto` (§4). The tests always use a temporary one.
- **Raw console:** exists only as `lasto sim console`, which has no `--live` option.
- **GUI (Phase 9):** `lasto gui` starts the local web GUI and opens the browser (§14). The CLI keeps every capability; the GUI is another front end over the same service layer.

*(open: what `verify` should do)*

## 10. Dependencies and tooling

Each dependency arrives in the phase that first needs it, pinned exactly with hashes in a lock file.

| Phase | Package | Version | Notes |
|---|---|---|---|
| dev | pytest, pytest-cov, hypothesis | 9.1.1, 7.1.0, 6.168.1 | hypothesis now ships a native wheel; fine on win_amd64 |
| 1 | *(none)* | | safety core, simulator, CLI skeleton are standard library (ctypes, sqlite3, argparse) |
| 2 | zstandard | 0.25.0 | |
| 3 | PyYAML | 6.0.3 | |
| 4 | rich | 15.0.0 | Live view with a built-in big-digit font from `█▀▄` (CP437-safe); Textual is heavier and not needed |
| 5 | cantools | 44.1.0 | pulls in python-can 4.6.1, used only for DBC parsing, never for bus access; pin exactly (frequent majors) |
| 7 | pyserial | 3.5 | |
| 8 | matplotlib | 3.11.2 | brings numpy 2.5.3, which needs an x86-64-v2 CPU (SSE4.2) |
| 8 | pyarrow | 25.0.1 | Parquet; needs SSE4.2, ~87 MB. polars is ~180-355 MB and needs AVX2 or a compat runtime, so not recommended |

**Not used:**
- python-can for bus access (§3.1).
- can-isotp: requests are always single-frame and the receive side is small, so a minimal ISO-TP inside the safety core is easier to prove than wrapping a library that transmits on its own.

**Tooling:** uv 0.12.19 manages `.venv` from `uv.lock`, which pins every dependency, transitive ones included, with sha256 hashes. `.python-version` pins Python 3.13. `uv sync --locked` builds the environment, and `uv run pytest` runs the tests.

Offline install on the truck laptop:
1. `uv export` a hashed requirements file.
2. `pip download` a win_amd64 wheelhouse.
3. Copy it with uv and a Python archive to the laptop.
4. Install with `--offline --no-index --require-hashes`.

*(open: the offline install)*

## 11. What's built

Safety core for both transports, `FakePcanDll`, `FakeStnPort`, the minimal vehicle model, the violation plugin, the hardware firewall, the structural tests, Hypothesis fuzzing, 100 % branch coverage, and a CLI skeleton with `--live` gating. No feature code.

The STN transport in Phase 1 has **no path that puts anything on the vehicle bus**. It covers reset, identification, voltage reads, CAN silent monitoring, and K-line passive monitoring (needed in Phase 3). The only bus-facing use it would ever have is K-line polling, which needs the Phase 7 decision on 0x81 anyway. Until then every hex-only request line is denied, and the tests prove it.

**Phase 2 (approved 2026-10-01, after the step D live tests at the truck; results in §4):** passive capture and storage (§4), `lasto drive` and `lasto log` (§9), and the §14.8 prerequisites for the GUI. Two safety core changes, each its own commit: on real hardware a session refuses an audit log that doesn't keep its records on disk (finding L5, §3.3), and held audit records go to a file as they're held. One new scanner exemption, its own commit: ctypes in `operations/keep_awake.py`. The bench test is optional for the passive tests (owner's decision, 2026-09-28). The independent Phase 2 review (2026-09-29) found nothing High or Medium, and six Lows: L8 to L11 are fixed (§4, §7), and L6 and L7, both in the safety core, are Phase 4 blockers in §13.

## 12. Verification you run (never Claude)

- **Bench test, PCAN listen-only:** the MX+ is the only transmitter and the PCAN listens in passive mode. With nothing to acknowledge its frames, the MX+ reports CAN errors (no ACK) and retransmits. If the PCAN were ACKing, the frames would go through cleanly. This proves the no-ACK behavior PEAK doesn't promise in writing.
- **Bench test, MX+ silent mode:** reversed. PCAN-View (PEAK's free tool) on the PCAN is the only transmitter, and the MX+ monitors after `STCMM 0`. PCAN-View should show ACK errors and a rising error counter.
- **At the truck:** the live commands each phase specifies.

**Minimum bench parts** (the two adapters test each other, so no extra CAN device is needed):

| Part | Purpose | Approx. cost |
|---|---|---|
| OBD-II (J1962) female socket with screw-terminal breakout | lets both adapters (or the 3-way splitter) plug in | $10-20 |
| 2 × 120 Ω resistors (¼ W) | terminate CAN-H (pin 6) to CAN-L (pin 14) at both ends, 60 Ω total | <$1 |
| 12 V DC supply, ≥ 1 A (a wall adapter or a small 12 V battery) with an inline 1-2 A fuse | powers the MX+ from pin 16 (+) and pins 4/5 (ground). The PCAN-USB is powered by USB | $10-15 |
| Hookup wire, and a multimeter to confirm ~60 Ω across pins 6-14 | wiring and a check | on hand |

The bench tests are the only time either adapter transmits on purpose, and only on the bench bus. Frames to use and exact steps come with the test when you're ready.

## 13. Carried into later phases

| Phase | Item |
|---|---|
| 3 | **C1777 (KDSS steering sensor signal):** step D shows the steering angle sensor healthy on the bus. 0x025 tracks the wheel from lock to lock (§4), so C1777, and the VSC ECU's reading stuck at 1150.875° (the invalid 0x7FF), are now expected to be downstream of a healthy sensor: in how the VSC ECU receives or uses the angle. Next: Phase 3's Creader captures of the VSC and KDSS data streams, set against 0x025 at the same moments. |
| 3 | **BLOCKER before any MX+ code touches hardware: the serial guard (review finding P2):** safety filters stopped both verification reviews while they analyzed the serial guard, so the P2 fix hasn't been independently verified. Nothing opens a serial port before Phase 3. First: <ul><li>**An independent review of the serial guard:** its path matching (`safety/serial_guard.py`), the test firewall's separate hook (`lasto.sim.pytest_plugin`), and the structural rule on serial device paths (`tests/safety/test_structure.py`). A later check already found two gaps in the path matching for that review to close: a device-interface path, such as `\\?\USB#VID_…` or a Bluetooth `\\?\BTHENUM#…` path, names a port with no COM name in it; and a port name written right after a drive letter (`C:COM5`) isn't recognized, because the drive letter and the name are read as one part. Phase 2 found a third: SQLite opens its database files itself, raising a `sqlite3.connect` audit event rather than `open`, so the hook never sees a database path at all, and a path that names a serial port would reach it. The data folder refuses device names on its own (`lasto.storage.root`), but the guard doesn't cover SQLite.</li><li>**The guard installed before any lasto code can open a port:** the audit hook goes in when the safety core is first imported. *Settled in Phase 2 (step A2):* `cli.main` imports the safety core as soon as it has parsed the command line, before it dispatches any command but `gui` (§14.4), and a subprocess test proves it for `drive`, `log` and `map`. For the review to confirm: that nothing lasto runs before that import can open a port (`lasto/__init__.py` and argument parsing open nothing), and that the GUI's own hook covers its process (§14.1).</li><li>**The safety core's opens of a path the caller names (Phase 2 review):** `JsonlAuditSink(path)` has always opened one, and B2 added a second, `audit.hold_on_disk(path)`. Both go through `open()`, which the guard's hook sees, and `drive` passes paths inside the data folder, which refuses device names. JSON Lines records can't carry the CR an STN adapter needs to run a command, since `json.dumps` escapes it. The review should still cover both. **Proposed fix:** after opening, refuse (audited) a file that `os.fstat` doesn't report as a regular file, so neither can land on NUL or a COM port whatever the guard's name matching misses. The sink's half of this is the Phase 4 L7 row.</li></ul> |
| 3 | **Creader captures confirm:** 0x7E0 and any new request IDs, the padding byte, and the trimmed manufacturer service list. |
| 3 | **MX+ checks:** stopping a monitor with a backspace, and silent monitoring on the real adapter. |
| 3 | **Bootloader window (review finding E, deferred here):** the rule in §3.7 isn't enforced in code yet. `reset()` sends `ATZ` as soon as a connection opens, and again when retried after a prompt timeout. Settle, and verify on the real MX+ with the other MX+ checks: whether to wait for the banner and `>` after opening, reconnecting, or waking before sending anything; never re-send `ATZ` after a prompt timeout without first reading what the adapter sent; and how a Bluetooth reconnect or wake is detected. Nothing in Phase 1 or 2 opens the adapter on real hardware. |
| 3 | **Audit an adapter that won't open (review finding L4):** `open_adapter` attaches the caller's audit log only after the port opens. So a bad port name is held rather than written to that log, and an error opening the port isn't audited at all. Attach first, and audit an open failure, as PCAN open failures are (fix #7). |
| 3 | **Closing an STN adapter twice (the L3 bug, in `stn_port`):** `StnAdapter.close()` detaches the audit log from the shared refusal log on every call, so a second close takes the log away from another open session sharing it, and that session's refusals are held instead of written. Make a second close do nothing, as for L3. |
| 4 | **BLOCKER before the first polled live test:** wire the Ctrl+Alt+K hotkey into polled sessions, and refuse to start polling if it can't be registered. Verify it on the truck laptop's real Windows. **The hotkey thread ending counts as a kill trigger (review finding N9):** the message loop can end on its own (`GetMessageW` returns -1, `trip()` raises, any exception), and a session must never keep polling with a dead hotkey. So any exit other than `stop()` trips the kill switch (cause `hotkey_thread_ended`) and is audited, and the polled session also checks the thread is alive before each request. |
| 4 | **BLOCKER before the first polled live test: a kill listener that raises (review finding L2):** if `enter_listen_only()` raises, the polled session's kill listener (`_on_kill`) skips both closing the channel and its audit record. Treat an exception there as "listen-only not confirmed": close the channel, and audit. |
| 4 | **BLOCKER before the first polled live test: the durable-log check runs after the log is attached (Phase 2 review finding L6).** Both session openers (`session.py`) call `REFUSALS.attach(auditor)` before B1's `require_durable`. So an `audit_log_not_durable` refusal is written to the very log being refused, and attaching also moves every held record into that log, which takes them out of the exit report. If it's the only log attached, none of this reaches disk. Nothing in Phase 2 triggers it: `drive` always passes a JSON Lines log. **Fix:** run the real-hardware checks (the system clock, the durable log) before attaching. A safety core change. |
| 4 | **BLOCKER before the first polled live test: `require_durable` checks types, not what the log keeps or when (Phase 2 review finding L7).** `JsonlAuditSink(os.devnull)` passes: it's the exact type, and it discards everything. An `Auditor` on a fake clock passes too: audit records take their times from the auditor's own clock, and only the session's clock is checked. **Fix:** require the auditor's clock to be `SystemClock` itself, and refuse a sink file that `fstat` doesn't report as a regular file. The same `fstat` check in `hold_on_disk` is in the Phase 3 serial guard review. A safety core change. |
| 4 | **Broadcast IDs for the gate, before the first polled live test:** step D saw 17 broadcast IDs, none of them a diagnostic ID: 020, 022, 023, 025, 223, 224, 2C1, 2C4, 2D0, 2D2, 3D0, 420, 423, 4C1, 4C3, 4C6, 4C7 (§4). They go into the gate's set of IDs seen carrying broadcast traffic as a safety core change of its own, with your approval. Conditions step D didn't cover (4WD low, cruise, reverse, lights) may add more, so the set is checked against later captures too. |
| 4 | **Save and report on a kill:** the kill switch already stops and logs. Passive capture in Phase 2 has no kill: listen-only mode only records bus state. A polled capture's kill goes through the stop path Phase 2 built (read the channel once more, close the session at its last frame, close the channel, index the audit log, end the run with its reason), and the report of a kill arrives with polled capture. |
| 4 | **A kill needs a cause (review finding L1):** `trip(None)` doesn't latch. The listeners run and the kill is audited, but `tripped` stays False. Refuse a cause that isn't a non-empty string. |
| 4 | **Closing a polled session twice (review finding L3):** a second `PolledSession.close()` detaches the audit log again, so when sessions share an audit log, another open session's refusals are held instead of written to it. And after close, `session.reader.poll_once()` can still read a reopened channel; only `pump()` stops reading. Make a second close do nothing, and stop the reader on close. The same bug in `StnAdapter.close()` is a Phase 3 row. |
| 4 | **Logging profiles (review finding N10, owner's decision, cap confirmed):** a logging profile may contain only verified definitions from the definitions library, never raw identifiers (no `read_local_id`/`read_did` built by hand). Identifier sweeps stay discovery-only and parked-only. The gate refuses (audited), when the session opens, any profile that breaks these rules: <ul><li>**Manufacturer entries (services 0x21 and 0x22):** at most **16**, each a verified definition. At the gate's fastest pace (19.6 requests per second) that still refreshes every value more than once a second, and 16 identifiers are nowhere near a sweep of a 256-entry local-ID space.</li><li>**Standard Mode 01 PIDs:** fully defined by the OBD-II standard and already allowlisted, so they count as verified and aren't capped. A profile may include only PIDs the ECU reports as supported (its Mode 01 supported-PID bitmaps: PIDs 0x00, 0x20, 0x40 and so on, read while parked before the drive).</li></ul> |
| after 5 | **Retention pruning (approved 2026-10-01):** a command, and later the GUI, that brings the data folder back under its 20 GB budget (`storage/retention.py`), with the rules in §4: raw segments oldest first, never the newest 30 days or a session that's open, tagged, noted, or used by mapping work, database rows kept, under the capture lock, in small batches, audited. `drive` already warns when the folder is over budget. |
| 7 | **K-line polling:** decide on 0x81 StartCommunication and running without 0x3E keep-alives. |
| accepted (Low) | **A dataclass's `__setstate__` isn't guarded at runtime:** a frozen, slotted dataclass's `__setstate__` (which `copy` uses) rewrites an existing instance in place, as a second `__init__` did before P1. Guarding it would need the `__setstate__` attribute in `_frozen`, a new exemption. Accepted under the threat model: the scanner bans `__setstate__` in `src/`, so reaching it takes deliberate code, and since P1 the policy and the gate read an ECU's route, so a rewritten entry can't move a frame. |
| accepted (Low) | **The interlocks read an entry's own kind:** rule 10's discovery exclusion, and the check that a probe goes only to the engine, read `target.kind` rather than the route, because their tests use unapproved SRS and immobilizer entries, which have no route. Accepted under the threat model: the gate refuses an unapproved entry before the interlocks run, rewriting an approved one takes deliberate code the scanner bans, and the policy's frame check still allows only DTC reads to a sensitive ECU, from its route. |

## 14. Phase 9: local web GUI (planned)

Status: planned 2026-09-28, and approved the same day with the owner's notes, which are written into this section: zooming reaches full-resolution data (§14.2), the two ISO-TP parsers are held together by a differential test (§14.4), and MX+ monitoring stays CLI-only (§14.1). No GUI code is written before Phase 9. The changes this plan asks of Phases 2 to 8 (§14.8) start with Phase 2.

A desk-side tool for reviewing and working with the data: sessions, interactive charts, health history, the Creader mapping workflow, broadcast decoding, and the definitions library. It runs on the truck laptop, or on any machine with a copy of the data, and makes the project approachable for other owners. Logging stays on the CLI and runs unattended. The GUI adds nothing while driving and never interferes with a capture in progress.

### 14.1 Safety boundary (non-negotiable)

1. **No GUI action can cause a transmission.** GUI code never imports the safety core, the transports, or any hardware module, directly or indirectly. Enforced three ways:
   - **Static:** a test resolves the import closure of `lasto.gui` with `tests/scan.py` and fails on `lasto.safety`, `lasto.sim`, the hardware-facing operations (§14.4), `serial`, or `can.interface`/`can.interfaces`.
   - **Dynamic:** a subprocess starts the GUI app and fails if any of those modules is loaded.
   - **At runtime:** the GUI process installs its own audit hook, written separately from the safety core's like the test firewall. It refuses loading `PCANBasic.dll` and opening a serial device path, so neither a dependency nor a path someone types can reach hardware.
2. **The only hardware action is starting and stopping a passive capture.** The GUI launches `lasto drive` with no profile as a separate process and monitors it. One function builds the command, always `[python, "-m", "lasto", "drive", "--live", "--channel", PCAN_USBBUSn]`, with no shell. The channel is chosen in the start dialog every time; nothing remembers it, just as the CLI wants it typed. A test proves the builder can't produce `--profile`, `--port`, or any other argument, and `subprocess` appears only in that one module. The GUI never starts polled profiles, snapshots, identify, discovery, or anything else that transmits. MX+ K-line monitoring (Phase 3) stays CLI-only, since starting it writes commands to the adapter (owner's decision).
3. **Safety configuration isn't editable from the GUI.** Allowlists, approved ECU IDs, rate limits, and interlock thresholds stay code changes in the safety core. The GUI shows them read-only from a snapshot (§14.4), without importing the safety core. User settings (alarm thresholds, tire size, units) are a different thing: the safety core never reads them.
4. **Network exposure:**
   - **Address:** the GUI binds to 127.0.0.1 only, with no option to change it, on a port the OS picks.
   - **Host header:** it must be `127.0.0.1:<port>` or `localhost:<port>`, or the request is refused (DNS rebinding).
   - **Launch token:** `lasto gui` opens the browser with a one-time token in the URL, exchanged for an HttpOnly, SameSite=Strict cookie. Every request needs that cookie.
   - **CSRF:** every request that changes state needs a CSRF token, plus a same-origin `Origin`/`Sec-Fetch-Site`. GET and HEAD never change state.
   - **Headers:** no CORS headers at all. A Content-Security-Policy of `default-src 'self'` with no inline script, `frame-ancestors 'none'`, `nosniff`, and `no-referrer`.

   Every other page open in the same browser is treated as hostile.
5. **Offline:**
   - **Assets:** all JavaScript and CSS are vendored in the package. A test checks each vendored file against a manifest of pinned versions and SHA-256 hashes.
   - **Fonts:** pages use the system font stack, so there are no fonts to vendor.
   - **No network calls:** templates reference no other origin, and a test fails on any outbound connection from the GUI process. No CDNs, no telemetry.
6. **Capture always wins.** The GUI browses through read-only database connections and makes its own writes in short transactions. It never holds a lock that could stall a running capture, and it degrades rather than blocking one (§14.3).

### 14.2 Stack (approved)

- **Server:** Flask 3 (with Werkzeug, Jinja2 with autoescaping, MarkupSafe, itsdangerous, click, blinker), served by Waitress, a pure-Python WSGI server with no dependencies of its own that runs well on Windows. All are pure-Python wheels: nothing to compile, and no Node.js. Exact versions go into `uv.lock` when 9a starts.
  - Server-rendered pages keep the logic in Python services.
  - Flask is what most contributors know.
  - Waitress is made for real use, unlike Werkzeug's development server.
  - Considered and not proposed: the standard library's `http.server` (the security would all be hand-rolled), FastAPI or Starlette with uvicorn (async, more dependencies), and Bottle (small, but less maintained).
- **Interactivity:** htmx, one vendored file, for partial page updates, plus a few small hand-written JavaScript modules. No build step.
- **Charts:** uPlot, vendored, about 50 KB. It draws on a canvas, stays smooth with hundreds of thousands of points, and synchronizes the cursor across stacked charts, with zoom and pan. The byte and bit change heatmap is a small custom canvas script.
- **Downsampling:** LTTB on the server. A chart starts from the 1 Hz rollups (§14.3): at most 3,600 points per signal for an hour, cheap in pure Python on the old laptop. Zooming in reaches the full-resolution data: the server reads the raw samples for the visible window only, through an index on session, signal, and time, and downsamples them to what the chart can show. Lists are paginated.
- **Presentation:**
  - Themes: light and dark, from CSS custom properties, following the system with a toggle.
  - Keyboard: native controls, visible focus, and documented shortcuts.
  - Screen: layouts that fit a small laptop screen.
  - Units: imperial by default, set per vehicle profile, through one units module the CLI shares. Everything is stored SI.
- **Command:** `lasto gui` starts the server and opens the browser. A demo mode, over a data folder the simulator fills, lets other owners try it without a truck.

### 14.3 Sharing the data with a running capture

- **Two databases, so the GUI and a capture never both write one** (proposed for Phase 2, §14.8):
  - The **capture database** holds sessions, clock anchors, the segment index, per-ID stats, events, the audit log, conversations, samples, rollups, DTCs, freeze frames, readiness, Mode 06, and vehicle info. Only capture and polling processes write it.
  - The **workbench database** holds tags, notes, mapping sessions and reference values, solver runs, verification history, saved chart layouts, and settings. The GUI and the CLI's analysis commands write it.
  - Rows refer to sessions by UUID, so copies and merges stay consistent.
- **GUI reads:**
  - **Connection:** the capture database is opened as `file:…?mode=ro` with `PRAGMA query_only`.
  - **Short reads:** one short read transaction per query, no cursor held across requests, and indexed, paginated queries. Long reads are taken in chunks.
  - **Why short:** in WAL mode readers never block the capture's writer, but a long read can keep a checkpoint from finishing and let the WAL grow. Short reads prevent that.
- **The GUI's only writes to the capture database:** deleting closed sessions under the retention policy, only while no capture is running, and in small batches.
- **Live feed:**
  - **What:** once a second, the capture process writes the current decoded values, alarms, frame counts, and a heartbeat to a small separate `live.sqlite`, in its own connection.
  - **Failures:** a failure there is logged and ignored; it never stops the capture.
  - **Readers:** the GUI's live monitor and `lasto view` (Phase 4) both read it, read-only.
  - **No socket:** the capture process opens no listening socket.
- **Is a capture running?** The capture process holds an OS lock on a file in the data folder for its whole life, and the live feed carries its heartbeat. Only the capture process recovers an interrupted session; the GUI never does.
- **Stopping a capture the GUI started:** it asks through `drive`'s stop request (§14.8). If the process doesn't exit in time, the GUI reports that and asks before ending it. The segment format already tolerates a torn tail.
- **Charts and the broadcast explorer:**
  - **Signal rollups:** each once-a-second commit also writes, for every decoded signal, that second's min, max, mean, and last value.
  - **Per-ID stats:** for every broadcast ID, the second's frame count and a mask of the bits that changed.
  - **Raw frames:** read from the zstd segments one second at a time, through the segment index.
- **Copies:** paths in the database are relative to the data root, so a copied folder works anywhere. `lasto backup` makes a consistent copy with SQLite's online backup, even during a capture.

### 14.4 Import boundary, service layer, and the serial guard

Proposed package split (settled at the start of Phase 2, §14.8):

```
src/lasto/
  services/     hardware-free application logic; the CLI and the GUI both call it
  operations/   hardware-facing work (drive, snapshot, identify, discover); the only
                code outside the safety core that opens safety core sessions; CLI only
  capture/  storage/  decode/  protocol/  mapping/  analysis/  report/  export/
                hardware-free, except capture/, which runs inside operations
  cli.py        argument parsing and rendering only: each command calls one service
                or operation
  gui/          routes, templates, vendored assets; calls services only
```

- **No logic only in the CLI:** services return plain data, stored SI. The CLI renders it as text or `rich`; the GUI renders it as HTML.
- **Service imports:** a structural test holds the import closure of `lasto.services` free of the safety core, the simulator, operations, and hardware libraries. The GUI's boundary test (§14.1) builds on that.
- **Plain data types:** storage, decoding, passive protocol reassembly, mapping, and the GUI use hardware-free record types, never `lasto.safety.frames`. The capture process converts at its reader subscriber.
- **Separate reassembly:** passive Creader reassembly (`protocol/`) has its own ISO-TP and KWP parser instead of importing `lasto.safety.isotp`. A differential test feeds the same corpus of frames to both ISO-TP parsers and requires identical results, so the two can't drift apart.
- **Safety configuration for display:**
  - **Snapshot file:** a committed `safety_config.json` holds the allowlists, approved ECU IDs, rate limits, and interlock thresholds. A test (which may import both) proves it matches the safety core exactly, and the GUI reads the file.
  - **Per session:** each session record also stores the configuration in force when it ran.
- **cantools:** at Phase 5, check whether importing cantools loads python-can's interfaces. If it does, DBC writing runs in a CLI subprocess rather than in the GUI process.

**The serial guard (the §13 Phase 3 blocker), reconciled with the boundary:**
- **The top-level package:** `lasto/__init__.py` stays free of the safety core, so importing `lasto`, or `lasto.gui`, never loads it.
- **Hardware processes:** `lasto.cli.main` imports `lasto.safety`, which installs the serial guard, as soon as it has parsed the command line and before it dispatches any command except `gui`. Argument parsing opens nothing, so the guard is in place before any lasto code in a hardware process could open a port.
- **The GUI process:** `lasto gui` dispatches without loading the safety core, and the GUI process relies on its own guard hook (§14.1).
- **The service layer:** it refuses serial device paths in anything a user types, such as an export or storage location, with a clear message.
- **Tests:**
  - in a subprocess, the guard is installed before a hardware command's handler runs;
  - in another, `lasto gui` never loads `lasto.safety`;
  - the independent review the blocker calls for covers both guards.

### 14.5 Features

1. **Home:** vehicle profiles; the last session; current DTC status across ECUs, with known conditions labeled; readiness; open alerts; disk usage against the storage budget.
2. **Sessions:**
   - A list with date, duration, mode, profile, frames captured, and events.
   - Filtering, search, tags, and notes.
   - Deletion with confirmation, respecting the retention policy (§14.3).
3. **Session detail:**
   - **Charts:** interactive time-series charts with zoom, pan, stacked charts with a synchronized cursor, a signal picker, and saved layouts.
   - **Built-in views:** Grades (transmission temperature, gear, lockup, RPM, speed, load, coolant), Emissions, and KDSS.
   - **Event markers:** threshold crossings, lockup drops under load, and kill-switch events.
   - **Statistics:** per signal.
   - **Audit log:** for a polled session, every transmitted frame and rejected request.
   - **Buttons:** the session report, and an export pack with VIN scrubbing on by default.
4. **Health:** DTC snapshot history per ECU with a diff between any two snapshots; readiness history; Mode 06 results with limits and trends. Maintenance events in the vehicle profile, such as the 2026-09-21 catalyst repair, mark the charts, so results since a repair are easy to follow.
5. **Mapping workbench** (the most important feature):
   - **Captures:** Creader capture sessions, with conversations grouped by bus (CAN or K-line), ECU, and identifier, and the raw payloads.
   - **Reference entry:** a form that replaces the CSV template (value name, value, unit, ordered steady-state points, optional timestamps), and can import a filled-in template.
   - **Solver:** ranked candidates with fit quality, and a plot of predicted against reference values.
   - **Confirm or reject:** each candidate. Confirming goes through the same service as `lasto verify`, with the same evidence (a Creader capture and a reference fit), and the history is recorded. Nothing, an imported definition included, can be made verified by a click without that evidence. Verified status is what admits a definition to a logging profile (N10).
   - **Unmapped queue:** identifiers the Creader requested that are still unmapped.
6. **Broadcast explorer:**
   - **IDs:** CAN IDs with frame rates and periods.
   - **Heatmap:** a byte and bit change heatmap over time, with filters.
   - **Correlation:** pick a known signal (polled, or a Creader reference) and rank broadcast IDs and bytes by how well they track it.
   - **Save:** a discovered signal to the DBC, with name, scale, offset, and units.
   - **Raw frames:** a read-only viewer with filtering.
7. **Definitions library:**
   - **Browse:** every polled definition and broadcast signal, with status (unverified, verified, rejected), source, verification history, and search.
   - **Export:** a shareable pack, with the VIN and personal data stripped.
   - **Import:** packs from other owners, always as unverified.
   - **Scope:** definitions say which vehicle platform they apply to.
8. **Live monitor, read-only:** decoded values and alarms from the live feed while a capture runs. Start and stop controls for passive capture only (§14.1, rule 2).
9. **Settings:**
   - **Editable:** vehicle profiles (tire size correction, alarm thresholds, known conditions, units, maintenance events), the storage location, retention and disk budget, and export defaults.
   - **Read-only:** the compiled-in safety configuration.

### 14.6 Tests

- **The import boundary from rule 1:** static and dynamic, plus the GUI process's own guard hook.
- **Network exposure:** bind address and Host header tests; the launch token and cookie; CSRF on every endpoint that changes state; a check that no GET changes state.
- **Offline:** the vendored-asset manifest, and no outbound connections.
- **Capture launcher:** the passive command builder.
- **Service layer:** unit tests, shared with the CLI.
- **Endpoints:** tests against a fixture database built from simulator sessions.
- **Capture coexistence:** a simulated capture writes to the same databases at full rate while the GUI browses, charts, and saves notes. The GUI stays responsive, the capture loses no frames, its once-a-second commits keep their schedule, and the WAL stays bounded.

### 14.7 Sub-phases (stop for approval after each)

- **9a:** read-only browsing: Home, Sessions, Session detail, Health.
- **9b:** Mapping workbench and Broadcast explorer.
- **9c:** Definitions library, Live monitor, Settings.

**Phase 9 is done when:**
- **Without the command line:** you can review any drive, map a new value from a Creader session, and publish a definitions pack.
- **Transmit reach:** tests prove no GUI code path can reach a transmit function.
- **Capture:** the GUI never slows or interrupts a running capture.

### 14.8 Changes this plan asks of Phases 2 to 8 (approved; they start with Phase 2)

Phase 2's rows are built (§4, §9). The safety configuration is recorded on each run, which every session belongs to. For the zoom row, Phase 2 provides the per-second index into the raw segments; the signal index follows with decoding.

| Phase | Change |
|---|---|
| 2, before feature code | **Service layer:** the `services/` and `operations/` split in §14.4, with the CLI kept to parsing and rendering, and the structural test on `lasto.services`. |
| 2, before feature code | **Safety core imports:** a structural test that `lasto.safety` imports nothing from `lasto` outside itself. That holds today but isn't enforced, and it keeps user settings from ever feeding the safety core. |
| 2 | **Storage schema:** the capture and workbench databases, session UUIDs, a vehicle ID on every session, and paths relative to the data root (§14.3). |
| 2 | **Plain records:** hardware-free frame and record types for storage, converted from the safety core's types in the capture process. |
| 2 | **Rollups:** per broadcast ID, each second's frame count and changed-bit mask, written with the once-a-second commit. Per-signal min, max, mean, and last follow once signals are decoded (Phases 4 and 5). |
| 2, 4, 5 | **Full resolution on zoom:** raw samples are stored with an index on session, signal, and time, so a zoomed chart reads only its window at full resolution, downsampled on the server (§14.2). |
| 2 | **Live feed and liveness:** `live.sqlite`, the heartbeat, and the capture lock file. |
| 2 | **Stopping `drive` from outside:** `drive` stops cleanly on a stop request from another process (a stop file, or a named event, checked every second), through the same path as Ctrl+C. A GUI, or any parent, can't send Ctrl+C to a background process on Windows. |
| 2 | **Serial guard in the CLI:** the dispatch order in §14.4, with its tests, as the Phase 3 blocker's second item. |
| 2 | **Safety configuration records:** each session stores the safety configuration in force; the committed `safety_config.json` and its consistency test. |
| 3 | **Separate reassembly:** `protocol/` gets its own reassembly (§14.4), with the differential test against the safety core's ISO-TP parser over a shared corpus of frames. |
| 3 | **Definitions:** stored as plain data with a vehicle-platform field. Verification is a service shared by `lasto verify` and the GUI. |
| 4 | **`lasto view`:** reads the live feed rather than running inside `drive`. Alarms are evaluated by a hardware-free service. |
| 5 | **cantools:** the import check in §14.4. |
| 8 | **Reports and exports:** services that return files, so the GUI's buttons and the CLI make the same thing. |
