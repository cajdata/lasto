# Review: Phase 3 Step A (guard v2, SQLite, child processes, foreign functions, STN link)

Independent, read-only review against `docs/reviews/phase3-step-a-brief.md`, 2026-10-03. Nothing was edited or committed, and nothing was run against hardware. The test suite was run once, as the brief allows.

Suite run: 2063 passed in 37 s, 100% branch coverage on lasto.safety, 100% on lasto.capture and lasto.storage.

## Verdict

Approvable, on one condition. Guard v2 holds against the Python 3.13 audit-event table for every route to a serial port, to native code, and to a child process that the brief set out to close, and every claim but two checks out as written. The condition is M1: a standard-library copy path that writes a file, possibly a device, with no audit event at all. No code in `src/` uses it today, and it closes with a structural test and a documentation note, with no safety core change. Close it before any MX+ code touches hardware. The Lows can be accepted or deferred.

## Findings

Severity is rated against the threat model in CLAUDE.md: accidental or convenient bypasses by code in this repository, including code a future session writes, and dependencies doing ordinary things.

### M1 (Medium): `shutil.copy2`, `copytree`, and cross-volume `move` write files with no event the guard sees, and the destination can be a device name

- Claim broken: C5, and the Q8 judgment that the `shutil` events do not open a device for I/O.
- Where: the stdlib's `shutil.copy2` on this interpreter (Python 3.13.14, `Lib/shutil.py` lines 446 to 466). When `_winapi.CopyFile2` exists it is called first, and only two specific errors fall back to `copyfile`. `_winapi.CopyFile2` is not an audited function, and `copy2` raises no event of its own. `copytree` and a cross-volume `move` call `copy2`. The `shutil.copyfile` path, by contrast, goes through `open` and is caught.
- Scenario: a Phase 8 export writes `shutil.copy2(segment, args.out)` and the user types `COM5` as the destination. No `open`, `_winapi.CreateFile`, or `shutil.copyfile` event fires. The kernel opens `COM5` for writing and CopyFile2 streams the file's bytes to whatever answers. Whether CopyFile2 completes against a serial device I could not verify and must not test; serial.sys accepts the end-of-file information classes that copy routines set, so assume it does. Even a failed copy has opened the port for writing with no event and no audit record.
- Today: `src/` uses `shutil` only for `disk_usage`, so there is no live route. This is exactly the class P2 was written for: convenient future code plus a typed path.
- Fix: a structural rule in `tests/safety/test_structure.py`, like `starts_a_child_process`, that bans `shutil.copy`, `copy2`, `copyfile`, `copytree`, and `move` in `src/`, so copies go through `open`, which the guard sees. Adding `shutil.copytree` and `shutil.move` to `guard_event` with `write_problem` on the destination gives runtime depth too, but `copy2` alone raises nothing, so the structural rule is the one that closes it. Record the judgment in Q8 and in architecture §3.7.

### L12 (Low): the C5 exception list is incomplete

None of these opens a serial device. Each should be written down with its reason so the next reviewer does not rediscover it.

- `import` of a C extension module loads native code with only the `import` event. Refusing that is impractical, since `_sqlite3`, `_ctypes`, and `zstandard` are all extensions. The defenses are the hashed lock file and the scanner's import rules.
- `_winapi.CreateJunction` and `winreg.SaveKey` change the filesystem outside the data folder with no `open`. A junction planted inside the data folder is caught by check 3 when something writes through it, and SaveKey needs backup privilege.
- SQLite's own temporary files go to `%TEMP%` with no event. "A process writes only inside its data folder" is true of lasto's writes, not of every byte the process causes.

Fix: documentation only, in Q8 of the brief and in §3.7.

### L13 (Low): subinterpreters (Q1)

Refuse `cpython.PyInterpreterState_New` outside a test run. A subinterpreter runs with none of this interpreter's Python-level hooks, so code there could open anything. Python 3.14's `concurrent.interpreters` makes using one convenient, and the classifiers list 3.14. Nothing in `src/` or the dependencies creates one today. This is a safety core change, one branch in `guard_event` plus a direct `guard_event` test, and needs the owner's approval.

### L14 (Low): every lasto connection carries the authorizer by inspection, not by rule

C10 depends on `sqlite3.connect` being called only in `storage/database.py`. That is true today, but a future module calling `sqlite3.connect` directly would get a connection that can `ATTACH`. The SQL-literal regex in the structural test also misses `ATTACH DATABASE foo AS x` with a bare identifier, which SQLite accepts as a string, and any statement assembled from pieces. Fix: a structural test that `sqlite3.connect` appears only in `lasto.storage.database`. Tests only.

### Notes, not findings

- C6 in the brief says the first three checks are lexical; two are. Check 3 calls `realpath` and check 4 calls `stat`, both only after the lexical checks pass, so neither ever runs against a device path. The `write_problem` docstring has it right.
- C20 omits one allowance Step A added: `allow_writes_in` joined `PUBLIC_API` in `test_structure_reach.py`, for `operations/data_folder.py` and `drive.py`. Reasonable, but it belongs on the list.
- Q6's text is inaccurate in a helpful direction; see the open questions.
- `drive.py` line 119 reads `awake =keep_awake`, a missing space. Cosmetic.

## Claims

- C1 Verified. `serial_guard.py` installs the hook and turns off bytecode writes at import; the subprocess test shows it. `sys.dont_write_bytecode` is a plain attribute any code could flip back; the scanner flags such an assignment in `src/`, with the one exemption, and if it were flipped a refused `.pyc` write fails the import loudly, which is fail-safe.
- C2 Verified. `cli.main` imports the core right after parsing, before the hardware and log argument checks and before dispatch; `gui` skips it. `--version` and parse errors exit before the import and only print.
- C3 Verified, with one caveat: `.pth` files run before `sitecustomize`, so the recorder cannot see them. The uv venv has none that do anything.
- C4 Verified. `guard_event` handles exactly the listed events and passes everything else; argument positions match the table, including the `_winapi.CreateFile` disposition at index 3.
- C5 Verified for device, native-code, and process routes, with the exceptions in M1 and L12.
- C6 Verified; two lexical checks, not three.
- C7 Verified. `drive()` registers the folder a second time, the same folder, which is harmless.
- C8 Verified. `test_only_the_pytest_plugin_resets_the_kill_switch` also holds that nothing in `src/` imports the plugin.
- C9 Verified. The event does not say whether `uri=True` was passed, so a `file:` string opened with `uri=False` is checked as a URI while SQLite opens it as a literal relative name. That literal name contains a colon, cannot be a device, and resolves under the same folder. Non-issue.
- C10 Verified in CPython 3.13 source: `self->initialized = 1` is set after the `sqlite3.connect/handle` audit, and `pysqlite_check_connection` refuses `set_authorizer` with "Base Connection.__init__ not called" until then. Both lasto openers set the authorizer; L14 makes that structural.
- C11 Verified. The 23 names match the locked `serial/win32.py`. `serialwin32.py` and `serial/__init__.py` make no further lookups at import or call time, so nothing pyserial needs is refused after the window. `ctypes.pythonapi` is created with the name "python dll", which the guard refuses lookups in.
- C12 Verified. Windows has no fork or posix_spawn; the structural list covers them anyway.
- C13 Verified.
- C14 Verified.
- C15 Verified; the refused file is closed before the refusal is raised.
- C16 Verified. The settle read runs with the 3 s timeout, and pyserial's `read_until` returns within it even when the adapter streams, so a monitor left running by a crashed session is refused with the power-cycle message.
- C17 Verified. `ATWS` is allowlisted but no routine sends it.
- C18 Verified; `OSError` catches pyserial's `SerialException`.
- C19 Verified. Attachment counts make the double attach safe, and a second `close` leaves the log alone.
- C20 Verified except the `PUBLIC_API` omission above. The `fce481e` rewrap is docstring-only. The `708ec95` change to `hardware_guard.py` is comment and message text only, with no change in what it blocks or asks.
- Scope rows outside the numbered claims: `06797e4` and `cdcde4b` do what they say. A passive session whose audit log fails closes its channel first, reopens nothing, and lets the log go; `drive` turns that into a `storage_error` run end, with tests for each path.

## Open questions

- Q1: finding L13.
- Q2: non-issue today; L14 makes it durable for `src/`. A dependency's own connection stays outside the guard. None exists, and attaching device files is not ordinary dependency behavior.
- Q3: non-issue for transmission. A read-only handle cannot send bytes. One note: any path probe on a user-typed device path, such as `os.stat`, `Path.exists`, or `realpath`, opens the device briefly with attribute access and no event. On a Bluetooth SPP port that may connect the link and wake the adapter, which is exactly what `_settle` and the banner check now handle. If `stn_port` already holds the port, a second open fails, since the serial driver is exclusive.
- Q4: non-issue under the threat model. Swapping a symlink into the data folder between check and open takes another process or symlink privilege, and C15 covers the audit files. No fix proposed.
- Q5: non-issue, with a property worth writing down. The mark that unlocks the test allowances lives on the same wrapper that makes PCANBasic.dll fail to load and stubs pyserial, so a process that somehow carries the mark cannot reach hardware through lasto's bindings either. Setting it takes ctypes or the plugin, both banned in `src/`.
- Q6: non-issue; correct the text. In CPython 3.13's `sock_initobj_impl`, the Windows branch skips the top `socket.__new__` audit whenever a `fileno` is given. For a shared socket from `fromshare`, a second audit fires with the handle's real family from the protocol info, so a shared Bluetooth socket is refused. For an integer `fileno`, no `socket.__new__` fires at all. Either way, getting such a handle takes another process or ctypes.
- Q7: accepted, write it down. Once `stn_port._pyserial()` has run, `serial.win32.CreateFileW` and `WriteFile` are bound and callable by any code in the process with no event, and `import serial` succeeds anywhere. That exposure exists only in a live STN process, after the adapter is already open and held. The scanner bans `import serial` outside `stn_port` and bans `sys.modules`, so a future session cannot reach it without a deliberate route.
- Q8: the `shutil` judgment is wrong for `copy2`, `copytree`, and `move` (M1); the rest hold. The reason given for `ctypes.call_function` and `cdata` is slightly overstated: a function address can also be derived from an allowed function's address and a loaded module's export table using only unrefused ctypes reads, but that is deliberate subversion, outside the model. The scanner's ctypes ban is the real defense. `import ctypes` binding kernel32's `GetLastError` at import is confirmed at line 502 of the stdlib's `ctypes/__init__.py`, inside the safety core's own import and before the hook.
- Q9: non-issue as documented. A stale `.pyc` in a working tree is Python's write into `__pycache__`, never a device.

## What I could not check

- CopyFile2 against a real serial device, the last step of M1's scenario.
- Anything on the real MX+: settle time, banner form, DTR behavior on open, and the backspace stopping a monitor. Those are B3.
- `.pth` files, which run before the A6 recorder installs.
- The thorough Hypothesis profile was not run.
