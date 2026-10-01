"""The CLI: simulator by default, --live only with an explicit channel or port, no abbreviations.

Commands that aren't built yet print which phase brings them. drive and log have their own tests
(test_cli_commands.py); no test here runs them.
"""

import runpy
import sys

import pytest

from lasto import __version__, cli

NOT_BUILT_YET = sorted(set(cli.COMMANDS) - cli.BUILT)


def run(*argv):
    return cli.main(list(argv))


def test_what_is_built():
    assert cli.BUILT == {"drive", "log"}


@pytest.mark.parametrize("command", NOT_BUILT_YET)
def test_every_command_defaults_to_the_simulator(command, capsys):
    assert run(command) == 0
    assert "the simulator" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["map", "--live", "--channel", "PCAN_USBBUS16", "--port", "COM5"],
        ["snapshot", "--live", "--channel", "PCAN_USBBUS1"],
        ["discover", "--live", "--port", "COM12"],
    ],
)
def test_live_needs_an_explicit_channel_or_port(argv, capsys):
    assert run(*argv) == 0
    assert "real hardware" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["drive", "--live"],  # no channel or port
        ["drive", "--channel", "PCAN_USBBUS1"],  # channel without --live
        ["drive", "--port", "COM5"],
        ["drive", "--live", "--channel", "PCAN_USBBUS17"],
        ["drive", "--live", "--channel", "PCAN_PCIBUS1"],
        ["drive", "--live", "--port", "/dev/ttyUSB0"],
        ["drive", "--live", "--port", "com5"],
        ["drive", "--liv", "--channel", "PCAN_USBBUS1"],  # abbreviations are not accepted
        ["drive", "--li", "--channel", "PCAN_USBBUS1"],
        ["drive", "--live", "--chan", "PCAN_USBBUS1"],
        ["log", "--live", "--channel", "PCAN_USBBUS1"],  # offline commands have no --live
        ["dri"],
        [],
        ["drive", "--seconds", "0"],
        ["drive", "--seconds", "-5"],
        ["drive", "--seconds", "soon"],
        ["drive", "--seconds", "nan"],
        ["drive", "--sec", "5"],
        ["drive", "--live", "--port", "COM12"],  # drive captures from the PCAN-USB only, for now
        ["drive", "--live", "--channel", "PCAN_USBBUS1", "--port", "COM12"],
        ["drive", "--profile", "crank"],  # polled logging profiles come in Phase 4
        ["drive", "--live", "--channel", "PCAN_USBBUS1", "--profile", "crank"],
        ["log", "--bu"],
    ],
)
def test_rejected_arguments(argv):
    with pytest.raises(SystemExit) as exited:
        run(*argv)
    assert exited.value.code == 2


def test_version(capsys):
    with pytest.raises(SystemExit):
        run("--version")
    assert capsys.readouterr().out.strip() == f"lasto {__version__}"


def test_python_dash_m(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["lasto", "report"])
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("lasto", run_name="__main__")
    assert exited.value.code == 0
    assert "'report'" in capsys.readouterr().out
