"""Approved diagnostic targets: the physical request IDs lasto may transmit on (rule 2).

Adding an entry is a safety core change. It needs the owner's approval, the
evidence (a Creader capture or an approved discovery result), and its own
commit. An entry must say what kind of ECU it is; SRS and immobilizer ECUs
get DTC reads only (rule 10).

Each entry's IDs, extended address, and kind are copied at import into a
Route, a tuple. The policy and the gate read only the routes (finding P1).
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


class Route(tuple, metaclass=SealedType):  # type: ignore[type-arg]
    """How lasto reaches one approved ECU, and what kind it is: (request ID, response ID, extended address, kind).

    Copied from the entry once, at import, into a tuple, whose items nothing in Python can change in place.
    The policy and the gate read only routes, never an Ecu's fields: a frozen dataclass can still be
    rewritten in place, and a rewritten entry must not change where lasto transmits (finding P1).
    """

    __slots__ = ()

    @property
    def request_id(self) -> int:
        return self[0]  # type: ignore[no-any-return]

    @property
    def response_id(self) -> int:
        return self[1]  # type: ignore[no-any-return]

    @property
    def ext_address(self) -> int | None:
        return self[2]  # type: ignore[no-any-return]

    @property
    def kind(self) -> EcuKind:
        return self[3]  # type: ignore[no-any-return]


# Each approved entry with its route, copied here and never read from the entry again.
_ROUTES = tuple((ecu, Route((ecu.request_id, ecu.response_id, ecu.ext_address, ecu.kind))) for ecu in APPROVED_ECUS)
REQUEST_IDS = frozenset(route.request_id for _, route in _ROUTES)
_BY_REQUEST_ID = MappingProxyType({route.request_id: route for _, route in _ROUTES})
_BY_RESPONSE_ID = MappingProxyType({route.response_id: route for _, route in _ROUTES})


def route_of(ecu: object) -> Route | None:
    """The route of one of the table's own entries, by identity. A copy, or a look-alike whose fields merely
    compare equal (an int subclass as its ID, say), has none, so every ID the gate uses comes from a route."""
    for entry, route in _ROUTES:
        if ecu is entry:
            return route
    return None


def is_approved(ecu: object) -> bool:
    """Only the table's own entries, by identity (see route_of)."""
    return route_of(ecu) is not None


def by_request_id(can_id: int) -> Route | None:
    return _BY_REQUEST_ID.get(can_id)


def by_response_id(can_id: int) -> Route | None:
    return _BY_RESPONSE_ID.get(can_id)


freeze(__name__)
