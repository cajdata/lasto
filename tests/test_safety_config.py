"""The committed snapshot of the safety configuration matches the safety core exactly (§14.4).

Anything that shows the configuration (each run's record, and later the GUI's settings page) reads
src/lasto/data/safety_config.json through lasto.services.safety_config, without importing the safety
core. This test builds the expected content from the core's own constants. If an approved safety
core change moves one, it fails and prints the new file to commit with that change.
"""

from __future__ import annotations

import json

from lasto.safety import ecus, interlocks, killswitch, policy, ratelimit, session, stn_policy
from lasto.services import safety_config


def _hex(values: object, width: int = 2) -> list[str]:
    return [f"0x{value:0{width}X}" for value in sorted(values)]  # type: ignore[call-overload]


def expected() -> dict[str, object]:
    return {
        "about": (
            "The safety core's configuration in this build, for reading only. Changing any of it is a safety core "
            "change, and tests/test_safety_config.py proves this file matches the code."
        ),
        "can": {
            "functional_request_id": f"0x{policy.FUNCTIONAL_REQUEST_ID:03X}",
            "approved_ecus": [
                {
                    "name": ecu.name,
                    "kind": ecu.kind.value,
                    "request_id": f"0x{ecu.request_id:03X}",
                    "response_id": f"0x{ecu.response_id:03X}",
                    "ext_address": None if ecu.ext_address is None else f"0x{ecu.ext_address:02X}",
                    "evidence": ecu.evidence,
                }
                for ecu in ecus.APPROVED_ECUS
            ],
            "obd_services": _hex(policy.OBD_SERVICES),
            "manufacturer_read_services": _hex(policy.MANUFACTURER_READ_SERVICES),
            "never_allowed_services": _hex(policy.NEVER_SERVICES),
            "sensitive_ecu_kinds": sorted(kind.value for kind in ecus.SENSITIVE_KINDS),
            "sensitive_ecu_services": _hex(policy.DTC_READ_SERVICES),
            "interlock_probe_pids": _hex(policy.PROBE_PIDS),
            "padding_byte": f"0x{policy.PADDING_BYTE:02X}",
            "max_request_payload_bytes": policy.MAX_REQUEST_PAYLOAD,
        },
        "rates": {
            "hard_ceiling_requests_per_second": ratelimit.HARD_CEILING_PER_SECOND,
            "requests_per_second_by_purpose": {purpose.value: rate for purpose, rate in sorted(
                ratelimit.PURPOSE_RATES.items(), key=lambda item: item[0].value
            )},  # fmt: skip
            "busy_backoff_start_seconds": ratelimit.BACKOFF_START,
            "busy_backoff_max_seconds": ratelimit.BACKOFF_MAX,
        },
        "kill_switch": {
            "consecutive_negative_responses": killswitch.CONSECUTIVE_NRC_LIMIT,
            "negative_responses_in_window": killswitch.WINDOW_NRC_LIMIT,
            "negative_response_window_seconds": killswitch.NRC_WINDOW_SECONDS,
            "consecutive_timeouts": killswitch.CONSECUTIVE_TIMEOUT_LIMIT,
            "discovery_not_supported_codes": _hex(killswitch.DISCOVERY_NOT_SUPPORTED),
            "discovery_not_supported_limit": killswitch.DISCOVERY_NRC_LIMIT,
            "discovery_not_supported_window_seconds": killswitch.DISCOVERY_WINDOW_SECONDS,
        },
        "interlocks": {
            "speed_max_age_seconds": dict(sorted(interlocks.SPEED_MAX_AGE.items())),
            "rpm_max_age_seconds": interlocks.RPM_MAX_AGE,
            "voltage_max_age_seconds": interlocks.VOLTAGE_MAX_AGE,
            "min_engine_off_volts": interlocks.MIN_ENGINE_OFF_VOLTAGE,
            "parked_only_purposes": sorted(purpose.value for purpose in interlocks.PARKED_PURPOSES),
        },
        "polled_session": {"listen_window_seconds": session.LISTEN_WINDOW},
        "stn": {
            "exact_commands": sorted(stn_policy.EXACT_COMMANDS),
            "pattern_commands": [pattern.pattern for pattern in stn_policy.PATTERN_COMMANDS],
            "reset_commands": sorted(stn_policy.RESET_COMMANDS),
            "monitor_commands": sorted(stn_policy.MONITOR_COMMANDS),
            "can_monitor_protocols": sorted(stn_policy.CAN_MONITOR_PROTOCOLS),
            "kline_monitor_protocols": sorted(stn_policy.KLINE_MONITOR_PROTOCOLS),
            "silent_monitor_parameter": f"0x{stn_policy.SILENT_MONITOR_PP:02X}",
        },
    }


def render(config: dict[str, object]) -> str:
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def test_the_snapshot_matches_the_safety_core():
    assert safety_config.snapshot() == expected(), "commit this as src/lasto/data/safety_config.json:\n" + render(expected())


def test_the_snapshot_text_is_what_a_run_records():
    """Each run stores the text, byte for byte, so the record shows exactly what was in force."""
    text = safety_config.snapshot_text()
    assert text == render(expected()) and json.loads(text) == safety_config.snapshot()
