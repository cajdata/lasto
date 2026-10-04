# Review brief: guard v2 and the rest of Phase 3, Step A

*Amended after the review (`phase3-step-a-review.md`, 2026-10-03): C6, C20, Q5, Q6 and Q8 now say what the review found. The rest is as reviewed.*

You're doing an independent, read-only review of lasto's Phase 3 Step A changes, for the project's owner. lasto is a read-only CAN data logger for the owner's 2006 Lexus GX470. Its safety core keeps the app from ever sending anything to the vehicle except allowlisted diagnostic reads. Step A rebuilt the guard that stops a lasto process from reaching the OBDLink MX+ adapter's serial port behind the safety core's back (review finding P2), and settled the other items blocking MX+ work. No MX+ code touches hardware until this review is done.

## Ground rules

- **Read-only.** Don't edit files, commit, or push. Report findings.
- **Never run anything that opens hardware:** no PCAN adapter, no COM port, no serial device, not even in a scratch script or `python -c`. Reading code is fine, and so is `uv run pytest`: its hardware firewall makes any PCAN load or serial open fail.
- The repo's hooks block shell commands that name `--live`, COM ports or PCAN channels. Don't work around them.
- **Don't enumerate device path spellings.** Guard v2 no longer recognizes device paths to refuse writes. A write must first resolve inside a registered folder, and no device path does (claim C6). Earlier reviews stalled while working through path forms. Check the guard against Python's documented audit-event table instead.

## Read first

- `CLAUDE.md`: the safety rules, and the threat model you rate severity against.
- `docs/architecture.md` §3.7 (guard v2 and the STN link) and §13 (the Phase 3 rows).
- `src/lasto/safety/serial_guard.py`, `audit.py`, `stn_port.py`; `src/lasto/storage/database.py`; `src/lasto/operations/data_folder.py`; `src/lasto/sim/pytest_plugin.py`.
- Python 3.13's audit events table: https://docs.python.org/3.13/library/audit_events.html

**Threat model, in short:** the safety core defends against accidental or convenient bypasses by code in this repository, including code a future Claude Code session writes, and against dependencies doing ordinary things. It doesn't claim to stop code in the same process that deliberately sets out to subvert it. The structural scanner exists to make any such route stand out and fail the suite.

## Scope

| Commit | What |
|---|---|
| 06797e4, cdcde4b | A passive session whose audit log fails still closes its channel; the run ends as a storage error |
| f88b04e, 708ec95 | Docstring and doc corrections |
| f978ef2 | No `.pyc` writes once the guard installs (approved exemption) |
| cd068d9 | A1: guard v2, a process writes only inside its data folder |
| 529f454 | A2: SQLite |
| 6cec243 | A3: child processes and Bluetooth sockets |
| f155571 | pyserial 3.5 joins the lock |
| ba74a3e | A4: foreign functions (ctypes) |
| 18f64c0 | Test plugin registers its folders as the session starts |
| 134fa06 | A5: audit files must be regular files |
| 5fc59a3 | A6: nothing happens before the CLI imports the safety core |
| f75142b | A7: the STN bootloader window (finding E) |
| 767e0dc, 8d8440b | A8: L4, and `StnAdapter.close` |
| fce481e | Docstring rewrap |

`git diff 04c1048..fce481e` shows all of it. The suite passes (`uv run pytest`: 2063 tests, 100% branch coverage on `lasto.safety`). For more property-test examples: `HYPOTHESIS_PROFILE=thorough uv run pytest tests/safety/test_properties.py --no-cov`.

## How to report

- **Verdict:** approvable or not.
- **Each claim below:** Verified, or Not verified and why.
- **Findings:** severity (High, Medium, Low) against the threat model, the claim it breaks, the file and function, a concrete failure scenario, and a proposed fix. Number Lows from L12 (Phases 1 and 2 used L1 to L11).
- Rate the open questions at the end as findings or non-issues.
- Say what you couldn't check.

## Claims

### Installation

- **C1.** The first import of `lasto.safety` installs the guard's audit hook (`serial_guard.guard_event`), process-wide. A Python-level audit hook can't be removed. From then on, Python writes no `.pyc` files. *Tests:* `tests/safety/test_serial_guard.py::test_importing_the_safety_core_stops_bytecode_writes`.
- **C2.** `cli.main` imports the safety core before it dispatches any command but `gui`, whose process never imports it. *Tests:* `tests/test_layers.py::test_the_serial_guard_is_installed_before_a_command_runs`, `::test_the_gui_command_never_loads_the_safety_core`.
- **C3.** Before that import, for the console script and `python -m lasto`, nothing opens a file to write or changes one, loads or looks up native code, connects, or starts a process. Python's own bytecode cache is off in that test (see Q9). *Tests:* `tests/test_startup_events.py`, including a control run that shows the recorder catches a write and a ctypes load.

### The event table

- **C4.** `guard_event` handles exactly these events. Everything else passes through.

  | Event | Arguments used | Decision |
  |---|---|---|
  | `open` | path, mode, flags | A write if `flags` has any bit outside the read-only set (`_READ_FLAGS`), or, without flags, if the mode has `w`, `a`, `x` or `+`. A write must pass `write_problem`. A read is refused only if its path names a serial device (C14). |
  | `os.truncate` | path | Must pass `write_problem`. A file descriptor passes, since its path was checked when it opened. |
  | `_winapi.CreateFile` | name, access, disposition | A write if any access bit is outside the read-only set (`_READ_ACCESS`), or the disposition isn't `OPEN_EXISTING`. Otherwise treated as a read. |
  | `sqlite3.connect` | database | Must pass `sqlite_problem` (C9). |
  | `sqlite3.load_extension`, `sqlite3.enable_load_extension` (turning it on) | | Refused. |
  | `ctypes.dlopen` | name | Must be one of lasto's libraries (C11). |
  | `ctypes.dlsym` | library, name | Must be on the list (C11). |
  | `subprocess.Popen`, `_winapi.CreateProcess`, `os.system`, `os.startfile`, `os.startfile/2`, `os.spawn`, `os.exec` | | Refused outside a test run (C12). |
  | `socket.__new__` | family | `AF_BLUETOOTH` (32) refused everywhere (C13). |

  Every refusal goes through `audit.refuse`, so it's recorded and then raised. *Tests:* the direct `guard_event` calls in `tests/safety/test_write_guard.py`, `test_sqlite_guard.py`, `test_process_guard.py` and `test_foreign_functions.py` (coverage doesn't trace code while an audit hook runs), plus real opens, connects, lookups and process starts in each.
- **C5.** Measured against Python 3.13's audit-event table, no event that opens a file for writing, opens a device, loads native code, or starts a process is missing from C4. The exceptions are those listed in Q1 and Q8. *This is the main thing to check.*

### Writes: the four checks

- **C6.** `write_problem(target, roots)` allows a write only if all of these hold, in order. The first two are lexical; checks 3 and 4 call `realpath` and `stat`, and run only on a path that passed the first two, so neither ever touches a device path:
  1. After `os.path.abspath` (or, for an extended-length drive path, after stripping the extended-length prefix, with no other rewriting), the path is inside a registered root: its case-folded form starts with the root plus a separator.
  2. Every name below the root is a plain name. Its base (before the first dot, trailing spaces dropped) isn't a reserved device name, and it has no colon, other invalid character, trailing dot or trailing space.
  3. `os.path.realpath` of the path is the path itself, so no link or junction is on the way.
  4. The path is an existing regular file, or doesn't exist and its folder is a real folder.

  An open file descriptor passes, since its path was checked when it opened. Anything that isn't a path is refused. *Tests:* `tests/safety/test_write_guard.py` (each check, the extended-length form, a drive-relative name, a junction, the root and its parent, the hook end to end, and a production subprocess).
- **C7.** A process registers one folder, through `allow_writes_in`, and a second, different one is refused and audited. The exception is a test run (the hardware firewall installed, C8), which also registers its temp folder and tool caches. A registered folder must be an existing real folder on a drive: plain names, no link, not a drive root. `drive` registers its data folder and points held audit records at a file in it (`operations/data_folder.use_data_folder`). `log` registers its data folder only if the folder exists, so a read creates nothing. *Tests:* `test_write_guard.py::test_the_data_folder_is_registered_once`, `::test_only_a_real_folder_on_a_drive_can_be_registered`, `::test_a_registered_folder_cannot_be_a_reserved_name_or_a_link`, `::test_outside_a_test_run_a_process_writes_only_to_the_one_folder_it_registered`; `tests/test_cli_commands.py::test_drive_then_log_in_processes_of_their_own`, `::test_log_of_a_data_folder_that_does_not_exist_creates_nothing`.
- **C8.** "A test run" means `pcan_dll.hardware_firewall_installed()`: a mark on `ctypes.CDLL.__init__` that only the test plugin sets. Nothing in `src/` outside the hardware bindings may use ctypes (scanner). The plugin registers its folders in `pytest_sessionstart`. *Tests:* `tests/safety/test_structure.py::test_ctypes_only_in_the_hardware_bindings`, `tests/sim/test_simulator.py::test_a_collection_error_is_reported_as_one`.

### SQLite (A2)

- **C9.** `sqlite_problem` lets `sqlite3.connect` open only an in-memory database (`:memory:`, `file::memory:`, or `mode=memory`), or a path or local `file:` URI that passes `write_problem`, in any mode, since SQLite may create the file. A non-local URI and SQLite's own temporary database (`""`) are refused. *Tests:* `tests/safety/test_sqlite_guard.py`.
- **C10.** `ATTACH` and `VACUUM INTO` open a second file with no audit event. The guard can't give a new connection an authorizer, because Python raises `sqlite3.connect/handle` before the connection is initialized. So:
  - every connection lasto opens (`storage/database.py`: `connect`, `connect_read_only`) has an authorizer that refuses `SQLITE_ATTACH`, which both statements reach;
  - the scanner flags `set_authorizer`, `enable_load_extension` and `load_extension` outside the safety core, with one approved exemption, `set_authorizer` in `lasto.storage.database`;
  - a structural rule bans `ATTACH` and `VACUUM INTO` SQL literals anywhere in `src/`.

  *Tests:* `tests/storage/test_database_setup.py::test_lastos_connections_refuse_to_attach_another_file`, `tests/safety/test_structure.py::test_the_sql_check`, `::test_no_sql_in_src_opens_another_database_file`, `tests/safety/test_structure_reach.py` (exemptions).

### Native code (A4)

- **C11.** `ctypes.dlopen` may load only three libraries:
  - `PCANBasic.dll` by its exact path in the system folder (from `GetSystemDirectoryW`, not the environment);
  - `kernel32` and `user32`, by those names.

  `ctypes.dlsym` may look up only these:
  - `PCANBasic.dll`'s `CAN_` functions, by prefix. The guard is on the passive path, which may not name the transmit function, so which `CAN_` functions `src/` binds stays the structural tests' job.
  - The exact kernel32 and user32 functions lasto binds (`FOREIGN_FUNCTIONS`).
  - pyserial 3.5's 23 kernel32 bindings (`PYSERIAL_FUNCTIONS`), only on the thread inside `with PYSERIAL_IMPORT`, which only `stn_port._pyserial()` enters.

  A lookup by ordinal, or in anything that isn't a ctypes library, is refused. *Tests:* `tests/safety/test_foreign_functions.py`. It includes a test that pins `PYSERIAL_FUNCTIONS` to the locked pyserial's `serial/win32.py`, and a production subprocess that shows a plain `import serial` is refused while `stn_port._pyserial()` imports the real pyserial.

### Child processes and Bluetooth (A3)

- **C12.** Outside a test run, every event in C4's process row is refused before the process starts. A test run may start processes, so a structural test holds that nothing in `src/` can start one (`subprocess`, `multiprocessing`, `concurrent.futures.process`, `webbrowser`, `_winapi`, and `os.system`, `startfile`, `popen`, `spawn*`, `exec*`, `posix_spawn*`, and asyncio's process functions). *Tests:* `tests/safety/test_process_guard.py`, including a production subprocess; `tests/safety/test_structure.py::test_the_child_process_check`, `::test_nothing_in_src_starts_a_child_process`.
- **C13.** A Bluetooth socket (`AF_BLUETOOTH`) is refused everywhere, before it exists. *Tests:* `test_process_guard.py::test_a_bluetooth_socket_is_refused_before_it_exists`, `::test_the_hook_decides_a_socket`.

### Reads

- **C14.** Reads stay open everywhere, since a read-only handle can't send the adapter anything. A read whose path names a serial device (COM or AUX in any part, or GLOBALROOT) is refused as a backstop. *Tests:* `tests/safety/test_serial_guard.py`.

### Audit files (A5)

- **C15.** `JsonlAuditSink(path)` and `hold_on_disk(path)` open the path to append, then refuse (audited) anything `fstat` doesn't report as a regular file, closing it first. A file descriptor passes C6, so this is what keeps the audit log off a pipe or console. *Tests:* `tests/safety/test_audit.py::test_the_audit_log_must_be_a_regular_file`, `tests/safety/test_refusals.py::test_the_held_records_file_must_be_a_regular_file`.

### The STN link (A7, A8)

- **C16.** Constructing an `StnAdapter` sends nothing. It reads until a prompt arrives (after a banner, if opening the link rebooted the adapter), or until `SETTLE_TIMEOUT` passes with nothing at all. Output with no prompt refuses the open (audited), closes the port and lets the audit log go. The settle time (3 s) and the banner's form (`ELM327 v…`) are provisional until the bench test.
- **C17.** `ATZ` goes only through `reset()`, which sends nothing more until the prompt is back. After a prompt that never came, or a banner outside `reset()`, the adapter refuses every command, monitor read and stop until it's opened again.
- **C18.** A failed read or write (an `OSError`, as pyserial's `SerialException` is) closes the adapter, audited, and nothing retries it.
- **C19.** `open_adapter` attaches the caller's audit log before the port opens, so a refused port name or a failed open (`adapter_open_failed`) is written to it (L4). `StnAdapter.close()` stops a running monitor first, closes the port and lets the log go even if the stop fails, and does nothing the second time.

  *Tests for C16 to C19:* `tests/safety/test_stn_port.py`: the bootloader-window, unknown-state, dropped-link, L4 and closing tests. The simulator (`sim/fake_stn.py`) records a violation, failing the test, for any byte sent during its bootloader window.

### Structural allowances added in Step A (all approved by the owner)

- **C20.** The changes Step A made to what the scanner and `test_frozen` allow are these, and nothing else:
  - scanner exemptions (`tests/safety/test_structure_reach.py::EXEMPTIONS`): `change sys.dont_write_bytecode` in `serial_guard`, and `set_authorizer` in `lasto.storage.database`;
  - `test_frozen` process-wide stateful objects: the write guard (`GUARD`) and `PYSERIAL_IMPORT`;
  - the test-run allowances for the write guard: its own folders, more than one root, child processes;
  - *added after the review:* the public API (`PUBLIC_API` in `tests/safety/test_structure_reach.py`) gained `serial_guard.allow_writes_in`, for `operations/data_folder.py` and `drive.py`.

## Open questions

Rate each as a finding or a non-issue.

- **Q1. Subinterpreters.** Python-level audit hooks belong to one interpreter, so code in a new subinterpreter would run without guard v2's hook. Creating one raises `cpython.PyInterpreterState_New` in the calling interpreter, which the guard could refuse. Should it?
- **Q2. A dependency's own SQLite connection.** Only lasto's connections refuse `ATTACH`. One a dependency opened (none does today) could attach a file that `sqlite3.connect` never saw, a device name included.
- **Q3. Reads of the adapter's port.** A read-only open sends no bytes. But opening a Bluetooth COM port connects the link, which may reboot the adapter. A device-interface path with no COM name in it isn't refused for reads (C14). Is either a transmit risk?
- **Q4. Between check and open.** `write_problem` checks a path, and the open follows. Something that swaps a junction into the data folder in between gets past check 3. For the audit files, C15 catches the result. For the data folder's other files, the folder is the owner's own. Is that enough?
- **Q5. The test-run signal (C8).** Setting the mark on `ctypes.CDLL.__init__` unlocks more than one root and child processes. It takes ctypes, which the scanner bans in `src/` outside the bindings. *Recorded after the review:* the mark sits on the test plugin's wrapper that makes `PCANBasic.dll` fail to load, installed together with the pyserial stub, so a process that carries it can't reach hardware through lasto's bindings either.
- **Q6. Sockets from a handle.** *Corrected after the review:* in CPython 3.13's `sock_initobj_impl`, the Windows branch skips the first `socket.__new__` audit whenever a `fileno` is given. For a shared socket from `socket.fromshare`, a second audit fires with the handle's real family, from its protocol info, so a shared Bluetooth socket is refused. For an integer `fileno`, no `socket.__new__` fires at all. Either way, getting such a handle takes another process or ctypes.
- **Q7. pyserial after `stn_port` has imported it.** The module is cached, so a later `import serial` anywhere in the process gets a working pyserial. The scanner allows `import serial` only in `stn_port` within `src/`; a dependency isn't scanned.
- **Q8. Events left alone on purpose.** Each of these was judged not to be a route to a port. Check that judgment.
  - `os.rename`, `os.remove`, `os.mkdir`, `os.link` and `os.symlink`. They don't open a device for I/O, and a link is caught by check 3 when it's opened.
  - *Corrected after the review (M1):* the `shutil` events were on this list, and the judgment was wrong for `copy2`, and for `copytree` and a cross-volume `move`, which call it. `copy2` copies through `_winapi.CopyFile2`, which raises no audit event, so the guard never sees the destination. Python 3.14's `Path.copy`, `copy_into`, `move` and `move_into` reach `CopyFile2` too. A structural test now keeps all of these, `shutil.copy` and `copyfile`, and any import of `_winapi` out of `src/` (`tests/safety/test_structure.py::test_nothing_in_src_copies_a_file_outside_open`). Removing `CopyFile2` at guard install, to cover dependencies, is evaluated and waits on the owner's decision.
  - `msvcrt.open_osfhandle`. It needs a handle that `_winapi.CreateFile`, which is checked, or something else opened.
  - `_winapi.CreateNamedPipe`. A pipe isn't a port.
  - `ctypes.call_function` and `ctypes.cdata`. They need a function address that only an allowed lookup gives.
  - `socket.connect` for IP sockets. The MX+ isn't on a network.
  - `import ctypes` itself loads kernel32 and looks up `GetLastError`. lasto's first `import ctypes` is inside the safety core's own import, before the hook goes in.
- **Q9. Python's bytecode cache before the guard.** If a module imported before the safety core has a stale `.pyc`, Python writes a new one into the package's `__pycache__` before the guard installs. An install built with `uv sync --locked --compile-bytecode` leaves nothing stale. C3's test turns the cache off for that reason.
