"""Shared fixtures. The hardware firewall and the simulator violation check come from lasto.sim.pytest_plugin."""

from __future__ import annotations

import os

import pytest
from hypothesis import HealthCheck, settings

from lasto.safety.audit import Auditor, MemoryAuditSink
from lasto.sim.clock import FakeClock
from lasto.sim.vehicle import Sim, build_sim

_suppress = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]
settings.register_profile("lasto", deadline=None, max_examples=150, suppress_health_check=_suppress)
# HYPOTHESIS_PROFILE=thorough python -m pytest tests/safety/test_properties.py  (slower, many more examples)
settings.register_profile("thorough", deadline=None, max_examples=3000, suppress_health_check=_suppress)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "lasto"))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sink() -> MemoryAuditSink:
    return MemoryAuditSink()


@pytest.fixture
def auditor(sink: MemoryAuditSink, clock: FakeClock) -> Auditor:
    return Auditor(sink, clock)


@pytest.fixture
def sim(clock: FakeClock) -> Sim:
    return build_sim(clock)
