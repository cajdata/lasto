"""Bit-level encoding of a classic CAN data frame (11-bit ID), for the ACK-slot figure.

Follows ISO 11898-1: CRC-15 (polynomial 0x4599) over SOF through the data field, bit
stuffing from SOF through the CRC sequence, then the fixed-form tail. Bits are 0 for
dominant and 1 for recessive. The unit tests check the CRC against the standard check
value (CRC-15/CAN of "123456789" is 0x059E).
"""

from __future__ import annotations

from dataclasses import dataclass

CRC15_POLY = 0x4599


def crc15(bits: list[int]) -> int:
    crc = 0
    for bit in bits:
        nxt = bit ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= CRC15_POLY
    return crc


def bits_of(value: int, width: int) -> list[int]:
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


@dataclass(frozen=True)
class Bit:
    level: int  # 0 dominant, 1 recessive, as the sender drives it
    field: str  # SOF, ID, RTR, IDE, r0, DLC, D0..D7, CRC, CRCDEL, ACK, ACKDEL, EOF, IFS
    stuff: bool = False


@dataclass(frozen=True)
class Frame:
    can_id: int
    data: bytes
    bits: tuple[Bit, ...]
    crc: int

    @property
    def stuff_count(self) -> int:
        return sum(b.stuff for b in self.bits)

    @property
    def ack_index(self) -> int:
        return next(i for i, b in enumerate(self.bits) if b.field == "ACK")

    def span(self, field: str) -> tuple[int, int]:
        idx = [i for i, b in enumerate(self.bits) if b.field == field]
        return idx[0], idx[-1]

    @property
    def fields(self) -> list[str]:
        out: list[str] = []
        for b in self.bits:
            if not out or out[-1] != b.field:
                out.append(b.field)
        return out


def encode(can_id: int, data: bytes) -> Frame:
    if not 0 <= can_id <= 0x7FF:
        raise ValueError("11-bit identifiers only")
    if len(data) > 8:
        raise ValueError("classic CAN carries at most 8 data bytes")
    raw: list[tuple[int, str]] = [(0, "SOF")]
    raw += [(b, "ID") for b in bits_of(can_id, 11)]
    raw += [(0, "RTR"), (0, "IDE"), (0, "r0")]
    raw += [(b, "DLC") for b in bits_of(len(data), 4)]
    for n, byte in enumerate(data):
        raw += [(b, f"D{n}") for b in bits_of(byte, 8)]
    crc = crc15([b for b, _ in raw])
    raw += [(b, "CRC") for b in bits_of(crc, 15)]

    stuffed: list[Bit] = []
    run_level, run = -1, 0
    for level, field in raw:
        stuffed.append(Bit(level, field))
        if level == run_level:
            run += 1
        else:
            run_level, run = level, 1
        if run == 5:
            stuffed.append(Bit(1 - level, field, stuff=True))
            run_level, run = 1 - level, 1

    tail = [Bit(1, "CRCDEL"), Bit(1, "ACK"), Bit(1, "ACKDEL")] + [Bit(1, "EOF")] * 7 + [Bit(1, "IFS")] * 3
    return Frame(can_id, bytes(data), tuple(stuffed + tail), crc)


def parse_frame(text: str) -> tuple[int, bytes]:
    """'7E8#04410C0AF0000000' in candump form."""
    ident, _, payload = text.partition("#")
    return int(ident, 16), bytes.fromhex(payload)
