# Re-review brief: the Step A review fixes

A targeted, read-only re-review of the commits made in answer to `docs/reviews/phase3-step-a-review.md`, and nothing else. The first brief (`phase3-step-a-brief.md`, amended where the review corrected it) and the review give the background. The same ground rules apply:
- Read-only.
- Never run anything that opens hardware, even in a scratch script.
- `uv run pytest` is safe.
- Don't enumerate device path spellings.
- Don't work around the repo's hooks.

Rate severity against the threat model in `CLAUDE.md`.

## Scope

`git diff 6ff8a1e..HEAD`, where 6ff8a1e is the review's own commit:

| Commit | What | Answers |
|---|---|---|
| 493b1ad | Structural rule: `src/` copies a file only through `open` | M1 (a), (c) |
| b8d5fc3 | Structural rule: nothing in `src/` creates a subinterpreter | L13 |
| ec181bf | Structural rule: only `storage/database.py` opens SQLite connections | L14 |
| 9a339d1 | `drive.py` spacing | note |
| f0c50bd | Docs: accepted limits (L12, Q7), the Q5 property, the `.pth` limitation, brief corrections | L12, notes, C3 |
| f6cea78 | **Safety core:** the guard removes `_winapi.CopyFile2` | M1 (b) |
| e4256e1 | Structural rule: `src/` loads no module around the import statement | L13 |
| 326a65f | **Safety core:** the guard refuses importing what creates subinterpreters | L13 |
| 6fb9eac | Structural rule: no star imports in `src/` | found while writing this brief |
| *(this commit)* | This brief, and one doc phrase | |

Suite: 2159 passed, 100% branch coverage on `lasto.safety`, `lasto.capture` and `lasto.storage`. The thorough Hypothesis profile passed (9 property tests).

## Decisions the owner made after the review

- **M1:** close it as a class: a structural rule, plus removing `CopyFile2` at guard install, which covers dependencies.
- **L13:** the approved fix, refusing `cpython.PyInterpreterState_New`, turned out to be dead code on 3.13 (F4) and was dropped. The guard refuses the import statement's `import` event for the modules that create subinterpreters instead. The refusal is a class that's both a `SafetyViolation` and an `ImportError`, because Python 3.14's `concurrent.futures` imports `_interpreters` inside `try/except ImportError` as it loads.
- **L12:** documented as accepted limits, with their reasons.

## Claims

- **F1. The copy rule** (`tests/safety/test_structure.py::copies_outside_open`) flags each of these, imported, as an attribute, taken as a value, or through `getattr`:
  - `shutil.copy`, `copy2`, `copyfile`, `copytree` and `move`;
  - any import of `_winapi`;
  - pathlib's `copy_into`, `move` and `move_into`;
  - `copy` when called with arguments or reached through pathlib. `dict.copy()` and `copy.copy(x)` are left alone.

  `src/` passes. The one exception is `serial_guard`'s `import _winapi`, which the child-process and copy rules let through as that exact hit only, and `test_the_guards_winapi_import_is_still_there` fails if it goes unused.
- **F2. `CopyFile2` is removed** (`serial_guard.py`, the line after `sys.dont_write_bytecode`). The deletion is unconditional and runs once per process. *Tests:* `tests/safety/test_copy_guard.py`.
  - `shutil.copy2`, `copytree` and `move` fall back to `copyfile`'s `open()`, so a copy outside the allowed folders is refused before the file exists.
  - A direct caller fails with `AttributeError`.
  - Only deleting `_winapi` from `sys.modules` and importing it again brings `CopyFile2` back, and `shutil` keeps the module it already holds.

  The scanner exemption is `("change _winapi.CopyFile2", "lasto.safety.serial_guard")`. Check two things:
  - nothing binds `CopyFile2` before the guard installs. In 3.13, only `shutil` uses it, and only at call time. 3.14's pathlib decides at import, and lasto's CLI loads pathlib after the core.
  - the unconditional deletion can only fail loudly: a Python without `CopyFile2` would fail lasto's import, never run unguarded.
- **F3. The SQLite rule** (`opens_sqlite_connections`) flags these outside `lasto.storage.database`:
  - `sqlite3.connect` under any name: an alias, `from sqlite3 import connect`, `sqlite3.dbapi2.connect`, taken as a value, or through `getattr`;
  - a call or subclass of `sqlite3.Connection`, which opens a connection with no authorizer (probed: it accepts `ATTACH`). Annotations are allowed;
  - any import of `_sqlite3`.
- **F4. Subinterpreters, the dropped fix.** Check these from CPython 3.13's source, and against the probe:
  - `_interpreters.create()` calls `PyThreadState_Swap(NULL)` before `Py_NewInterpreterFromConfig` (`Python/crossinterp.c`, `_PyXI_NewInterpreter`).
  - `sys_audit_tstate` returns without calling any hook when the thread state is NULL (`Python/sysmodule.c`).
  - `_testcapi.run_in_subinterp` swaps to NULL too.
  - Probed on 3.13.14: no audit event at all fired while a subinterpreter was created.
- **F5. Subinterpreters, the guard's refusal.** `guard_event` refuses the `import` event for `_interpreters`, `_xxsubinterpreters`, `_testcapi` and `_testinternalcapi`, unconditionally, with `ImportRefused` (`errors.py`).
  - **Composition:** `ImportRefused` takes ImportError's instance layout with no conflict, and its MRO runs SafetyViolation, SafetyError, ImportError, Exception.
  - **Handling:** the CLI's `except capture.CANNOT_START` and the gate's `except SafetyError` treat it as a refusal; nothing in `src/` catches `ImportError`.
  - **Nothing needs them:** a recording hook over a full run saw no other import of the four, and no locked dependency names them in source or compiled extensions.
  - **On 3.14,** a process that loads `concurrent.futures` gets one `subinterpreter_refused` audit line, and no `InterpreterPoolExecutor`.

  *Tests:* `tests/safety/test_process_guard.py` (the subinterpreter section), and `tests/test_startup_events.py`, which now forbids importing the four before the core, since only a first import raises the event.
- **F6. The `import` event comes only from the import statement.** `importlib.import_module`, `importlib.util.module_from_spec` and `_imp.create_builtin` load a module with no event (probed). The subinterpreter rule bans these in `src/`, along with the modules, 3.14's `InterpreterPoolExecutor`, and `concurrent.interpreters`.
- **F7. Star imports.** A star import binds names that every name-based rule reads past: `from shutil import *` then `copy2(...)`, or `from os import *` then `system(...)`. `src/` has none, and a rule now keeps it that way.
- **F8. Docs say what the code does:**
  - §3: the known-limitations list (`.pth` files);
  - §3.7: the folders bullet (the Q5 property), File copies, Subinterpreters, and the accepted limits (L12, Q7);
  - the first brief's amended C6, C20, Q1, Q5, Q6 and Q8.

## Questions

Rate each as a finding or a non-issue.

- **R1. Aliasing by assignment.** The name-based rules follow names bound by imports, not by assignment. `import shutil` then `s = shutil; s.copy2(...)`, or the same with `os.system` or `sqlite3.connect`, isn't flagged. Is that a convenient bypass under the threat model, or deliberate code?
- **R2. Python 3.14.** Its behavior here is reasoned from source (`shutil`, `pathlib`, `concurrent.futures`, `concurrent.interpreters`), not run; no 3.14 interpreter was available. pyproject lists 3.14 as supported.
- **R3. The import refusal's reach.** It stops only a first import by statement. Anything that loaded one of the four before the guard installed keeps it. Is the startup-events test, which covers lasto's own startup, enough?

## How to report

Give a verdict, then each claim Verified or Not and why, then findings. Number them from L15 (or M2 and H1 for Medium and High), each with a file, a function and a concrete scenario. Say what you couldn't check.
