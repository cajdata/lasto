"""Frozen test fixture, not the app: what sitegen reads from src/lasto/safety/ecus.py, copied from
the app at dab6e21 (Phase 2 as approved), and nothing else. See site/tests/fixtures/README.md.
"""

ENGINE = Ecu(
    name="engine",
    kind=EcuKind.ENGINE,
    request_id=0x7E0,
    response_id=0x7E8,
    ext_address=None,
    evidence=(
        "Project spec: the engine ECU answers on 11-bit CAN at 0x7E0 (ISO 15765-4). "
        "2005 GX470 manual: the ECM uses ISO 15765-4."
    ),
)
APPROVED_ECUS: tuple[Ecu, ...] = (ENGINE,)
