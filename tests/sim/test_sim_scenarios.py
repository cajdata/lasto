"""Simulator scenarios: the key switch, actions at a set time, and a busier bus (lasto.sim)."""

from __future__ import annotations

import pytest

from lasto.safety import pcan_constants as pc
from lasto.sim.bus import Broadcaster, SimBus
from lasto.sim.clock import FakeClock
from lasto.sim.vehicle import build_sim


def frames_between(times: list[float], start: float, end: float) -> int:
    return sum(start <= time < end for time in times)


def test_actions_run_at_their_time_among_the_frames():
    clock = FakeClock()
    bus = SimBus(clock)
    seen: list[object] = []
    bus.add_tap("tap", lambda time, can_id, data: seen.append(round(time - 1000, 3)))
    bus.add_broadcaster(Broadcaster(0x100, 0.010, lambda t: b"\x00"))
    bus.call_at(1000.015, lambda: seen.append("action"))
    clock.advance(0.025)
    bus.advance()
    assert seen == [0.0, 0.01, "action", 0.02]


def test_an_action_already_due_runs_at_the_next_advance():
    clock = FakeClock()
    bus = SimBus(clock)
    ran: list[str] = []
    clock.advance(1.0)
    bus.call_at(1000.5, lambda: ran.append("late"))
    assert ran == []
    bus.advance()
    assert ran == ["late"]


def test_key_off_silences_the_vehicle_and_key_on_brings_it_back_on_schedule():
    sim = build_sim()
    times: list[float] = []
    sim.bus.add_tap("tap", lambda time, can_id, data: times.append(time))
    sim.bus.call_at(1001.0005, sim.vehicle.key_off)
    sim.bus.call_at(1003.0005, sim.vehicle.key_on)
    sim.clock.advance(4.0)
    sim.bus.advance()
    assert frames_between(times, 1001.001, 1003.0) == 0
    before = frames_between(times, 1000.1005, 1000.9005)
    after = frames_between(times, 1003.1005, 1003.9005)
    assert before == after == 240  # 300 frames a second, at the same rates after key on
    assert not sim.vehicle.key_is_off


def test_background_traffic_makes_the_bus_as_busy_as_asked():
    sim = build_sim()
    sim.vehicle.add_background_traffic(1700)
    times: list[float] = []
    ids: set[int] = set()
    sim.bus.add_tap("tap", lambda time, can_id, data: (times.append(time), ids.add(can_id)))
    sim.clock.advance(2.0)
    sim.bus.advance()
    assert frames_between(times, 1000.5005, 1001.5005) == 2000
    assert not ids & ({0x7DF} | set(range(0x7E0, 0x7F0)))  # never a diagnostic ID
    sim.vehicle.key_off()
    count = len(times)
    sim.clock.advance(1.0)
    sim.bus.advance()
    assert len(times) == count  # the key switch silences it too


def test_an_unplugged_adapter_hears_nothing():
    """Frames the bus carries after an unplug never reach the channel's receive queue, even if it's still initialized."""
    sim = build_sim()
    handle = pc.USB_CHANNELS[1]
    channel = sim.dll.channel(handle)
    channel.initialized = True
    channel.receive(1000.0, 0x025, b"\x01")
    sim.dll.unplug(handle)
    channel.receive(1000.01, 0x025, b"\x02")
    assert [data for *_, data, _time in channel.rx] == [b"\x01"]


def test_background_traffic_never_reaches_the_diagnostic_ids():
    with pytest.raises(ValueError, match="at most 102400"):
        build_sim().vehicle.add_background_traffic(102_500)
