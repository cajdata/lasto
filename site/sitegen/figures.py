"""Figures: generated SVG for the HTML pages, and a text version of each for the Markdown mirrors.

A figure appears in content as a fenced block:

    ```figure service-map
    ```

The HTML renderer and the mirror writer both call the same figure function, so the
picture and its text can't say different things. Every figure's facts also appear as
text on the page (a list, a table, or the caption).
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Callable

from sitegen import can
from sitegen.data import BuildError


@dataclass
class Figure:
    html: str
    markdown: str
    wide: bool = True


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def hex2(n: int) -> str:
    return f"{n:02X}"


# ---------------------------------------------------------------------------
# Fig.: service map. Generated from the safety core's allowlist, never hand-coded.
# ---------------------------------------------------------------------------

_CELL, _X0, _Y0 = 18, 28, 18


def _map_defs() -> str:
    cols = "".join(f'<text x="{_X0 + 9 + _CELL * c}" y="13">{c:X}</text>' for c in range(16))
    rows = "".join(f'<text x="23" y="{_Y0 + 13 + _CELL * r}">{r:X}0</text>' for r in range(16))
    grid = "".join(f"M{_X0 + .5} {_Y0 + .5 + _CELL * r}h288" for r in range(1, 16))
    grid += "".join(f"M{_X0 + .5 + _CELL * c} {_Y0 + .5}v288" for c in range(1, 16))
    return (
        '<svg class="sprite" aria-hidden="true" focusable="false"><defs>'
        f'<g id="smax"><g text-anchor="middle">{cols}</g><g text-anchor="end">{rows}</g></g>'
        f'<path id="smgl" d="{grid}"/>'
        f'<pattern id="cells" patternUnits="userSpaceOnUse" x="{_X0}" y="{_Y0}" width="{_CELL}" height="{_CELL}"><rect class="cell" x="2" y="2" width="14" height="14"/></pattern>'
        '<pattern id="hatch-l" class="hatch" patternUnits="userSpaceOnUse" width="3" height="3" patternTransform="rotate(45)"><path d="M0 0V3"/></pattern>'
        '<pattern id="hatch-d" class="hatch" patternUnits="userSpaceOnUse" width="4" height="4" patternTransform="rotate(45)"><path d="M0 0V4"/></pattern>'
        "</defs></svg>"
    )


def _map_svg(label_id: str, allowed: frozenset[int], never: frozenset[int], empty_note: str = "") -> str:
    parts = [
        f'<svg class="smap" viewBox="0 0 317 307" width="317" height="307" role="img" '
        f'aria-labelledby="{label_id}{" " + label_id + "-note" if empty_note else ""}">',
        '<use class="ax" href="#smax"/>',
        f'<rect class="cells" x="{_X0}" y="{_Y0}" width="288" height="288"/>',
        '<use class="gl" href="#smgl"/>',
        f'<rect class="frame" x="{_X0 + .5}" y="{_Y0 + .5}" width="288" height="288"/>',
    ]
    if allowed:
        d = "".join(f"M{_X0 + 2 + _CELL * (s & 15)} {_Y0 + 2 + _CELL * (s >> 4)}h14v14h-14z" for s in sorted(allowed))
        parts.append(f'<path class="allow" d="{d}"/>')
    if never:
        d = "".join(f"M{_X0 + 2.5 + _CELL * (s & 15)} {_Y0 + 2.5 + _CELL * (s >> 4)}h13v13h-13z" for s in sorted(never))
        parts.append(f'<path class="never" d="{d}"/>')
    if allowed:
        labels = "".join(
            f'<text x="{_X0 + 9 + _CELL * (s & 15)}" y="{_Y0 + 12 + _CELL * (s >> 4)}">{hex2(s)}</text>' for s in sorted(allowed)
        )
        parts.append(f'<g class="lbl" text-anchor="middle">{labels}</g>')
    if empty_note:
        parts.append(
            f'<g class="none"><rect class="plate" x="101" y="163" width="143" height="17"/>'
            f'<text id="{label_id}-note" x="172" y="175" text-anchor="middle">{esc(empty_note)}</text></g>'
        )
    parts.append("</svg>")
    return "".join(parts)


def service_map(ctx: dict, number: int) -> Figure:
    facts = ctx["facts"]
    services = ctx["services"]
    phase4 = ctx["roadmap"].phase(4)
    allowed, never = facts.allowed_services, facts.never_services
    obd = " ".join(hex2(s) for s in sorted(facts.obd_services))
    mfr = " ".join(hex2(s) for s in sorted(facts.manufacturer_services))
    nev = " ".join(hex2(s) for s in sorted(never))
    cap_id = f"fig{number}cap"
    polled_when = "Polled mode" if phase4.done else "Polled mode, Phase&nbsp;4"
    caption = (
        f"<b>Fig. {number}.</b> Service map, generated from the safety core’s allowlist every time the site is built. "
        "One cell for every possible service byte, 0x00 to 0xFF. Row plus column gives the byte: row 20, column 2 is 0x22. "
        "Phase&nbsp;3 captures may trim the allowlist. Flow-control frames carry no service byte, so they aren’t cells here."
    )
    out = f"""{_map_defs()}
<figure class="smap-fig c-wide" aria-labelledby="{cap_id}">
<div class="mapbox"><div class="maps">
<div class="map"><p class="maphd" id="smh-passive"><b>Passive mode</b> <span>0 of 256</span></p>
{_map_svg("smh-passive", frozenset(), frozenset(), "no transmit path")}</div>
<div class="map"><p class="maphd" id="smh-polled"><b>{polled_when}</b> <span>{len(allowed)} of 256</span></p>
{_map_svg("smh-polled", allowed, never)}</div>
</div></div>
<div class="figtext">
<ul class="legend">
<li><svg class="sw" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" focusable="false"><rect class="sw-a" width="12" height="12"/></svg>Allowed, read-only</li>
<li><svg class="sw" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" focusable="false"><rect class="sw-n" x=".5" y=".5" width="11" height="11"/></svg>Never allowed</li>
<li><svg class="sw" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" focusable="false"><rect class="sw-d" x=".5" y=".5" width="11" height="11"/></svg>Denied by default</li>
</ul>
<p class="cap" id="{cap_id}">{caption}</p>
<p class="svh">{polled_when}: service bytes</p>
<dl class="svcs">
<dt><span class="nw">OBD-II</span></dt><dd>{obd}</dd>
<dt>Manufacturer reads</dt><dd>{mfr} <span class="q">(one approved module at a time, never the functional address 0x{facts.functional_id:03X})</span></dd>
<dt class="nvr">Never</dt><dd class="nvr">{nev}</dd>
</dl>
</div>
</figure>"""
    md = (
        f"Fig. {number}. Service map, generated from the safety core's allowlist on every build. "
        "There are 256 possible service bytes (0x00 to 0xFF).\n\n"
        f"- Passive mode: 0 of 256. The passive code path has no transmit call.\n"
        f"- Polled mode (Phase 4): {len(allowed)} of 256, all read-only.\n"
        f"  - OBD-II: {obd}\n"
        f"  - Manufacturer reads, to one approved module at a time and never to 0x{facts.functional_id:03X}: {mfr}\n"
        f"- Never allowed, in any mode: {nev}\n"
        "- Everything else is denied by default.\n"
    )
    return Figure(out, md)


# ---------------------------------------------------------------------------
# Fig.: the ACK slot. A real frame encoded bit by bit, with the listen-only TX line flat.
# ---------------------------------------------------------------------------

_FIELD_NAMES = {
    "SOF": "Start of frame",
    "ID": "Identifier",
    "CTRL": "RTR, IDE, r0",
    "DLC": "Data length",
    "DATA": "Data",
    "CRC": "CRC",
    "CRCDEL": "CRC delimiter",
    "ACK": "ACK slot",
    "ACKDEL": "ACK delimiter",
    "EOF": "End of frame",
    "IFS": "Intermission",
}


def _group(field: str) -> str:
    if field in ("RTR", "IDE", "r0"):
        return "CTRL"
    if field.startswith("D") and field[1:].isdigit():
        return "DATA"
    return field


def ack_slot(ctx: dict, number: int, frame_text: str) -> Figure:
    can_id, data = can.parse_frame(frame_text)
    frame = can.encode(can_id, data)
    bits = frame.bits
    n = len(bits)
    bw, left, top = 7, 164, 34
    width = left + n * bw + 12
    lane_h, gap = 28, 26
    bus_y = top
    tx_y = top + lane_h + gap
    band_y = tx_y + lane_h + 18
    height = band_y + 58

    def level_y(level: int, y0: int) -> float:
        return y0 + (2.5 if level else lane_h - 2.5)

    ack = frame.ack_index
    bus_levels = [0 if i == ack else b.level for i, b in enumerate(bits)]  # other modules ACK

    def trace(levels: list[int], y0: int) -> str:
        """One H per run of equal bits, and a V at each change: the same line, drawn with fewer bytes."""
        d = f"M{left} {level_y(levels[0], y0)}"
        for i in range(1, len(levels)):
            if levels[i] != levels[i - 1]:
                d += f"H{left + i * bw}V{level_y(levels[i], y0)}"
        return d + f"H{left + len(levels) * bw}"

    groups: list[tuple[str, int, int]] = []
    for i, b in enumerate(bits):
        g = _group(b.field)
        if groups and groups[-1][0] == g:
            groups[-1] = (g, groups[-1][1], i)
        else:
            groups.append((g, i, i))

    ack_x = left + ack * bw
    stuff_marks = "".join(
        f'<rect class="stuff" x="{left + i * bw + 2}" y="{bus_y - 7}" width="{bw - 4}" height="3"/>' for i, b in enumerate(bits) if b.stuff
    )
    ticks = "".join(f"M{left + i * bw}.5 {band_y}v6" for _, i, _ in groups) + f"M{left + n * bw}.5 {band_y}v6"
    labels = []
    for g, a, b in groups:
        span = (b - a + 1) * bw
        cx = left + a * bw + span / 2
        if g == "DATA":
            text = "Data: " + " ".join(f"{x:02X}" for x in data)
        elif g == "ID":
            text = f"ID {can_id:03X}"
        elif g == "DLC":
            text = f"DLC {len(data)}"
        elif g == "CRC":
            text = f"CRC {frame.crc:04X}"
        elif g == "EOF":
            text = "End of frame"
        elif g in ("SOF", "CTRL", "CRCDEL", "ACKDEL", "IFS", "ACK"):
            text = ""
        else:
            text = g
        if text:
            labels.append(f'<text class="fl" x="{cx:.1f}" y="{band_y + 20}" text-anchor="middle">{esc(text)}</text>')
    cap_id = f"fig{number}cap"
    tbl_id = f"fig{number}tbl"
    svg = f"""<svg class="ackd" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="Timing diagram: the bus and Lasto’s transmit line, bit by bit" aria-describedby="{cap_id} {tbl_id}">
<rect class="ackband" x="{ack_x}" y="{bus_y - 12}" width="{bw}" height="{tx_y + lane_h - bus_y + 16}"/>
<text class="ln-t" x="0" y="{bus_y + 11}">Bus</text><text class="ln-s" x="0" y="{bus_y + 25}">what every module sees</text>
<text class="ln-t" x="0" y="{tx_y + 11}">Lasto TX</text><text class="ln-s" x="0" y="{tx_y + 25}">listen-only mode</text>
<text class="lvl" x="{left - 6}" y="{bus_y + 6}" text-anchor="end">R</text><text class="lvl" x="{left - 6}" y="{bus_y + lane_h - 1}" text-anchor="end">D</text>
<text class="lvl" x="{left - 6}" y="{tx_y + 6}" text-anchor="end">R</text><text class="lvl" x="{left - 6}" y="{tx_y + lane_h - 1}" text-anchor="end">D</text>
<path class="lane" d="M{left} {bus_y + lane_h + .5}H{left + n * bw}M{left} {tx_y + lane_h + .5}H{left + n * bw}"/>
<path class="wave" d="{trace(bus_levels, bus_y)}"/>
<path class="wave tx" d="{trace([1] * n, tx_y)}"/>
{stuff_marks}
<path class="tick" d="{ticks}"/>
{''.join(labels)}
<text class="ack-t" x="{ack_x + bw / 2}" y="{bus_y - 16}" text-anchor="middle">ACK</text>
<path class="ack-p" d="M{ack_x + bw / 2} {tx_y + lane_h + 4}V{band_y - 2}"/>
</svg>"""
    total = sum(1 for b in bits if b.field != "IFS")
    ifs = n - total
    what = ""
    if len(data) >= 5 and data[1] == 0x41 and data[2] == 0x0C:  # Mode 01 PID 0C answer: RPM = (A * 256 + B) / 4
        what = f" (engine speed, {(data[3] * 256 + data[4]) / 4:g} rpm)"
    caption = (
        f"<b>Fig. {number}.</b> An engine computer’s answer, <span class=\"nw\">{esc(frame_text)}</span>{what.replace(' rpm', '&nbsp;rpm')}, "
        f"as it crosses the bus bit by bit (R is recessive, D is dominant). The frame takes {total} bit times, {frame.stuff_count} of them "
        f"stuff bits (marked above the trace), and then {ifs} bits of intermission. "
        f"At the ACK slot, bit {ack + 1}, every module that received the frame pulls the bus dominant. "
        "In listen-only mode the PCAN-USB’s controller doesn’t, so its transmit line stays recessive for the whole frame. "
        "Generated from the frame at build time."
    )
    field_rows = []
    for g, a, b in groups:
        if g == "IFS":
            continue
        field_rows.append((_FIELD_NAMES[g], f"{a + 1}" if a == b else f"{a + 1} to {b + 1}"))
    table = "".join(f"<tr><th scope=\"row\">{esc(name)}</th><td>{esc(rng)}</td></tr>" for name, rng in field_rows)
    out = f"""<figure class="ackfig c-wide" aria-labelledby="{cap_id}">
<p class="scroll-hint" aria-hidden="true">The diagram scrolls sideways. The ACK slot is near the right end.</p>
<div class="scroll" tabindex="0" role="region" aria-label="Timing diagram, scrolls sideways">{svg}</div>
<div class="acktext">
<p class="cap" id="{cap_id}">{caption}</p>
<table class="qr" id="{tbl_id}"><caption>Bit positions in Fig. {number}</caption><thead><tr><th scope="col">Field</th><th scope="col">Bits</th></tr></thead><tbody>{table}</tbody></table>
</div>
</figure>"""
    md_rows = "\n".join(f"| {name} | {rng} |" for name, rng in field_rows)
    md = (
        f"Fig. {number}. An engine computer's answer, {frame_text}{what}, crossing the bus bit by bit. The frame takes {total} bit times, "
        f"{frame.stuff_count} of them stuff bits, and then {ifs} bits of intermission. At the ACK slot (bit {ack + 1}) every module that received the frame "
        "pulls the bus dominant. In listen-only mode the PCAN-USB's controller doesn't, so its transmit line stays recessive "
        "for the whole frame.\n\n| Field | Bits |\n|---|---|\n" + md_rows + "\n"
    )
    return Figure(out, md)


# ---------------------------------------------------------------------------
# Ex.: example frames. Content fence "frames"; the first line is "caption: ...".
# ---------------------------------------------------------------------------


def frames(body: str, number: int, render_inline: Callable[[str], str]) -> Figure:
    lines = [ln.rstrip() for ln in body.strip("\n").splitlines()]
    if not lines or not lines[0].lower().startswith("caption:"):
        raise BuildError("a frames block must start with 'caption: ...'")
    caption = lines[0].split(":", 1)[1].strip()
    rows = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        frame, _, comment = ln.partition("  ")
        rows.append((frame.strip(), comment.strip()))
    width = max(len(f) for f, _ in rows)
    cap_id = f"ex{number}cap"
    code = "".join(
        f'<span class="ln"><span class="fr">{esc(f)}</span><span class="sp">{" " * (width - len(f) + 3)}</span><span class="cm">{esc(c)}</span></span>'
        for f, c in rows
    )
    out = (
        f'<figure class="frames" aria-labelledby="{cap_id}">'
        f'<pre><code>{code}</code></pre>'
        f'<figcaption id="{cap_id}"><b>Ex. {number}.</b> {render_inline(caption)}</figcaption></figure>'
    )
    text = "\n".join(f"{f.ljust(width)}   {c}".rstrip() for f, c in rows)
    md = f"```\n{text}\n```\n\nEx. {number}. {caption}\n"
    return Figure(out, md, wide=False)


# ---------------------------------------------------------------------------
# Fig.: how it connects. Drawn in a template; the facts come from hardware.toml.
# ---------------------------------------------------------------------------


def chain_items(hw: dict) -> list[tuple[str, str]]:
    """The chain as (label, sentence) pairs: the text version of the diagram."""
    pins = []
    for p in hw["port"]["pins"]:
        if p.get("wire"):
            pins.append(f"pin {p['pin']} {p['signal'][:3]}-{p['signal'][3:]} on a {p['wire_name']} ({p['wire']}) wire")
    by = pins_by_signal(hw)
    port = (
        f"{hw['port']['description']}: " + ", ".join(pins)
        + f", pin {by['SIL']['pin']} the K-line (SIL), pin {by['BAT']['pin']} battery power (live with the key off), "
        f"pins {by['CG']['pin']} (CG) and {by['SG']['pin']} (SG) ground."
    )
    items = [(hw["port"]["name"], port), ("Splitter", hw["splitter"]["description"] + ".")]
    for leg in hw["legs"]:
        name = leg["full"] + (f" ({leg['model']})" if leg.get("model") else "")
        link = f", {leg['link']} to a {hw['laptop']['name']}" if leg.get("link") == "USB" else ""
        link = link or (f" over {leg['link']}" if leg.get("link") else "")
        items.append((f"Leg {leg['leg']}", f"{name}{link}. {leg['role']}"))
    return items


def pins_by_signal(hw: dict) -> dict[str, dict]:
    """The DLC3 pins keyed by signal (CANH, CANL, SIL, BAT, CG, SG), so the diagram's labels come from hardware.toml."""
    by = {p["signal"]: p for p in hw["port"]["pins"]}
    missing = {"CANH", "CANL", "SIL", "BAT", "CG", "SG"} - by.keys()
    if missing:
        raise BuildError(f"hardware.toml has no pin for {', '.join(sorted(missing))}; Fig. 2 labels them")
    return by


def chain(ctx: dict, number: int) -> Figure:
    hw = ctx["hardware"]
    items = chain_items(hw)
    out = ctx["render_partial"]("figures/chain.html", number=number, hw=hw, items=items, pins=pins_by_signal(hw))
    md = f"Fig. {number}. How it connects. {hw['planned_note']}\n\n" + "\n".join(
        f"{i}. {label}: {text}" for i, (label, text) in enumerate(items, 1)
    ) + "\n"
    return Figure(out, md)


FIGURES: dict[str, Callable[..., Figure]] = {
    "service-map": service_map,
    "ack-slot": ack_slot,
    "chain": chain,
}
