"""Rules 8, 9, 10: motion interlock, battery guard, sensitive ECUs."""

import pytest

from lasto.safety import requests as rq
from lasto.safety.ecus import ENGINE, Ecu, EcuKind
from lasto.safety.errors import SafetyViolation
from lasto.safety.interlocks import Interlocks, is_probe
from lasto.safety.requests import DtcKind, Purpose

NOW = 100.0
SRS = Ecu("airbag", EcuKind.SRS, 0x780, 0x788, None, "test only")
IMMOBILIZER = Ecu("immobilizer", EcuKind.IMMOBILIZER, 0x7A5, 0x7AD, None, "test only")
TRANSMISSION = Ecu("transmission", EcuKind.TRANSMISSION, 0x7E1, 0x7E9, None, "test only")
PROFILE_REQUEST = rq.read_pid([0x0C], purpose=Purpose.LOGGING, ecu=ENGINE)
PROFILE = frozenset({PROFILE_REQUEST.key})


def parked(*, volts=12.6, rpm=0.0, speed=0, age=0.0, source="poll"):
    interlocks = Interlocks()
    interlocks.update_speed(speed, NOW - age, source=source)
    interlocks.update_voltage(volts, NOW)
    interlocks.update_rpm(rpm, NOW)
    return interlocks


def refuse(interlocks, request, reason):
    with pytest.raises(SafetyViolation) as refused:
        interlocks.check(request, NOW, PROFILE)
    assert refused.value.reason == reason


def test_unknown_speed_counts_as_moving():
    assert not Interlocks().stationary(NOW)


@pytest.mark.parametrize(("source", "age", "expected"), [("poll", 1.0, True), ("poll", 1.01, False), ("broadcast", 0.5, True), ("broadcast", 0.51, False)])
def test_speed_freshness_by_source(source, age, expected):
    assert parked(source=source, age=age).stationary(NOW) is expected


def test_moving_is_not_stationary():
    assert not parked(speed=3).stationary(NOW)


def test_unknown_speed_source():
    with pytest.raises(ValueError):
        Interlocks().update_speed(0, NOW, source="guess")


def test_engine_off():
    interlocks = Interlocks()
    assert interlocks.engine_off(NOW)  # unknown RPM counts as off
    interlocks.update_rpm(700, NOW)
    assert not interlocks.engine_off(NOW)
    assert interlocks.engine_off(NOW + 2.01)  # stale
    interlocks.update_rpm(0, NOW)
    assert interlocks.engine_off(NOW)


def test_battery():
    interlocks = Interlocks()
    assert not interlocks.battery_ok(NOW)  # unknown counts as low
    interlocks.update_voltage(12.0, NOW)
    assert interlocks.battery_ok(NOW)
    assert not interlocks.battery_ok(NOW + 2.01)
    interlocks.update_voltage(11.99, NOW)
    assert not interlocks.battery_ok(NOW)


def test_logging_needs_the_profile():
    interlocks = Interlocks()  # nothing known: logging doesn't depend on speed
    interlocks.check(PROFILE_REQUEST, NOW, PROFILE)
    refuse(interlocks, rq.read_pid([0x0D], purpose=Purpose.LOGGING, ecu=ENGINE), "not_in_logging_profile")


def test_probes_are_allowed_while_moving():
    interlocks = parked(speed=100)
    for pid in (0x0C, 0x0D, 0x42):
        interlocks.check(rq.interlock_probe(pid), NOW, PROFILE)
        interlocks.check(rq.interlock_probe(pid, ecu=ENGINE), NOW, PROFILE)


def test_probe_purpose_with_other_payloads_is_refused():
    refuse(Interlocks(), rq.read_pid([0x05], purpose=Purpose.INTERLOCK_PROBE), "not_an_interlock_probe")


def test_is_probe():
    assert is_probe(rq.interlock_probe(0x0D))
    assert not is_probe(rq.read_pid([0x0D, 0x0C], purpose=Purpose.INTERLOCK_PROBE))
    assert not is_probe(rq.read_vehicle_info(0x0D, purpose=Purpose.INTERLOCK_PROBE))
    assert not is_probe(rq.read_pid([0x05], purpose=Purpose.INTERLOCK_PROBE))
    assert not is_probe(rq.read_pid([0x0D], purpose=Purpose.INTERLOCK_PROBE, ecu=TRANSMISSION))


@pytest.mark.parametrize("purpose", [Purpose.SNAPSHOT, Purpose.IDENTIFY, Purpose.DISCOVERY])
def test_parked_purposes_need_a_fresh_zero_speed(purpose):
    request = rq.read_dtcs(DtcKind.STORED, purpose=purpose)
    refuse(Interlocks(), request, "vehicle_not_confirmed_stationary")
    refuse(parked(speed=1), request, "vehicle_not_confirmed_stationary")
    refuse(parked(age=1.5), request, "vehicle_not_confirmed_stationary")
    parked().check(request, NOW, PROFILE)


@pytest.mark.parametrize("purpose", [Purpose.SNAPSHOT, Purpose.IDENTIFY, Purpose.DISCOVERY])
def test_battery_guard_with_engine_off(purpose):
    request = rq.read_mode06(0x01, purpose=purpose)
    refuse(parked(volts=11.8), request, "battery_low_or_unknown")
    interlocks = Interlocks()
    interlocks.update_speed(0, NOW, source="poll")
    refuse(interlocks, request, "battery_low_or_unknown")  # voltage unknown
    parked(volts=11.8, rpm=650).check(request, NOW, PROFILE)  # engine running: the guard doesn't apply


@pytest.mark.parametrize("ecu", [SRS, IMMOBILIZER])
def test_sensitive_ecus(ecu):
    interlocks = parked()
    interlocks.check(rq.read_dtcs_kwp(ecu, purpose=Purpose.SNAPSHOT), NOW, PROFILE)
    interlocks.check(rq.read_dtc_information(ecu, 0x02, 0xFF, purpose=Purpose.SNAPSHOT), NOW, PROFILE)
    refuse(interlocks, rq.read_local_id(ecu, 0x01, purpose=Purpose.SNAPSHOT), "sensitive_ecu_dtc_reads_only")
    refuse(interlocks, rq.read_ecu_id(ecu, 0x88, purpose=Purpose.IDENTIFY), "sensitive_ecu_dtc_reads_only")
    refuse(interlocks, rq.read_dtcs_kwp(ecu, purpose=Purpose.DISCOVERY), "sensitive_ecu_excluded_from_discovery")
