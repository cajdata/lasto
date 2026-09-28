"""Typed request builders: the only way the rest of lasto asks for data (rule 5).

Builders take typed values (PID numbers, local IDs, an approved ECU entry),
never raw bytes or CAN IDs. A Request can only be made here, and it re-checks
its own service on creation. The gate re-checks the encoded bytes again
before anything is transmitted. Every refusal here is audited (rule 11).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from lasto.safety import policy
from lasto.safety._frozen import SealedEnum, SealedType, freeze
from lasto.safety.audit import refuse
from lasto.safety.ecus import Ecu

_BUILDER = object()

MAX_PIDS_PER_REQUEST = 6
MAX_DTC_INFORMATION_PARAMETERS = 5


class Purpose(SealedEnum):
    LOGGING = "logging"
    SNAPSHOT = "snapshot"
    IDENTIFY = "identify"
    DISCOVERY = "discovery"
    INTERLOCK_PROBE = "interlock_probe"


class DtcKind(SealedEnum):
    STORED = 0x03
    PENDING = 0x07
    PERMANENT = 0x0A


def describe(target: object, payload: object, purpose: object) -> str:
    """Audit-log text for a request, even a malformed one."""
    name = "functional" if target is None else getattr(target, "name", repr(target))
    data = payload.hex(" ").upper() if isinstance(payload, bytes | bytearray) else repr(payload)
    return f"{name} {data} ({getattr(purpose, 'value', repr(purpose))})"


@dataclass(frozen=True, slots=True)
class Request(metaclass=SealedType):
    """A read request for one ECU (target) or for every OBD ECU (target None, the functional ID)."""

    target: Ecu | None
    payload: bytes
    purpose: Purpose
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        text = self.describe()
        if self._token is not _BUILDER:
            refuse(
                TypeError("build requests with the functions in lasto.safety.requests"),
                transport="request",
                request=text,
                reason="request_not_from_a_builder",
            )
        if self.target is not None and not isinstance(self.target, Ecu):
            refuse(TypeError("target must be an ECU entry or None"), transport="request", request=text, reason="bad_target")
        if not isinstance(self.purpose, Purpose):
            refuse(TypeError("purpose must be a Purpose"), transport="request", request=text, reason="bad_purpose")
        if not isinstance(self.payload, bytes) or not 1 <= len(self.payload) <= policy.MAX_REQUEST_PAYLOAD:
            refuse(
                ValueError(f"a request payload is 1 to {policy.MAX_REQUEST_PAYLOAD} bytes"),
                transport="request",
                request=text,
                reason="bad_payload",
            )
        policy.check_service(self.payload[0], functional=self.target is None, request=text)

    @property
    def service(self) -> int:
        return self.payload[0]

    @property
    def key(self) -> tuple[str | None, bytes]:
        return (None if self.target is None else self.target.name, self.payload)

    def describe(self) -> str:
        return describe(self.target, self.payload, self.purpose)


def _refuse_argument(error: Exception, reason: str, request: str) -> None:
    refuse(error, transport="request", request=request, reason=reason)


def _byte(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFF:
        _refuse_argument(ValueError(f"{name} must be an integer 0-255, got {value!r}"), "bad_argument", f"{name}={value!r}")
    return value  # type: ignore[return-value]


def _word(name: str, value: object) -> list[int]:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFF:
        _refuse_argument(ValueError(f"{name} must be an integer 0-65535, got {value!r}"), "bad_argument", f"{name}={value!r}")
    return [value >> 8, value & 0xFF]  # type: ignore[operator]


def _ecu(ecu: object) -> Ecu:
    if not isinstance(ecu, Ecu):
        _refuse_argument(TypeError("pass an approved ECU entry from lasto.safety.ecus"), "not_an_ecu_entry", repr(ecu))
    return ecu  # type: ignore[return-value]


def _optional_ecu(ecu: object) -> Ecu | None:
    return None if ecu is None else _ecu(ecu)


def _build(target: Ecu | None, payload: list[int], purpose: object) -> Request:
    if not isinstance(purpose, Purpose):
        _refuse_argument(TypeError("purpose must be a Purpose"), "bad_purpose", describe(target, bytes(payload), purpose))
    return Request(target, bytes(payload), purpose, _BUILDER)  # type: ignore[arg-type]


def read_pid(pids: Iterable[int], *, purpose: Purpose, ecu: Ecu | None = None) -> Request:
    """OBD Mode 01: current data, up to six PIDs in one request."""
    try:
        items = list(pids)
    except TypeError:
        _refuse_argument(TypeError(f"pids must be a list of PID numbers, got {pids!r}"), "bad_argument", repr(pids))
    pid_list = [_byte("pid", pid) for pid in items]
    if not 1 <= len(pid_list) <= MAX_PIDS_PER_REQUEST:
        _refuse_argument(ValueError(f"ask for 1 to {MAX_PIDS_PER_REQUEST} PIDs per request"), "pid_count", repr(pid_list))
    return _build(_optional_ecu(ecu), [0x01, *pid_list], purpose)


def read_freeze_frame(pid: int, *, purpose: Purpose, frame: int = 0, ecu: Ecu | None = None) -> Request:
    """OBD Mode 02: freeze frame data."""
    return _build(_optional_ecu(ecu), [0x02, _byte("pid", pid), _byte("frame", frame)], purpose)


def read_dtcs(kind: DtcKind, *, purpose: Purpose, ecu: Ecu | None = None) -> Request:
    """OBD Mode 03 (stored), 07 (pending), or 0A (permanent) DTCs."""
    if not isinstance(kind, DtcKind):
        _refuse_argument(TypeError("kind must be a DtcKind"), "bad_dtc_kind", repr(kind))
    return _build(_optional_ecu(ecu), [kind.value], purpose)


def read_mode06(test_id: int, *, purpose: Purpose, ecu: Ecu | None = None) -> Request:
    """OBD Mode 06: on-board monitoring test results."""
    return _build(_optional_ecu(ecu), [0x06, _byte("test_id", test_id)], purpose)


def read_vehicle_info(info_type: int, *, purpose: Purpose, ecu: Ecu | None = None) -> Request:
    """OBD Mode 09: vehicle information (VIN, calibration IDs, CVNs)."""
    return _build(_optional_ecu(ecu), [0x09, _byte("info_type", info_type)], purpose)


def read_local_id(ecu: Ecu, local_id: int, *, purpose: Purpose) -> Request:
    """KWP2000 0x21: read data by local identifier."""
    return _build(_ecu(ecu), [0x21, _byte("local_id", local_id)], purpose)


def read_did(ecu: Ecu, did: int, *, purpose: Purpose) -> Request:
    """0x22: read data by (common) identifier."""
    return _build(_ecu(ecu), [0x22, *_word("did", did)], purpose)


def read_ecu_id(ecu: Ecu, option: int, *, purpose: Purpose) -> Request:
    """KWP2000 0x1A: read ECU identification."""
    return _build(_ecu(ecu), [0x1A, _byte("option", option)], purpose)


def read_dtcs_kwp(ecu: Ecu, *, purpose: Purpose, group: int = 0xFF00) -> Request:
    """KWP2000 0x13: read diagnostic trouble codes."""
    return _build(_ecu(ecu), [0x13, *_word("group", group)], purpose)


def read_dtc_status(ecu: Ecu, dtc: int, *, purpose: Purpose) -> Request:
    """KWP2000 0x17: read status of a DTC."""
    return _build(_ecu(ecu), [0x17, *_word("dtc", dtc)], purpose)


def read_dtcs_by_status(ecu: Ecu, *, purpose: Purpose, status: int = 0x00, group: int = 0xFF00) -> Request:
    """KWP2000 0x18: read DTCs by status."""
    return _build(_ecu(ecu), [0x18, _byte("status", status), *_word("group", group)], purpose)


def read_dtc_information(ecu: Ecu, subfunction: int, *parameters: int, purpose: Purpose) -> Request:
    """UDS 0x19: read DTC information."""
    if len(parameters) > MAX_DTC_INFORMATION_PARAMETERS:
        _refuse_argument(
            ValueError(f"at most {MAX_DTC_INFORMATION_PARAMETERS} parameter bytes"), "too_many_parameters", repr(parameters)
        )
    values = [_byte("parameter", value) for value in parameters]
    return _build(_ecu(ecu), [0x19, _byte("subfunction", subfunction), *values], purpose)


def interlock_probe(pid: int, *, ecu: Ecu | None = None) -> Request:
    """A Mode 01 read of speed, RPM, or module voltage for the motion interlock and battery guard."""
    pid = _byte("pid", pid)  # 13.0 equals 0x0D, but isn't a PID number
    if pid not in policy.PROBE_PIDS:
        _refuse_argument(ValueError("interlock probes read PID 0x0C, 0x0D, or 0x42"), "not_a_probe_pid", repr(pid))
    return _build(_optional_ecu(ecu), [0x01, pid], Purpose.INTERLOCK_PROBE)


freeze(__name__)
