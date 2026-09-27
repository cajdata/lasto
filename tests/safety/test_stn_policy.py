"""Rule 4: every STN adapter command allowlist decision."""

import pytest

from lasto.safety import stn_policy
from lasto.safety.errors import SafetyViolation

ALLOWED = sorted(stn_policy.EXACT_COMMANDS)

# From the OBDLink manual (Rev F): commands that transmit on the bus, initialize it, defeat silent
# monitoring, shape headers or flow control, write the adapter's memory, or disturb the link.
DANGEROUS = [
    "STPX H:7E0, D:0101",  # arbitrary frame
    "STPO",  # open protocol
    "ATRTR",
    "ATSI",
    "ATFI",
    "STIFI 25,25,C133F18166",
    "ATBI",
    "ATSP0",  # automatic search sends 01 00
    "ATSP6",  # also saved to memory
    "ATTP6",
    "STP22",  # K-line with 5-baud init
    "STP24",
    "STP25",
    "STP32",  # 29-bit CAN
    "STPPMA 1000,7DF,013E",  # periodic message
    "ATWM C133F13E",  # custom keep-alive
    "ATSW92",  # keep-alive on
    "STCMM1",  # ACK frames
    "ATCSM0",
    "ATCSM1",  # deprecated; not on the list
    "ATPP21SVFF",
    "ATPP 21 ON",
    "ATPP FF OFF",
    "ATSD 12",
    "AT@3 ABCDEFGHIJKL",
    "STSAVCAL",
    "STVCAL 12.6",
    "ATCV1260",
    "STWBR",
    "STRSTNVM",
    "STSLU 1",
    "STBTDN LASTO",
    "STBR 2000000",
    "STSBR 115200",
    "ATBRD 23",
    "STSLEEP",
    "ATLP",
    "STGPC 1",
    "STBC 1",  # batch mode
    "ATSH 7E0",  # header change
    "ATFCSH 7E0",
    "ATFCSM 1",
    "ATCFC0",
    "ATD",  # restore defaults (not the same as ATD0/ATD1)
    "ATI",
    "ATMA",
    "STM 5",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed_commands(command):
    assert stn_policy.check_command(command) == command


@pytest.mark.parametrize(("spelled", "canonical"), [("stcmm 0", "STCMM0"), ("  ATE0 ", "ATE0"), ("St Vr 2", "STVR2")])
def test_case_and_spaces_are_normalized(spelled, canonical):
    assert stn_policy.check_command(spelled) == canonical


@pytest.mark.parametrize("command", ["STFPA 7E8,7F8", "STFPA 18DAF110,1FFFFFFF", "stfpa 025, 7ff"])
def test_filter_patterns(command):
    assert stn_policy.check_command(command).startswith("STFPA")


@pytest.mark.parametrize("command", ["STFPA 7E,7F8", "STFPA 7E8", "STFPA 7E8,7F8,1", "STFPA 123456789,7F8"])
def test_bad_filter_patterns(command):
    with pytest.raises(SafetyViolation) as refused:
        stn_policy.check_command(command)
    assert refused.value.reason == "adapter_command_not_allowlisted"


@pytest.mark.parametrize("command", DANGEROUS)
def test_dangerous_commands_are_refused(command):
    with pytest.raises(SafetyViolation):
        stn_policy.check_command(command)


@pytest.mark.parametrize("command", ["0100", "01 0C", "3E", "10 03", "abcdef", "7E0", "0 1 0 D"])
def test_hex_lines_are_requests_and_are_refused(command):
    with pytest.raises(SafetyViolation) as refused:
        stn_policy.check_command(command)
    assert refused.value.reason == "adapter_hex_request"


@pytest.mark.parametrize("command", ["", "   ", None, 42, b"STI"])
def test_empty_or_not_text(command):
    with pytest.raises(SafetyViolation) as refused:
        stn_policy.check_command(command)
    assert refused.value.reason == "adapter_command_empty"


@pytest.mark.parametrize("command", ["STI\r", "ATZ\rSTPX", "ST\x08PX", "STI|STPX", "STVR.", "ST-I", "STİ", "ATD\x00"])
def test_characters_outside_the_set_are_refused(command):
    with pytest.raises(SafetyViolation) as refused:
        stn_policy.check_command(command)
    assert refused.value.reason == "adapter_command_characters"


def test_reset_and_monitor_commands_are_marked():
    assert stn_policy.check_command("ATZ") == "ATZ"
    assert stn_policy.is_reset("ATZ") and stn_policy.is_reset("ATWS")
    assert not stn_policy.is_reset("STI")
    assert stn_policy.is_monitor("STMA") and stn_policy.is_monitor("STM")
    assert not stn_policy.is_monitor("STI")


def test_parse_pp_summary():
    text = "00:FF F  01:FF F  02:FF F  03:32 F\r21:FF F  2A:0A N"
    assert stn_policy.parse_pp_summary(text) == {0x00: (0xFF, False), 0x01: (0xFF, False), 0x02: (0xFF, False), 0x03: (0x32, False), 0x21: (0xFF, False), 0x2A: (0x0A, True)}


@pytest.mark.parametrize(
    ("parameters", "silent"),
    [
        ({}, False),  # can't tell: refuse
        ({0x21: (0x00, False)}, True),  # off: factory default FF applies
        ({0x21: (0xFF, True)}, True),
        ({0x21: (0x00, True)}, False),  # on, ACKs by default
    ],
)
def test_silent_by_default(parameters, silent):
    assert stn_policy.silent_by_default(parameters) is silent


def test_protocol_sets():
    assert stn_policy.CAN_MONITOR_PROTOCOLS == {"31", "33"}
    assert stn_policy.KLINE_MONITOR_PROTOCOLS == {"21", "23"}
