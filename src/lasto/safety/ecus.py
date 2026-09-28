"""Approved diagnostic targets: the physical request IDs lasto may transmit on (rule 2).

Adding an entry is a safety core change. It needs the owner's approval, the
evidence (a Creader capture or an approved discovery result), and its own
commit. An entry must say what kind of ECU it is; SRS and immobilizer ECUs
get DTC reads only (rule 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from lasto.safety._frozen import SealedEnum, SealedType, freeze


class EcuKind(SealedEnum):
    ENGINE = "engine"
    TRANSMISSION = "transmission"
    ABS_VSC = "abs_vsc"
    KDSS = "kdss"
    SUSPENSION = "suspension"
    TPMS = "tpms"
    SRS = "srs"
    IMMOBILIZER = "immobilizer"
    BODY = "body"


SENSITIVE_KINDS = frozenset({EcuKind.SRS, EcuKind.IMMOBILIZER})


@dataclass(frozen=True, slots=True)
class Ecu(metaclass=SealedType):
    name: str
    kind: EcuKind
    request_id: int
    response_id: int
    ext_address: int | None
    evidence: str


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

# ISO 15765-4 response IDs for 11-bit functional (0x7DF) requests.
FUNCTIONAL_RESPONSE_IDS = frozenset(range(0x7E8, 0x7F0))

_BY_REQUEST_ID = MappingProxyType({ecu.request_id: ecu for ecu in APPROVED_ECUS})
_BY_RESPONSE_ID = MappingProxyType({ecu.response_id: ecu for ecu in APPROVED_ECUS})


def is_approved(ecu: object) -> bool:
    """Only the table's own entries, by identity: a copy, or a look-alike whose fields merely compare equal
    (an int subclass as its ID, say), is not approved. The gate then takes every ID from the entry itself."""
    return any(ecu is approved for approved in APPROVED_ECUS)


def by_request_id(can_id: int) -> Ecu | None:
    return _BY_REQUEST_ID.get(can_id)


def by_response_id(can_id: int) -> Ecu | None:
    return _BY_RESPONSE_ID.get(can_id)


freeze(__name__)
