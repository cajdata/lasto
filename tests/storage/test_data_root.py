"""Where lasto keeps its data (lasto.storage.root)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lasto.storage.root import DataRoot, names_a_device, stored_path_problem


def test_an_explicit_folder_comes_first(tmp_path):
    environ = {"LASTO_DATA": str(tmp_path / "env"), "LOCALAPPDATA": str(tmp_path / "local")}
    assert DataRoot.locate(str(tmp_path / "chosen"), environ=environ).path == (tmp_path / "chosen").resolve()


def test_then_the_environment(tmp_path):
    environ = {"LASTO_DATA": str(tmp_path / "env"), "LOCALAPPDATA": str(tmp_path / "local")}
    assert DataRoot.locate(None, environ=environ).path == (tmp_path / "env").resolve()


def test_then_local_app_data(tmp_path):
    assert DataRoot.locate(None, environ={"LOCALAPPDATA": str(tmp_path)}).path == (tmp_path / "lasto").resolve()


def test_without_local_app_data_the_home_folder():
    assert DataRoot.locate(None, environ={}).path == (Path.home() / "AppData" / "Local" / "lasto").resolve()


def test_the_files_under_the_root(tmp_path):
    root = DataRoot(tmp_path)
    assert (root.capture_db, root.workbench_db, root.live_db) == (
        tmp_path / "capture.sqlite",
        tmp_path / "workbench.sqlite",
        tmp_path / "live.sqlite",
    )
    assert (root.audit_dir, root.sessions_dir) == (tmp_path / "audit", tmp_path / "sessions")
    assert (root.lock_file, root.stop_file) == (tmp_path / "capture.lock", tmp_path / "capture.stop")


def test_ensure_makes_the_folders(tmp_path):
    root = DataRoot(tmp_path / "new")
    root.ensure()
    assert root.audit_dir.is_dir() and root.sessions_dir.is_dir()


def test_paths_are_stored_relative_to_the_root(tmp_path):
    root = DataRoot(tmp_path)
    segment = root.sessions_dir / "2026-09-28" / "abc" / "seg-0001.candump.zst"
    assert root.relative(segment) == "sessions/2026-09-28/abc/seg-0001.candump.zst"
    assert root.absolute("sessions/2026-09-28/abc/seg-0001.candump.zst") == segment


@pytest.mark.parametrize("stored", ["../elsewhere/x", "/x", "C:/x", r"C:\x", r"sessions\x", "", "sessions/../../x"])
def test_a_stored_path_must_stay_under_the_root(tmp_path, stored):
    assert stored_path_problem(stored)
    with pytest.raises(ValueError):
        DataRoot(tmp_path).absolute(stored)


def test_a_path_outside_the_root_is_not_stored(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        DataRoot(tmp_path / "root").relative(tmp_path / "elsewhere" / "x")


@pytest.mark.parametrize(
    "folder",
    [
        "COM5",
        r"D:\data\COM1",
        "C:COM5",  # a port name right after a drive letter
        r"\\.\COM5",
        "//./COM5",
        r"\\.\pipe\lasto",
        r"\\?\GLOBALROOT\Device\Serial0",
        r"\??\C:\data",
        r"C:\data\aux.txt",
        "NUL",
        r"C:\data\LPT1\x",
    ],
)
def test_a_device_is_never_the_data_folder(folder):
    """The data folder is typed by the user, and SQLite opens its files itself, past the serial guard's hook."""
    assert names_a_device(folder)
    with pytest.raises(ValueError, match="device"):
        DataRoot.locate(folder, environ={})


@pytest.mark.parametrize("folder", [r"C:\Users\Chris\AppData\Local\lasto", "D:/lasto data/2026", "commute", "auxiliary"])
def test_ordinary_folders_are_fine(folder):
    assert not names_a_device(folder)
