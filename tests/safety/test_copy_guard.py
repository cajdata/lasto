"""Guard v2 and file copies (Step A review finding M1, approved removal).

shutil.copy2 copies through _winapi.CopyFile2 when it exists, and CopyFile2 raises no audit event, so the guard
never saw the destination, which could be a device. copytree and a cross-volume move call copy2, and Python
3.14's pathlib copies call CopyFile2 too. The guard removes CopyFile2 from _winapi as it installs:

- shutil looks it up at each call, so copy2 falls back to copyfile, whose open() the guard checks;
- 3.14's pathlib, loaded after the guard, finds no CopyFile2 and copies through open() (if something loaded it
  earlier, its copy fails with AttributeError: closed, with no write);
- a direct caller fails with AttributeError.

The structural rule in test_structure.py keeps all of these out of src/ as well; this covers dependencies.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import _winapi
import pytest
from helpers import events

import lasto
from lasto.safety.audit import REFUSALS
from lasto.safety.errors import SafetyViolation

REPO = Path(lasto.__file__).resolve().parents[2]
OUTSIDE = REPO / "lasto-copy-guard-probe.txt"  # not a folder a test run may write in


def test_the_guard_removes_copyfile2():
    assert not hasattr(_winapi, "CopyFile2")


def test_a_direct_caller_fails_closed(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("data", encoding="utf-8")
    with pytest.raises(AttributeError):
        _winapi.CopyFile2(str(source), str(tmp_path / "copy.txt"), 0)
    assert not (tmp_path / "copy.txt").exists()


def test_copy2_still_copies_data_and_times_where_writes_are_allowed(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("data", encoding="utf-8")
    os.utime(source, (1_000_000_000, 1_000_000_000))
    copied = Path(shutil.copy2(source, tmp_path / "copy.txt"))
    assert copied.read_text(encoding="utf-8") == "data"
    assert copied.stat().st_mtime == source.stat().st_mtime  # copystat, after copyfile's open()


def test_a_copy_outside_the_allowed_folders_is_refused_before_the_file_exists(auditor, sink, tmp_path):
    REFUSALS.attach(auditor)
    source = tmp_path / "source.txt"
    source.write_text("data", encoding="utf-8")
    try:
        with pytest.raises(SafetyViolation) as refused:
            shutil.copy2(source, OUTSIDE)
        assert refused.value.reason == "write_outside_the_data_folder"
        assert not OUTSIDE.exists()
    finally:
        OUTSIDE.unlink(missing_ok=True)  # only if a copy got past the guard
    [refusal] = events(sink, "rejected")
    assert refusal["request"].startswith("open ")
