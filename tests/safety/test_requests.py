"""Rule 5: typed request builders, and nothing else, make requests."""

import dataclasses

import pytest

from lasto.safety import requests as rq
from lasto.safety.ecus import ENGINE
from lasto.safety.errors import SafetyViolation
from lasto.safety.requests import DtcKind, Purpose

P = Purpose.SNAPSHOT


@pytest.mark.parametrize(
    ("request_", "target", "payload"),
    [
        (rq.read_pid([0x0C], purpose=P), None, "010C"),
        (rq.read_pid([0x0C, 0x0D, 0x05, 0x0F, 0x10, 0x11], purpose=P, ecu=ENGINE), ENGINE, "010C0D050F1011"),
        (rq.read_freeze_frame(0x0C, purpose=P), None, "020C00"),
        (rq.read_freeze_frame(0x0C, frame=1, purpose=P, ecu=ENGINE), ENGINE, "020C01"),
        (rq.read_dtcs(DtcKind.STORED, purpose=P), None, "03"),
        (rq.read_dtcs(DtcKind.PENDING, purpose=P), None, "07"),
        (rq.read_dtcs(DtcKind.PERMANENT, purpose=P, ecu=ENGINE), ENGINE, "0A"),
        (rq.read_mode06(0x21, purpose=P), None, "0621"),
        (rq.read_vehicle_info(0x02, purpose=P), None, "0902"),
        (rq.read_local_id(ENGINE, 0xD9, purpose=P), ENGINE, "21D9"),
        (rq.read_did(ENGINE, 0xF190, purpose=P), ENGINE, "22F190"),
        (rq.read_ecu_id(ENGINE, 0x88, purpose=P), ENGINE, "1A88"),
        (rq.read_dtcs_kwp(ENGINE, purpose=P), ENGINE, "13FF00"),
        (rq.read_dtc_status(ENGINE, 0x0420, purpose=P), ENGINE, "170420"),
        (rq.read_dtcs_by_status(ENGINE, purpose=P), ENGINE, "1800FF00"),
        (rq.read_dtcs_by_status(ENGINE, status=0x02, group=0x0000, purpose=P), ENGINE, "18020000"),
        (rq.read_dtc_information(ENGINE, 0x02, 0xFF, purpose=P), ENGINE, "1902FF"),
        (rq.interlock_probe(0x0D), None, "010D"),
        (rq.interlock_probe(0x42, ecu=ENGINE), ENGINE, "0142"),
    ],
)
def test_builders(request_, target, payload):
    assert request_.target == target
    assert request_.payload == bytes.fromhex(payload)
    assert request_.service == request_.payload[0]
    assert request_.key == (None if target is None else target.name, request_.payload)


def test_probe_purpose():
    assert rq.interlock_probe(0x0C).purpose is Purpose.INTERLOCK_PROBE


@pytest.mark.parametrize("bad", [-1, 256, 1.0, "0C", True, None])
def test_bytes_must_be_integers_in_range(bad):
    with pytest.raises(ValueError):
        rq.read_pid([bad], purpose=P)


@pytest.mark.parametrize("bad", [-1, 0x10000, "F190", False])
def test_words_must_be_integers_in_range(bad):
    with pytest.raises(ValueError):
        rq.read_did(ENGINE, bad, purpose=P)


@pytest.mark.parametrize("count", [0, 7])
def test_pid_count(count):
    with pytest.raises(ValueError):
        rq.read_pid(range(1, count + 1), purpose=P)


def test_ecu_must_be_an_ecu_entry():
    with pytest.raises(TypeError):
        rq.read_local_id(0x7E0, 0x01, purpose=P)  # a raw CAN ID is not accepted
    with pytest.raises(TypeError):
        rq.read_pid([0x0C], purpose=P, ecu="engine")


def test_purpose_must_be_a_purpose():
    with pytest.raises(TypeError):
        rq.read_pid([0x0C], purpose="logging")


def test_dtc_kind_must_be_a_dtc_kind():
    with pytest.raises(TypeError):
        rq.read_dtcs(0x04, purpose=P)  # can't smuggle in "clear DTCs"


def test_dtc_information_parameter_limit():
    with pytest.raises(ValueError):
        rq.read_dtc_information(ENGINE, 0x02, 1, 2, 3, 4, 5, 6, purpose=P)


def test_probe_pids_only():
    with pytest.raises(ValueError):
        rq.interlock_probe(0x05)


def test_requests_cannot_be_made_directly():
    with pytest.raises(TypeError):
        rq.Request(None, b"\x01\x0c", Purpose.LOGGING)
    with pytest.raises(TypeError):
        rq.Request(None, b"\x01\x0c", Purpose.LOGGING, object())


def test_copies_are_rechecked():
    request = rq.read_local_id(ENGINE, 0x01, purpose=P)
    with pytest.raises(SafetyViolation) as refused:
        dataclasses.replace(request, payload=b"\x10\x03")
    assert refused.value.reason == "service_never_allowed"
    with pytest.raises(SafetyViolation) as refused:
        dataclasses.replace(request, target=None)
    assert refused.value.reason == "manufacturer_service_on_functional_id"
    with pytest.raises(ValueError):
        dataclasses.replace(request, payload=b"")
    with pytest.raises(ValueError):
        dataclasses.replace(request, payload=bytes(8))


def test_requests_are_immutable():
    request = rq.read_pid([0x0C], purpose=P)
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.payload = b"\x04"  # type: ignore[misc]
