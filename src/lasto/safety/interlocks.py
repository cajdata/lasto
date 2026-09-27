"""Motion interlock, battery guard, and sensitive-ECU rules (rules 8, 9, and 10).

Unknown or stale readings count against the request: unknown speed means
moving, and unknown voltage with the engine off means the battery is low.
Every refusal is audited (rule 11).
"""

from __future__ import annotations

from dataclasses import dataclass

from lasto.safety import policy
from lasto.safety.audit import refuse
from lasto.safety.ecus import SENSITIVE_KINDS, EcuKind
from lasto.safety.errors import SafetyViolation
from lasto.safety.requests import Purpose, Request

# How old a reading may be and still count, by where it came from.
SPEED_MAX_AGE = {"broadcast": 0.5, "poll": 1.0}
RPM_MAX_AGE = 2.0
VOLTAGE_MAX_AGE = 2.0
MIN_ENGINE_OFF_VOLTAGE = 12.0

PARKED_PURPOSES = frozenset({Purpose.SNAPSHOT, Purpose.IDENTIFY, Purpose.DISCOVERY})


@dataclass(frozen=True, slots=True)
class Sample:
    value: float
    time: float
    source: str


def is_probe(request: Request) -> bool:
    """A single-PID Mode 01 read of speed, RPM, or voltage, to the engine ECU or the functional ID."""
    payload = request.payload
    return (
        len(payload) == 2
        and payload[0] == 0x01
        and payload[1] in policy.PROBE_PIDS
        and (request.target is None or request.target.kind is EcuKind.ENGINE)
    )


class Interlocks:
    def __init__(self) -> None:
        self._speed: Sample | None = None
        self._rpm: Sample | None = None
        self._voltage: Sample | None = None

    def update_speed(self, kph: float, now: float, *, source: str) -> None:
        if source not in SPEED_MAX_AGE:
            refuse(ValueError(f"unknown speed source {source!r}"), transport="interlock", reason="unknown_speed_source")
        self._speed = Sample(float(kph), now, source)

    def update_rpm(self, rpm: float, now: float) -> None:
        self._rpm = Sample(float(rpm), now, "poll")

    def update_voltage(self, volts: float, now: float) -> None:
        self._voltage = Sample(float(volts), now, "poll")

    def stationary(self, now: float) -> bool:
        speed = self._speed
        return speed is not None and now - speed.time <= SPEED_MAX_AGE[speed.source] and speed.value == 0

    def engine_off(self, now: float) -> bool:
        rpm = self._rpm
        return rpm is None or now - rpm.time > RPM_MAX_AGE or rpm.value == 0

    def battery_ok(self, now: float) -> bool:
        volts = self._voltage
        return volts is not None and now - volts.time <= VOLTAGE_MAX_AGE and volts.value >= MIN_ENGINE_OFF_VOLTAGE

    def check(self, request: Request, now: float, profile: frozenset[tuple[str | None, bytes]]) -> None:
        purpose = request.purpose
        target = request.target
        text = request.describe()

        def deny(reason: str, detail: str) -> None:
            refuse(SafetyViolation(reason, detail), transport="pcan", request=text)

        if target is not None and target.kind in SENSITIVE_KINDS:
            if purpose is Purpose.DISCOVERY:
                deny("sensitive_ecu_excluded_from_discovery", target.name)
            policy.check_sensitive(request.service, sensitive=True, request=text)
        if purpose is Purpose.LOGGING:
            if request.key not in profile:
                deny("not_in_logging_profile", request.payload.hex(" "))
        elif purpose is Purpose.INTERLOCK_PROBE:
            if not is_probe(request):
                deny("not_an_interlock_probe", request.payload.hex(" "))
        else:
            if not self.stationary(now):
                deny("vehicle_not_confirmed_stationary", purpose.value)
            if self.engine_off(now) and not self.battery_ok(now):
                deny("battery_low_or_unknown", purpose.value)
