"""Frozen test fixture, not the app: what sitegen reads from src/lasto/safety/policy.py, copied from
the app at dab6e21 (Phase 2 as approved), and nothing else. The site's tests read this copy, so their
expected values don't move when the app does; the strict build checks the live pages against the
live source. See site/tests/fixtures/README.md.
"""

FUNCTIONAL_REQUEST_ID = 0x7DF
PADDING_BYTE = 0x00
OBD_SERVICES = frozenset({0x01, 0x02, 0x03, 0x06, 0x07, 0x09, 0x0A})
MANUFACTURER_READ_SERVICES = frozenset({0x13, 0x17, 0x18, 0x19, 0x1A, 0x21, 0x22})
ALLOWED_SERVICES = OBD_SERVICES | MANUFACTURER_READ_SERVICES
DTC_READ_SERVICES = frozenset({0x03, 0x07, 0x0A, 0x13, 0x17, 0x18, 0x19})
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
PROBE_PIDS = frozenset({0x0C, 0x0D, 0x42})
