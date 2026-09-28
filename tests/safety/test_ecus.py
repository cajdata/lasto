"""Rule 2: the approved request IDs."""

import dataclasses

import pytest
from helpers import LooksLikeTheEngineId, rewritten

from lasto.safety import ecus, policy
from lasto.safety.ecus import ENGINE, Ecu, EcuKind
from lasto.safety.errors import SafetyViolation

MODE_01_RPM = bytes.fromhex("02010C0000000000")


def test_only_the_engine_is_approved_so_far():
    assert ecus.APPROVED_ECUS == (ENGINE,)
    assert (ENGINE.request_id, ENGINE.response_id, ENGINE.ext_address) == (0x7E0, 0x7E8, None)


def test_table_invariants():
    request_ids = [ecu.request_id for ecu in ecus.APPROVED_ECUS]
    assert len(set(request_ids)) == len(request_ids)
    for ecu in ecus.APPROVED_ECUS:
        assert 0x700 <= ecu.request_id <= 0x7FF
        assert ecu.request_id != policy.FUNCTIONAL_REQUEST_ID
        assert ecu.response_id == ecu.request_id + 8
        assert isinstance(ecu.kind, EcuKind)
        assert ecu.evidence.strip()


def test_lookups():
    engine = (0x7E0, 0x7E8, None, EcuKind.ENGINE)
    assert ecus.by_request_id(0x7E0) == engine and ecus.by_request_id(0x7E1) is None
    assert ecus.by_response_id(0x7E8) == engine and ecus.by_response_id(0x7E9) is None
    assert ecus.route_of(ENGINE) == engine and ecus.route_of(None) is None
    assert ecus.REQUEST_IDS == {0x7E0}
    assert ecus.FUNCTIONAL_RESPONSE_IDS == set(range(0x7E8, 0x7F0))


def test_each_route_is_a_tuple_copied_from_its_entry():
    """Finding P1: what the policy and the gate use is copied at import into a tuple, which can't change in place."""
    route = ecus.route_of(ENGINE)
    assert isinstance(route, tuple) and route is ecus.by_request_id(0x7E0) is ecus.by_response_id(0x7E8)
    assert (route.request_id, route.response_id, route.ext_address, route.kind) == (0x7E0, 0x7E8, None, EcuKind.ENGINE)
    assert [ecus.route_of(ecu) for ecu in ecus.APPROVED_ECUS] == [ecus.by_request_id(ecu.request_id) for ecu in ecus.APPROVED_ECUS]


def test_a_rewritten_entry_changes_no_route():
    with rewritten(ENGINE, request_id=0x0B0, response_id=0x0B8, ext_address=0x40, kind=EcuKind.SRS):
        assert ecus.route_of(ENGINE) == (0x7E0, 0x7E8, None, EcuKind.ENGINE)
        assert ecus.REQUEST_IDS == {0x7E0}
        assert ecus.by_request_id(0x0B0) is None and ecus.by_response_id(0x0B8) is None


def test_the_policy_ignores_a_rewritten_entry():
    """Finding P1: rewriting the approved entry in place used to move lasto onto any 11-bit ID."""
    with rewritten(ENGINE, request_id=0x0B0, ext_address=0x40, kind=EcuKind.SRS):
        assert policy.request_ids() == {0x7DF, 0x7E0}
        with pytest.raises(SafetyViolation) as caught:
            policy.check_frame(0x0B0, MODE_01_RPM)
        assert caught.value.reason == "can_id_not_allowlisted"
        # Still the engine, with normal addressing: the routes, not the rewritten entry, decide.
        assert policy.check_frame(0x7E0, MODE_01_RPM) == "request"


def test_forged_entries_are_not_approved():
    assert ecus.is_approved(ENGINE)
    forged = Ecu("transmission", EcuKind.TRANSMISSION, 0x7E1, 0x7E9, None, "made up")
    assert not ecus.is_approved(forged)
    assert not ecus.is_approved(0x7E0)


def test_approval_is_by_identity_not_equality():
    """Finding #4: only the table's own entries are approved, not copies or look-alikes."""
    copy = dataclasses.replace(ENGINE)
    look_alike = dataclasses.replace(ENGINE, request_id=LooksLikeTheEngineId(0x7E1))
    assert copy == ENGINE and look_alike == ENGINE  # equal by value...
    assert not ecus.is_approved(copy)  # ...but not the approved entry
    assert not ecus.is_approved(look_alike)


def test_sensitive_kinds():
    assert ecus.SENSITIVE_KINDS == {EcuKind.SRS, EcuKind.IMMOBILIZER}
