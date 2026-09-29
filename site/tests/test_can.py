"""Bit-level CAN encoding behind the ACK-slot figure."""

from sitegen import can


def _destuff(bits):
    out, run_level, run, skip = [], None, 0, False
    for b in bits:
        if skip:
            skip = False
            run_level, run = b, 1
            continue
        out.append(b)
        run = run + 1 if b == run_level else 1
        run_level = b
        if run == 5:
            skip = True
    return out


def test_crc15_standard_check_value():
    # CRC-15/CAN of the ASCII string "123456789" is 0x059E (reveng catalogue).
    bits = [b for byte in b"123456789" for b in can.bits_of(byte, 8)]
    assert can.crc15(bits) == 0x059E


def test_known_frame():
    f = can.encode(*can.parse_frame("7E8#04410C1AF8000000"))
    assert f.crc == 0x5389
    assert f.stuff_count == 11
    assert len(f.bits) == 122  # including 3 bits of intermission
    assert f.ack_index + 1 == 111


def test_site_frame_is_well_formed():
    f = can.encode(*can.parse_frame("7E8#04410C0AF0000000"))
    stuffed = [b.level for b in f.bits if b.field not in ("CRCDEL", "ACK", "ACKDEL", "EOF", "IFS")]
    # No run of six identical bits anywhere in the stuffed part.
    for i in range(len(stuffed) - 5):
        assert len(set(stuffed[i : i + 6])) == 2
    raw = _destuff(stuffed)
    assert raw[0] == 0  # SOF is dominant
    assert can.crc15(raw[:-15]) == f.crc
    assert [b.level for b in f.bits[f.ack_index - 1 :]] == [1] * (len(f.bits) - f.ack_index + 1)
