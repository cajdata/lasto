"""A simulated GX470: vehicle state, broadcast traffic, and the diagnostic ECUs.

FICTIONAL: the broadcast IDs and byte layouts are placeholders modeled on
other Toyotas (opendbc, forum reverse engineering) and the J120 service
manuals. Real captures will replace them. The steering angle is stuck at its
invalid maximum, the way the truck's VSC ECU reports 1150.875 degrees.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lasto.sim.bus import Broadcaster, SimBus
from lasto.sim.clock import FakeClock
from lasto.sim.ecu import MockEcu
from lasto.sim.fake_pcan import FakePcanDll

# Fictional broadcast IDs (hypotheses to confirm with a capture).
STEERING_ANGLE_ID = 0x025
YAW_RATE_ID = 0x024
VEHICLE_SPEED_ID = 0x0B4
ENGINE_RPM_ID = 0x2C4


@dataclass
class VehicleState:
    speed_kph: float = 0.0
    rpm: float = 0.0
    coolant_c: float = 85.0
    voltage: float = 12.6
    atf_c: float = 70.0
    steering_raw: int = 0x7FF  # all ones: the invalid maximum


def _u16(value: float) -> bytes:
    return max(0, min(0xFFFF, int(value))).to_bytes(2, "big")


class SimVehicle:
    def __init__(self, bus: SimBus, state: VehicleState | None = None) -> None:
        self.state = VehicleState() if state is None else state
        bus.add_broadcaster(Broadcaster(STEERING_ANGLE_ID, 0.010, self._steering))
        bus.add_broadcaster(Broadcaster(YAW_RATE_ID, 0.010, lambda _t: bytes(8)), offset=0.002)
        bus.add_broadcaster(Broadcaster(VEHICLE_SPEED_ID, 0.020, self._speed), offset=0.004)
        bus.add_broadcaster(Broadcaster(ENGINE_RPM_ID, 0.020, self._rpm), offset=0.006)
        state = self.state
        self.engine = MockEcu(
            "engine",
            request_id=0x7E0,
            response_id=0x7E8,
            pids={
                0x00: b"\x08\x18\x00\x01",  # supports 05, 0C, 0D, and the next range
                0x20: b"\x00\x00\x00\x01",
                0x40: b"\x40\x00\x00\x00",  # supports 42
                0x05: lambda: bytes([int(state.coolant_c) + 40]),
                0x0C: lambda: _u16(state.rpm * 4),
                0x0D: lambda: bytes([int(state.speed_kph)]),
                0x42: lambda: _u16(state.voltage * 1000),
            },
            local_ids={0xD9: lambda: bytes(4) + _u16((state.atf_c + 40) * 256) + bytes(2)},
            dtcs=[0x0420],
        )
        self.transmission = MockEcu(
            "transmission",
            request_id=0x7E1,
            response_id=0x7E9,
            pids={0x00: b"\x00\x08\x00\x00", 0x0D: lambda: bytes([int(state.speed_kph)])},
        )
        bus.add_node(self.engine)
        bus.add_node(self.transmission)

    def _steering(self, _time: float) -> bytes:
        raw = self.state.steering_raw & 0x7FF
        return bytes([raw >> 8, raw & 0xFF]) + bytes(6)

    def _speed(self, _time: float) -> bytes:
        return bytes(5) + _u16(self.state.speed_kph * 100) + bytes(1)

    def _rpm(self, _time: float) -> bytes:
        return _u16(self.state.rpm) + bytes(6)


@dataclass
class Sim:
    clock: FakeClock
    bus: SimBus
    vehicle: SimVehicle
    dll: FakePcanDll
    extras: dict[str, object] = field(default_factory=dict)


def build_sim(clock: FakeClock | None = None) -> Sim:
    clock = FakeClock() if clock is None else clock
    bus = SimBus(clock)
    vehicle = SimVehicle(bus)
    return Sim(clock, bus, vehicle, FakePcanDll(bus))
