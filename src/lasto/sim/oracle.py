"""The simulator's own judgment of every frame lasto writes.

Written separately from lasto.safety.policy on purpose: if the safety core's
allowlists had a mistake, it shouldn't be able to hide itself here too. So it
is exactly as strict as the real policy: when an ECU is approved (a safety
core commit), its request ID is added here by hand as well.
"""

from __future__ import annotations

from collections.abc import Collection

FUNCTIONAL_ID = 0x7DF
ENGINE_REQUEST_ID = 0x7E0
# The functional ID, plus the physical request ID of each approved ECU (only the engine so far).
ALLOWED_REQUEST_IDS = frozenset({FUNCTIONAL_ID, ENGINE_REQUEST_ID})
# Read-only services from the project spec: OBD 01, 02, 03, 06, 07, 09, 0A and manufacturer reads.
READ_ONLY_SERVICES = frozenset({0x01, 0x02, 0x03, 0x06, 0x07, 0x09, 0x0A, 0x13, 0x17, 0x18, 0x19, 0x1A, 0x21, 0x22})
OBD_ONLY = frozenset({0x01, 0x02, 0x03, 0x06, 0x07, 0x09, 0x0A})


def frame_violations(
    can_id: int,
    data: bytes,
    *,
    listen_only: bool,
    broadcast_ids: Collection[int],
    awaiting_flow_control: Collection[int],
) -> list[str]:
    frame = f"{can_id:03X}#{data.hex().upper()}"
    if listen_only:
        return [f"frame written while the channel is listen-only: {frame}"]
    found: list[str] = []
    if can_id not in ALLOWED_REQUEST_IDS:
        found.append(f"frame on an ID lasto may not transmit on (only 0x7DF and 0x7E0): {frame}")
    if can_id in broadcast_ids:
        found.append(f"frame on an ID that carries broadcast traffic: {frame}")
    if len(data) != 8:
        found.append(f"frame is not 8 bytes: {frame}")
    frame_type = data[0] >> 4 if data else -1
    if frame_type == 0:
        length = data[0] & 0x0F
        if not 1 <= length <= min(7, len(data) - 1):
            found.append(f"malformed single frame: {frame}")
        elif data[1] not in READ_ONLY_SERVICES:
            found.append(f"denied service 0x{data[1]:02X}: {frame}")
        elif can_id == FUNCTIONAL_ID and data[1] not in OBD_ONLY:
            found.append(f"manufacturer service on the functional ID: {frame}")
    elif frame_type == 3:
        if can_id not in awaiting_flow_control:
            found.append(f"flow control no ECU asked for: {frame}")
    else:
        found.append(f"lasto may only send single frames and flow control: {frame}")
    return found
