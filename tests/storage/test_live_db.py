"""The live feed: a capture's once-a-second status for other processes (lasto.storage.live_db)."""

from __future__ import annotations

import dataclasses

import pytest

from lasto.storage import live_db, workbench_db
from lasto.storage.live_db import LiveFeed, LiveStatus, read_live
from lasto.storage.root import DataRoot

STATUS = LiveStatus(
    pid=4242,
    run_id="run",
    session_id="session",
    state="capturing",
    interface="simulator",
    channel="PCAN_USBBUS1",
    started_utc="2026-09-26T18:00:00+00:00",
    heartbeat_utc="2026-09-26T18:00:05+00:00",
    frames=1500,
    error_frames=0,
    frames_per_second=300,
    bus="0x00000",
)


@pytest.fixture
def root(tmp_path) -> DataRoot:
    root = DataRoot(tmp_path)
    root.ensure()
    return root


@pytest.fixture
def changes() -> list[str | None]:
    return []


@pytest.fixture
def feed(root, changes):
    feed = LiveFeed(root.live_db, on_change=changes.append)
    yield feed
    feed.close()


def test_the_status_is_published_for_readers(root, feed):
    feed.publish(STATUS)
    assert read_live(root) == STATUS
    later = dataclasses.replace(STATUS, heartbeat_utc="2026-09-26T18:00:06+00:00", session_id=None, state="waiting")
    feed.publish(later)
    assert read_live(root) == later  # one row, replaced each second


def test_the_live_feed_can_be_rebuilt_so_it_is_written_without_waiting_for_the_disk(root, feed):
    feed.publish(STATUS)
    assert feed._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_there_is_nothing_to_read_before_a_capture_publishes(root, feed):
    assert read_live(root) is None
    feed.publish(STATUS)
    feed._conn.execute("DELETE FROM status")
    assert read_live(root) is None


def test_a_failure_is_reported_once_and_never_stops_the_capture(root, feed, changes):
    root.live_db.mkdir()  # a folder where the file should be: SQLite can't open it
    feed.publish(STATUS)
    feed.publish(STATUS)
    assert len(changes) == 1 and changes[0] is not None
    root.live_db.rmdir()
    feed.publish(STATUS)  # tried again each second, and it's back
    assert changes[1:] == [None]
    assert read_live(root) == STATUS


def test_a_connection_that_fails_after_it_opened_is_opened_again(root, feed, changes):
    feed.publish(STATUS)
    feed._conn.close()
    feed.publish(STATUS)
    feed.publish(STATUS)
    assert len(changes) == 2 and changes[1] is None


def test_another_kind_of_database_is_not_written(root, feed, changes):
    workbench_db.open_workbench(root).close()
    root.workbench_db.rename(root.live_db)
    feed.publish(STATUS)
    assert len(changes) == 1 and "live" in changes[0]
    assert live_db.SCHEMA.application_id != workbench_db.SCHEMA.application_id


def test_closing_twice_does_nothing(root, changes):
    feed = LiveFeed(root.live_db, on_change=changes.append)
    feed.publish(STATUS)
    feed.close()
    feed.publish(dataclasses.replace(STATUS, frames=1))  # a closed feed publishes nothing more
    feed.close()
    assert changes == [] and read_live(root) == STATUS
