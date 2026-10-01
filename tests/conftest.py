"""Shared fixtures. The hardware firewall and the simulator violation check come from lasto.sim.pytest_plugin."""

from __future__ import annotations

import os

import pytest
from coverage_gates import GATES, check
from hypothesis import HealthCheck, settings

from lasto.safety.audit import REFUSALS, Auditor, JsonlAuditSink, MemoryAuditSink
from lasto.sim.clock import FakeClock
from lasto.sim.vehicle import Sim, build_sim

_suppress = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]
settings.register_profile("lasto", deadline=None, max_examples=150, suppress_health_check=_suppress)
# HYPOTHESIS_PROFILE=thorough python -m pytest tests/safety/test_properties.py  (slower, many more examples)
settings.register_profile("thorough", deadline=None, max_examples=3000, suppress_health_check=_suppress)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "lasto"))


@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session: pytest.Session):
    """After pytest-cov has measured and saved: fail the run if a gated package is below its gate.

    This wrapper is registered after pytest-cov's, so it runs outside it: by the time the loop returns
    here, the coverage data is complete. With --no-cov there is nothing to check.
    """
    result = yield
    controller = getattr(session.config.pluginmanager.getplugin("_cov"), "cov_controller", None)
    if controller is None or session.config.option.collectonly:
        return result
    lines, failures = check(controller.cov, GATES)
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    reporter.write_sep("-", "coverage gates (tests/coverage_gates.py)")
    for line in lines:
        reporter.write_line(line)
    for failure in failures:
        reporter.write_line(f"ERROR: {failure}", red=True, bold=True)
        session.testsfailed += 1  # the run exits as failed
    return result


@pytest.fixture(autouse=True, scope="session")
def _no_test_touches_the_real_data_folder(tmp_path_factory):
    """lasto's data folder defaults to %LOCALAPPDATA%\\lasto. In tests the default is a temporary folder instead;
    a test that needs its own passes --data or a DataRoot."""
    patch = pytest.MonkeyPatch()
    patch.setenv("LASTO_DATA", str(tmp_path_factory.mktemp("lasto-data")))
    yield
    patch.undo()


@pytest.fixture(autouse=True)
def _fresh_refusal_log():
    """Each test starts with nothing attached to the process-wide refusal log and nothing held."""
    REFUSALS.reset()
    yield
    REFUSALS.reset()


@pytest.fixture
def opened():
    """Wrap each database connection a test opens, opened(conn), and it is closed when the test ends."""
    connections = []

    def keep(conn):
        connections.append(conn)
        return conn

    yield keep
    for conn in connections:
        conn.close()


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
def durable_auditor(tmp_path, clock: FakeClock):
    """An audit log that keeps its records on disk, which real hardware requires (finding L5)."""
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    yield Auditor(sink, clock)
    sink.close()


@pytest.fixture
def sim(clock: FakeClock) -> Sim:
    return build_sim(clock)
