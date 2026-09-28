"""Installing lasto must never change anyone else's test runs.

The test plugin (hardware firewall, kill-switch reset between tests) loads
only through this repo's own pytest configuration. As a pytest11 entry point,
pytest would load it into every test run in any environment with lasto
installed.
"""

import tomllib
from importlib.metadata import entry_points
from pathlib import Path

PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"


def test_the_package_declares_no_pytest_entry_point():
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert "pytest11" not in project["project"].get("entry-points", {})


def test_the_installed_package_registers_no_pytest_plugin():
    assert [entry.value for entry in entry_points(group="pytest11") if entry.value.startswith("lasto")] == []


def test_this_repo_loads_the_plugin_by_name():
    addopts = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]["addopts"]
    assert addopts[addopts.index("lasto.sim.pytest_plugin") - 1] == "-p"
