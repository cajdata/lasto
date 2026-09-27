"""Transmit policy for diagnostic requests (rules 2, 3, and 10). Constants and pure checks.

The never-list is checked first and nothing overrides it. Everything not on
the allowlist is denied. Manufacturer services go only to a specific approved
ECU, never to the functional ID, so a single request can't reach every ECU.
"""

from __future__ import annotations

from lasto.safety.audit import refuse
from lasto.safety.errors import SafetyViolation

FUNCTIONAL_REQUEST_ID = 0x7DF

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
