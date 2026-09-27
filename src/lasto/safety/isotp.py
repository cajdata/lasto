"""The ISO-TP (ISO 15765-2) framing lasto needs, and an independent parser.

lasto only ever sends single frames (requests) and flow control frames
(answering an ECU's first frame). It never sends first or consecutive frames.
The parser is what the gate uses to re-check the exact bytes before they are
written, and to read responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from lasto.safety.errors import SafetyViolation

FRAME_BYTES = 8


class FrameKind(Enum):
    SINGLE = "single"
    FIRST = "first"
    CONSECUTIVE = "consecutive"
    FLOW_CONTROL = "flow_control"


@dataclass(frozen=True, slots=True)
class IsoTpFrame:
    kind: FrameKind
    payload: bytes = b""
    length: int = 0
    sequence: int = 0
    flow_status: int = 0
    block_size: int = 0
    st_min: int = 0


def _prefix(ext_address: int | None) -> bytes:
    return b"" if ext_address is None else bytes([ext_address])


def _pad(raw: bytes, padding: int) -> bytes:
    return raw + bytes([padding]) * (FRAME_BYTES - len(raw))


def encode_single_frame(payload: bytes, *, ext_address: int | None, padding: int) -> bytes:
    capacity = FRAME_BYTES - 1 - len(_prefix(ext_address))
    if not 1 <= len(payload) <= capacity:
        raise SafetyViolation("not_single_frame", f"a {len(payload)}-byte request doesn't fit in one frame")
    return _pad(_prefix(ext_address) + bytes([len(payload)]) + bytes(payload), padding)


def encode_flow_control(*, ext_address: int | None, padding: int) -> bytes:
    """Clear to send, no block limit, no minimum separation."""
    return _pad(_prefix(ext_address) + b"\x30\x00\x00", padding)


def parse(data: bytes, *, ext_address: int | None) -> IsoTpFrame:
    offset = 0
    if ext_address is not None:
        if not data or data[0] != ext_address:
            raise SafetyViolation("isotp_address_mismatch", data.hex(" "))
        offset = 1
    body = data[offset:]
    if not body:
        raise SafetyViolation("isotp_malformed", "no protocol byte")
    frame_type, low = body[0] >> 4, body[0] & 0x0F
    if frame_type == 0:
        if not 1 <= low <= len(body) - 1:
            raise SafetyViolation("isotp_malformed", f"single frame length {low}")
        return IsoTpFrame(FrameKind.SINGLE, payload=bytes(body[1 : 1 + low]))
    if frame_type == 1:
        if len(body) < 2:
            raise SafetyViolation("isotp_malformed", "short first frame")
        length = (low << 8) | body[1]
        if length < FRAME_BYTES - offset:
            raise SafetyViolation("isotp_malformed", f"first frame length {length} fits in a single frame")
        return IsoTpFrame(FrameKind.FIRST, payload=bytes(body[2:]), length=length)
    if frame_type == 2:
        return IsoTpFrame(FrameKind.CONSECUTIVE, payload=bytes(body[1:]), sequence=low)
    if frame_type == 3:
        if len(body) < 3:
            raise SafetyViolation("isotp_malformed", "short flow control")
        return IsoTpFrame(FrameKind.FLOW_CONTROL, flow_status=low, block_size=body[1], st_min=body[2])
    raise SafetyViolation("isotp_malformed", f"unknown frame type {frame_type}")
