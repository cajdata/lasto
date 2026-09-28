"""Transmit policy for diagnostic requests (rules 2, 3, and 10). Constants and pure checks.

The never-list is checked first and nothing overrides it. Everything not on
the allowlist is denied. Manufacturer services go only to a specific approved
ECU, never to the functional ID, so a single request can't reach every ECU.

check_frame is the whole stateless check on one frame bound for the bus. The
gate runs it, and the write function runs it again on the exact bytes it is
about to write, so neither depends on the other having done it.
"""

from __future__ import annotations

from collections.abc import Collection

from lasto.safety import ecus, isotp
from lasto.safety.audit import refuse
from lasto.safety.errors import SafetyViolation

FUNCTIONAL_REQUEST_ID = 0x7DF
MAX_STANDARD_ID = 0x7FF

# Filler for unused bytes in the 8-byte frames lasto sends. Confirm against Creader captures.
PADDING_BYTE = 0x00

# Requests are always one ISO-TP single frame (normal addressing).
MAX_REQUEST_PAYLOAD = 7

OBD_SERVICES = frozenset({0x01, 0x02, 0x03, 0x06, 0x07, 0x09, 0x0A})
MANUFACTURER_READ_SERVICES = frozenset({0x13, 0x17, 0x18, 0x19, 0x1A, 0x21, 0x22})
ALLOWED_SERVICES = OBD_SERVICES | MANUFACTURER_READ_SERVICES

# Services allowed for SRS and immobilizer ECUs.
DTC_READ_SERVICES = frozenset({0x03, 0x07, 0x0A, 0x13, 0x17, 0x18, 0x19})

# Never allowed, even behind a flag added later.
NEVER_SERVICES = frozenset(
    {
        0x04,  # clear DTCs (OBD)
        0x08,  # control on-board systems
        0x10,  # session control
        0x11,  # ECU reset
        0x14,  # clear DTCs
        0x23,  # read memory by address
        0x27,  # security access
        0x28,  # communication control
        0x2C,  # dynamically define identifier
        0x2E,  # write data by identifier
        0x2F,  # I/O control
        0x30,  # I/O control by local identifier
        0x31,  # routine control
        0x34,  # request download
        0x35,  # request upload
        0x36,  # transfer data
        0x37,  # transfer exit
        0x38,  # request file transfer
        0x3B,  # write data by local identifier
        0x3D,  # write memory by address
        0x3E,  # tester present
        0x85,  # control DTC setting
    }
)

# Mode 01 PIDs the motion interlock and battery guard read: RPM, vehicle speed, module voltage.
PROBE_PIDS = frozenset({0x0C, 0x0D, 0x42})


def check_service(service: int, *, functional: bool, request: str = "") -> None:
    """Refuse (audited) any service that isn't allowed, and manufacturer services on the functional ID."""
    if service in NEVER_SERVICES:
        refuse(SafetyViolation("service_never_allowed", f"0x{service:02X}"), transport="pcan", request=request)
    if service not in ALLOWED_SERVICES:
        refuse(SafetyViolation("service_not_allowlisted", f"0x{service:02X}"), transport="pcan", request=request)
    if functional and service not in OBD_SERVICES:
        refuse(
            SafetyViolation("manufacturer_service_on_functional_id", f"0x{service:02X}"),
            transport="pcan",
            request=request,
        )


def check_sensitive(service: int, *, sensitive: bool, request: str = "") -> None:
    """Refuse (audited) anything but a DTC read for an SRS or immobilizer ECU."""
    if sensitive and service not in DTC_READ_SERVICES:
        refuse(SafetyViolation("sensitive_ecu_dtc_reads_only", f"0x{service:02X}"), transport="pcan", request=request)


def request_ids() -> frozenset[int]:
    """Every ID lasto may transmit on: the functional ID and the approved ECUs' request IDs."""
    return frozenset({FUNCTIONAL_REQUEST_ID, *(ecu.request_id for ecu in ecus.APPROVED_ECUS)})


def hex_id(can_id: object) -> str:
    return f"0x{can_id:03X}" if type(can_id) is int else repr(can_id)


def frame_text(can_id: object, data: object) -> str:
    shown = bytes(data).hex(" ").upper() if isinstance(data, bytes | bytearray) else repr(data)
    return f"{hex_id(can_id)} {shown}"


def check_frame(can_id: object, data: object, *, broadcast_ids: Collection[int] = ()) -> str:
    """Refuse (audited) any frame lasto may never send, whatever state it is in.

    Returns the frame's kind: "request" (an allowlisted single frame) or
    "flow_control" (the canonical clear-to-send, on an approved ECU's request
    ID). Whether a flow control frame answers a first frame right now is the
    gate's check; this one needs no state.
    """
    text = frame_text(can_id, data)
    if type(data) is not bytes or len(data) != isotp.FRAME_BYTES:
        refuse(
            SafetyViolation("frame_length", f"lasto only sends {isotp.FRAME_BYTES}-byte frames as immutable bytes"),
            transport="pcan",
            request=text,
        )
    if type(can_id) is not int or not 0 <= can_id <= MAX_STANDARD_ID or can_id not in request_ids():
        refuse(SafetyViolation("can_id_not_allowlisted", hex_id(can_id)), transport="pcan", request=text)
    if can_id in broadcast_ids:
        refuse(SafetyViolation("can_id_carries_broadcast", hex_id(can_id)), transport="pcan", request=text)
    ecu = ecus.by_request_id(can_id)
    ext_address = None if ecu is None else ecu.ext_address
    try:
        frame = isotp.parse(data, ext_address=ext_address)
    except isotp.IsoTpError as exc:
        refuse(SafetyViolation(exc.reason, exc.detail), transport="pcan", request=text)
    if frame.kind is isotp.FrameKind.FLOW_CONTROL:
        if ecu is None:
            refuse(SafetyViolation("flow_control_wrong_id", hex_id(can_id)), transport="pcan", request=text)
        if data != isotp.encode_flow_control(ext_address=ext_address, padding=PADDING_BYTE):
            refuse(SafetyViolation("flow_control_malformed", data.hex(" ")), transport="pcan", request=text)
        return "flow_control"
    if frame.kind is not isotp.FrameKind.SINGLE:
        refuse(SafetyViolation("not_single_frame", frame.kind.value), transport="pcan", request=text)
    service = frame.payload[0]
    check_service(service, functional=ecu is None, request=text)
    check_sensitive(service, sensitive=ecu is not None and ecu.kind in ecus.SENSITIVE_KINDS, request=text)
    return "request"
