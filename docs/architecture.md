# lasto architecture

Status: **Phase 0 approved 2026-09-26.** Decisions are in §0 and override anything below that says *(open)*. Facts about the truck are hypotheses from documentation research until a capture confirms them.

## 0. Decisions (Phase 0, 2026-09-26)

- **Machines:** Claude runs on the desktop, which never has the PCAN or MX+ attached. The truck laptop (i7-8550U with SSE4.2 and AVX2, 16 GB RAM, Windows 11 25H2) is the only machine that touches hardware. It has internet at home for setup and updates and runs offline in the truck.
- **Hardware:**
  - PCAN-USB IPEH-002022 (opto-decoupled, SJA1000 controller), with PCAN-Basic ≥ 4.7.0 and not 5.0.0 (version to be reported).
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
- **Data:** `%LOCALAPPDATA%\lasto`. The disk budget is set after the Phase 2 storage estimate.
- **Tooling:** uv (you install it), Python 3.13. Until uv is installed, development uses a plain `.venv` with the pinned dev group from `pyproject.toml`.
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
- **Carried forward:** see §13.

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

### 3.1 PCAN passive (rule 1)

python-can 4.6.1's `PcanBus` sets listen-only before `CAN_Initialize`, but it ignores the `SetValue` result, and its `state` property returns a cached value instead of asking the driver. So if the pre-init set fails, the channel silently comes up active. The safety core binds the DLL itself with ctypes:

- **Loading:** from `%SystemRoot%\System32\PCANBasic.dll` by absolute path. Require a 64-bit Python process, and require a PCAN-Basic API version of at least 4.7.0 other than 5.0.0 (5.0.0 mishandles status messages).
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

The `Writer` doesn't trust its caller. For every frame it re-runs the policy's full stateless check (`policy.check_frame`: ID allowlist, broadcast IDs, ISO-TP parse, service allowlist and never-list, sensitive ECUs, canonical flow control on an approved ECU's request ID), refuses a frame that isn't the kind the caller named, checks the kill switch, holds request frames to the hard ceiling (§3.3, rule 6), and writes the audit record, all before `CAN_Write`. So even a direct call can't send a denied service, use a non-allowlisted ID, or exceed 20 requests per second. The checks that need state stay in the gate: flow control only for a first frame that is waiting for it, the interlocks, and the per-purpose rates and back-off.

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
   - The `Writer` enforces the ceiling again on its own: at most 20 request frames in any 1.0 s window, by its own clock readings, with a lock so concurrent callers can't slip past together. Flow control frames don't count (the gate sends at most one per first frame).
   - So the gate never meets that backstop, it counts the shared spacing from the end of each write and adds a 1 ms margin, making its fastest pace 19.6 requests per second. Pacing at exactly 50 ms would put 21 frames inside one second through float rounding alone.
   - **Reading before sending:** the gate waits for its slot in slices of at most 20 ms and reads the channel after each (the polled reader, with an immediate status check), stopping at once on a kill. The last read comes right before the frame goes out, so every kill trigger that has already arrived is seen first: an error frame, a bus-off shown only in the status, or another tester. Then the interlocks are checked again with that moment's readings.
7. **Audit, write-ahead:** log the frame and purpose. If the audit write fails, don't transmit.
8. **Write**, then log the result.

Rejections are logged with the reason and raised as `SafetyViolation`.

**Flow control:** the receive side sends `30 00 00` (padded) only when a first frame arrives on the response ID paired with an outstanding request. It goes to that request's physical ID, at most once per first frame. After a functional request, FC goes to the responding ECU's physical request ID, and only if that ID is approved. Otherwise the response is abandoned.

**Approved request IDs and K-line addresses** live in `safety/ecus.py`. It starts with 0x7E0 only. Each entry records the response ID, addressing mode, ECU kind (engine, transmission, abs_vsc, kdss, suspension, tpms, srs, immobilizer, body), and the evidence (which capture). Adding one is a safety core change: it needs your approval, the ask rule fires, and it gets its own commit. An unclassified ID can't be approved. *(open)*

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
- **Refusals are audited:** a refused change is recorded as `safety_core_frozen` (transport `core`).

What pure Python can't block at runtime (`object.__setattr__` or `type.__setattr__` called directly, frames, gc, ctypes, builtins, pickle) is banned in `src/` by the deliberate-routes test (§7). The static change test catches any assignment, deletion, in-place change, `setattr`, monkeypatch or `mock.patch` aimed at a safety module, class or module-level object, from `src/` and from the tests. Tests may change only instances they made.

## 4. Capture and storage

- **Reader thread** per channel, owned by the safety core. It pushes to subscribers in order: raw recorder → kill-switch monitor → gate response matcher → ISO-TP/KWP conversation tracker → live decoder.
- **Raw log:** candump text (`(1727380000.123456) can0 7E0#0201000000000000`), written as one complete zstd frame (checksum on) per second of traffic, then flush and `fsync`. A new segment file starts at every session start and every hour, and the app never appends to a file that might end in a torn frame. The reader decodes frame by frame and drops a truncated or corrupt tail. A skippable zstd frame before each data frame holds a sequence number, the first and last timestamps, and the frame count, so `zstd -d` and `can-utils` still work.
- **SQLite:** WAL mode with `synchronous=FULL` and one batched commit per second.
  - Tables: sessions, clock anchors, raw segment index, per-ID stats (count, period, DLC, first/last seen), events (status, kill, errors), audit, conversations, signals and samples, DTCs, freeze frames, readiness, Mode 06, vehicle info, definitions and verification history, mapping sessions and reference values, discovery results, schema migrations.
- **Timestamps:** the session start in UTC, the host monotonic clock, and the hardware timestamp, plus an anchor every 60 s. Per-frame time is a microsecond offset from the hardware clock, and UTC is derived from it. The laptop is offline, so UTC is only as good as its clock.
- **Recovery:** sessions stay marked open until they close cleanly. On the next start, open sessions are trimmed to their last good frame and marked recovered.
- **Keep awake:** `SetThreadExecutionState` stops Windows from sleeping during capture. The lid-close action stays a Windows setting you control.
- **Storage estimate:** a four-node bus is probably lightly loaded, perhaps 500-2,000 frames/s, which is about 5-30 MB per hour compressed. The real figure and a retention policy come after the first capture.

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
- **Labeling:** every broadcast ID and local ID is labeled fictional until a real capture replaces it.
- **Violation recorder:** any frame written while listen-only is on, any frame on an ID other than 0x7DF and the approved 0x7E0 (the oracle is exactly as strict as the policy, kept by hand), and any denied service or STN command is recorded even if the code under test catches the exception. A pytest plugin fails the run at session end if anything was recorded.

## 7. How the tests prove the rules

- **Unit tests** for every allowlist decision: IDs, services, the never-list, addressing modes, FC conditions, STN commands (including normalization tricks: case, spaces, backspace, hex-only lines, empty lines).
- **Hypothesis** fuzzes CAN IDs, payloads, addressing, typed request sequences, and injected faults through the gate against `FakePcanDll`, asserting that every frame reaching `CAN_Write` satisfies the policy. It fuzzes the `Writer` directly too, around the gate. It does the same for strings reaching the fake serial port.
- **Structural tests** read the source:
  - only `safety/pcan_active.py` names `CAN_Write`, and only the gate calls the `Writer`
  - only `safety/stn_port.py` opens or writes serial
  - nothing outside `lasto.safety` imports the DLL bindings or pyserial
  - code outside the safety core uses only its public API. Names are resolved through every re-export and attribute chain back to the module that defines them (`tests/scan.py`).
  - no code in `src/` uses a deliberate route around the core's guards (ctypes, gc, inspect, importlib, builtins, pickle and other code-rebuilding modules, `sys.modules`, `vars`/`globals`/`setattr`/`delattr`, `getattr` with a private or computed name, attribute-guard dunders, function defaults, frames and tracebacks, trace and import hooks, or, outside the safety core, another object's private attributes), except an explicit exemption list in `tests/safety/test_structure_reach.py`. Each new exemption is its own commit, approved by the owner.
  - nothing in `src/` or the tests changes a safety module, class or module-level object (§3.9), and the safety core has no `global` statements
- **Runtime freeze tests** (`tests/safety/test_frozen.py`) try to change every name in every safety module, every attribute of every safety class, and every enum member, and expect each attempt to be refused and audited. They also check that every module-level value is immutable and that every instance keeps its state in private slots.
  - passive modules contain no write reference
  - every argparse parser has `allow_abbrev=False`
- **Reachability tests** walk every attribute path from a live polled session: the raw `CAN_Write` is reachable only inside the `Writer`, and the reader's channel can't write.
- **Hardware firewall** (`tests/conftest.py`, autouse): loading `PCANBasic.dll` or opening a real serial port raises.
- **Coverage:** `python -m pytest` runs branch coverage on `lasto.safety` and fails below 100 %.
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
  capture/  protocol/  storage/  decode/  mapping/  polling/  snapshot/  identify/  discover/
  analysis/  report/  export/  view/
  sim/                       # FakePcanDll, FakeStnPort, vehicle model, ECUs, Creader, violations
definitions/  dbc/
tests/  (conftest.py, safety/, sim/, storage/, guardrails/, ...)
docs/architecture.md
.claude/settings.json  .claude/hooks/hardware_guard.py  CLAUDE.md
```

## 9. CLI

- **Entry point:** one command, `lasto`, with subcommands `drive`, `map`, `snapshot`, `identify`, `log`, `view`, `discover`, `decode`, `report`, `export`, `verify`.
- **Simulator by default:** real hardware needs `--live` plus `--channel PCAN_USBBUSn` or `--port COMn`, typed every time. Config holds no default channel.
- **No abbreviations:** every parser sets `allow_abbrev=False`, so `--liv` can't mean `--live`.
- **Drive modes:** `drive` alone is passive; `drive --profile NAME` is polled.
- **Raw console:** exists only as `lasto sim console`, which has no `--live` option.

*(open: what `log` and `verify` should do)*

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

**Tooling proposal:** uv 0.12.19 manages Python 3.12 and a hashed `uv.lock`. python.org's last Windows installer for 3.12 is 3.12.10; uv installs current 3.12.x builds.

Offline install on the truck laptop:
1. `uv export` a hashed requirements file.
2. `pip download` a win_amd64 wheelhouse.
3. Copy it with uv and a Python archive to the laptop.
4. Install with `--offline --no-index --require-hashes`.

*(open)*

## 11. Phase 1 scope (built)

Safety core for both transports, `FakePcanDll`, `FakeStnPort`, the minimal vehicle model, the violation plugin, the hardware firewall, the structural tests, Hypothesis fuzzing, 100 % branch coverage, and a CLI skeleton with `--live` gating. No feature code.

The STN transport in Phase 1 has **no path that puts anything on the vehicle bus**. It covers reset, identification, voltage reads, CAN silent monitoring, and K-line passive monitoring (needed in Phase 3). The only bus-facing use it would ever have is K-line polling, which needs the Phase 7 decision on 0x81 anyway. Until then every hex-only request line is denied, and the tests prove it.

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
| 2 | **Save and report on a kill:** the kill switch already stops and logs; flushing capture storage and writing the report arrive with Phase 2 storage. |
| 2 | **Audit log storage:** the durable audit log (SQLite, alongside the JSON Lines sink) is wired into real sessions. |
| 2 | **Held audit records on disk:** refusals and kills recorded while no audit log is open are held in memory, handed to the next log that opens, and reported on stderr at exit. Once Phase 2 sets the data location, they also go to a durable fallback file there, so a crash can't lose them. |
| 2 | **Broadcast IDs:** the gate's set of IDs seen carrying broadcast traffic is produced from capture statistics. |
| 2 | **Storage:** estimate per driving hour, then set the disk budget and retention. |
| 2 | **Bench test:** you decide before the first live test (optional; parts list in §12). |
| 2 | **"Controller activated" messages:** the first live passive test shows whether the driver sends them routinely. If it does, their re-check stays cheap; if they come with resets, the reopen path handles them. |
| 3 | **Creader captures confirm:** 0x7E0 and any new request IDs, the padding byte, and the trimmed manufacturer service list. |
| 3 | **MX+ checks:** stopping a monitor with a backspace, and silent monitoring on the real adapter. |
| 3 | **Bootloader window (review finding E, deferred here):** the rule in §3.7 isn't enforced in code yet. `reset()` sends `ATZ` as soon as a connection opens, and again when retried after a prompt timeout. Settle, and verify on the real MX+ with the other MX+ checks: whether to wait for the banner and `>` after opening, reconnecting, or waking before sending anything; never re-send `ATZ` after a prompt timeout without first reading what the adapter sent; and how a Bluetooth reconnect or wake is detected. Nothing in Phase 1 or 2 opens the adapter on real hardware. |
| 4 | **BLOCKER before the first polled live test:** wire the Ctrl+Alt+K hotkey into polled sessions, and refuse to start polling if it can't be registered. Verify it on the truck laptop's real Windows. |
| 7 | **K-line polling:** decide on 0x81 StartCommunication and running without 0x3E keep-alives. |
| after approval | Switch to uv with a hashed lock file (all dependencies, including transitive ones, pinned). |
