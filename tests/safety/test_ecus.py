"""Rule 2: the approved request IDs."""

import dataclasses

from helpers import LooksLikeTheEngineId

from lasto.safety import ecus, policy
from lasto.safety.ecus import ENGINE, Ecu, EcuKind


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
    assert ecus.by_request_id(0x7E0) is ENGINE
    assert ecus.by_request_id(0x7E1) is None
    assert ecus.by_response_id(0x7E8) is ENGINE
    assert ecus.by_response_id(0x7E9) is None
    assert ecus.FUNCTIONAL_RESPONSE_IDS == set(range(0x7E8, 0x7F0))


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
