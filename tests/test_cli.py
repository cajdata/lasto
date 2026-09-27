"""The CLI: simulator by default, --live only with an explicit channel or port, no abbreviations."""

import runpy
import sys

import pytest

from lasto import __version__, cli


def run(*argv):
    return cli.main(list(argv))


@pytest.mark.parametrize("command", sorted(cli.COMMANDS))
def test_every_command_defaults_to_the_simulator(command, capsys):
    assert run(command) == 0
    assert "the simulator" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["drive", "--live", "--channel", "PCAN_USBBUS1"],
        ["map", "--live", "--channel", "PCAN_USBBUS16", "--port", "COM5"],
        ["drive", "--live", "--port", "COM12", "--profile", "crank"],
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
    monkeypatch.setattr(sys, "argv", ["lasto", "log"])
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("lasto", run_name="__main__")
    assert exited.value.code == 0
    assert "'log'" in capsys.readouterr().out
