"""Retention: the data folder's disk budget, and the warnings a capture gives at start (lasto.storage.retention)."""

from __future__ import annotations

import os
from collections import namedtuple

import pytest

from lasto.storage import retention
from lasto.storage.retention import BUDGET_BYTES, GIB, LOW_FREE_BYTES, check_disk, data_folder_size
from lasto.storage.root import DataRoot

Usage = namedtuple("Usage", "total used free")


def disk(free: int):
    return lambda path: Usage(444 * GIB, 444 * GIB - free, free)


@pytest.fixture
def root(tmp_path) -> DataRoot:
    root = DataRoot(tmp_path / "data")
    root.ensure()
    return root


def test_the_approved_budget():
    """Owner's decision, 2026-10-01: 20 GB for the data folder, and a warning below 2 GB free."""
    assert (BUDGET_BYTES, LOW_FREE_BYTES) == (20 * GIB, 2 * GIB)


def test_the_data_folder_size_counts_every_file_in_it(root):
    (root.audit_dir / "run.jsonl").write_bytes(b"x" * 1000)
    (root.sessions_dir / "2026-10-01" / "s").mkdir(parents=True)
    (root.sessions_dir / "2026-10-01" / "s" / "seg-0001.candump.zst").write_bytes(b"y" * 2500)
    assert data_folder_size(root) == 3500


def test_a_file_that_goes_away_while_counting_is_skipped(root, monkeypatch):
    (root.audit_dir / "a.jsonl").write_bytes(b"x" * 10)
    (root.audit_dir / "b.jsonl").write_bytes(b"x" * 20)
    real = os.path.getsize

    def getsize(path):
        if str(path).endswith("a.jsonl"):
            raise FileNotFoundError(path)
        return real(path)

    monkeypatch.setattr(retention.os.path, "getsize", getsize)
    assert data_folder_size(root) == 20


def test_plenty_of_room_means_no_warning(root):
    check = check_disk(root, disk_usage=disk(69 * GIB))
    assert check.warnings == () and check.free_bytes == 69 * GIB and check.used_bytes == 0


def test_a_nearly_full_disk_is_a_warning_and_never_a_refusal(root):
    [warning] = check_disk(root, disk_usage=disk(GIB)).warnings
    assert "1.0 GB free" in warning


def test_a_data_folder_over_its_budget_is_a_warning(root):
    (root.audit_dir / "run.jsonl").write_bytes(b"x" * 2048)
    [warning] = check_disk(root, disk_usage=disk(69 * GIB), budget_bytes=1024).warnings
    assert "over its budget" in warning and "nothing is deleted" in warning


def test_the_summary_line(root):
    check = check_disk(root, disk_usage=disk(69 * GIB + GIB // 2))
    assert check.summary() == "69.5 GB free on the data folder's disk; the data folder uses 0.0 GB of its 20 GB budget."
