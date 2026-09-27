"""Rule 3: every service allowlist decision, checked against the lists in the project spec."""

import pytest

from lasto.safety import policy
from lasto.safety.errors import SafetyViolation

# Copied from the spec, not from the code under test.
SPEC_OBD = {0x01, 0x02, 0x03, 0x06, 0x07, 0x09, 0x0A}
SPEC_MANUFACTURER = {0x21, 0x22, 0x1A, 0x13, 0x17, 0x18, 0x19}
SPEC_NEVER = {
    0x04, 0x14, 0x08, 0x10, 0x11, 0x27, 0x28, 0x2C, 0x2E, 0x2F, 0x30,
    0x31, 0x3B, 0x3D, 0x23, 0x34, 0x35, 0x36, 0x37, 0x38, 0x3E, 0x85,
}  # fmt: skip
SPEC_DTC_READS = {0x03, 0x07, 0x0A, 0x13, 0x17, 0x18, 0x19}
EVERYTHING_ELSE = sorted(set(range(256)) - SPEC_OBD - SPEC_MANUFACTURER - SPEC_NEVER)


def test_lists_match_the_spec():
    assert policy.OBD_SERVICES == SPEC_OBD
    assert policy.MANUFACTURER_READ_SERVICES == SPEC_MANUFACTURER
    assert policy.NEVER_SERVICES == SPEC_NEVER
    assert policy.ALLOWED_SERVICES == SPEC_OBD | SPEC_MANUFACTURER
    assert policy.DTC_READ_SERVICES == SPEC_DTC_READS


def test_nothing_is_both_allowed_and_never_allowed():
    assert not policy.ALLOWED_SERVICES & policy.NEVER_SERVICES


def test_functional_id_and_probe_pids():
    assert policy.FUNCTIONAL_REQUEST_ID == 0x7DF
    assert policy.PROBE_PIDS == {0x0C, 0x0D, 0x42}


@pytest.mark.parametrize("service", sorted(SPEC_OBD))
def test_obd_services_are_allowed_functional_and_physical(service):
    policy.check_service(service, functional=True)
    policy.check_service(service, functional=False)


@pytest.mark.parametrize("service", sorted(SPEC_MANUFACTURER))
def test_manufacturer_services_only_go_to_a_specific_ecu(service):
    policy.check_service(service, functional=False)
    with pytest.raises(SafetyViolation) as refused:
        policy.check_service(service, functional=True)
    assert refused.value.reason == "manufacturer_service_on_functional_id"


@pytest.mark.parametrize("functional", [True, False])
@pytest.mark.parametrize("service", sorted(SPEC_NEVER))
def test_never_list_is_refused_everywhere(service, functional):
    with pytest.raises(SafetyViolation) as refused:
        policy.check_service(service, functional=functional)
    assert refused.value.reason == "service_never_allowed"


@pytest.mark.parametrize("functional", [True, False])
@pytest.mark.parametrize("service", EVERYTHING_ELSE)
def test_everything_else_is_denied_by_default(service, functional):
    with pytest.raises(SafetyViolation) as refused:
        policy.check_service(service, functional=functional)
    assert refused.value.reason == "service_not_allowlisted"


@pytest.mark.parametrize("service", sorted(SPEC_DTC_READS))
def test_sensitive_ecus_get_dtc_reads(service):
    policy.check_sensitive(service, sensitive=True)


@pytest.mark.parametrize("service", sorted((SPEC_OBD | SPEC_MANUFACTURER) - SPEC_DTC_READS))
def test_sensitive_ecus_get_nothing_else(service):
    with pytest.raises(SafetyViolation) as refused:
        policy.check_sensitive(service, sensitive=True)
    assert refused.value.reason == "sensitive_ecu_dtc_reads_only"
    policy.check_sensitive(service, sensitive=False)


def test_violation_message_without_detail():
    assert str(SafetyViolation("just_a_reason")) == "just_a_reason"
