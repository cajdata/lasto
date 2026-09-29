---
layout: home
stamp: objective
order: 1
title: "Lasto: a read-only data logger for a 2006 Lexus GX470"
h1: A read-only data logger for a 2006 Lexus GX470
description: "Lasto is a free, read-only data logger being built for a 2006 Lexus GX470. Passive mode sends nothing. Pre-alpha: nothing runs at the truck yet."
lede: "Lasto is being built to record what a GX470's modules say on the OBD-II port, and to ask read-only questions for data the truck may not broadcast, like transmission temperature on a long grade."
og_text: "Passive mode sends nothing. Polled mode will ask read-only questions, and only through the allowlist."
page_type: WebPage
about_app: true
jsonld: [WebSite, SoftwareApplication, SoftwareSourceCode]
---

## Two modes {#modes}

Lasto is being built with two modes, and both use a PEAK PCAN-USB interface on the truck's high-speed CAN bus. Fig. 1 shows what each one may send.

```figure service-map
```

### Passive mode {#passive}

In passive mode, Lasto puts the PCAN-USB in hardware listen-only mode, reads the setting back, and won't record if it can't confirm it. While it records, it reads the setting again every {{ facts.status_interval|num }} seconds. In listen-only mode the adapter's CAN controller sends nothing on the bus. It doesn't even send the acknowledge bit every other module sends.

[What isn't proven yet](/safety/#not-proven)

### Polled mode {#polled tag="Phase 4"}

Polled mode arrives in Phase 4. It sends read-only diagnostic requests, plus the flow-control frame a module needs before a long answer, and every one goes through allowlists, rate limits, and a kill switch first.

```frames
caption: Example frames in standard OBD-II format, made up for this page, since there's no capture from this truck yet. A request for engine speed, and the engine computer's answer.
7DF#02010C0000000000  request: engine speed
7E8#04410C0AF0000000  answer: 0x0AF0 / 4 = 700 rpm
```

### Never allowed {#never}

It won't clear codes, reset or reflash a module, open a diagnostic session, ask for security access, or run actuator tests. No setting turns those on. That's your scan tool's job. The [Safety page](/safety/) lists every rule and where the code enforces it.

::: side
Table: Quick reference data

| Parameter | Value |
|---|---|
| Vehicle | {{ hardware.vehicle.name }}: {{ hardware.vehicle.engine }}, {{ hardware.vehicle.transmission }}, {{ hardware.vehicle.features }} |
| CAN bus | {{ hardware.vehicle.can_bus }}, DLC3 pins 6 and 14 |
| K-line | DLC3 pin 7. KDSS talks here, per the factory diagram |
| CAN interface | {{ hardware.legs[0].full }}, {{ hardware.legs[0].variant }}, {{ hardware.legs[0].model }} |
| K-line interface | {{ hardware.legs[1].full }} over Bluetooth, from Phase 3 |
| Sends in passive mode | Nothing. The passive code path has no transmit call |
| Request rate, polled mode | Up to {{ facts.rates.logging|num }} per second while logging, {{ facts.rates.discovery|num }} during discovery, never more than {{ facts.ceiling|num }} per second in total |
| Addresses it may ask | Standard OBD-II requests: `0x{{ facts.functional_id|hex3 }}`, which every OBD-II module answers. Everything else: {% for e in facts.ecus %}the {{ e.name }} computer, requests on `0x{{ e.request_id|hex3 }}` and answers on `0x{{ e.response_id|hex3 }}`{% if not loop.last %}; {% endif %}{% endfor %} |
| Broadcast IDs | None recorded yet (Phase 2) |
| Runs on | Windows, {{ python_req }} |
| License | {{ site.license }}, no warranty |

Sources: Toyota's wiring diagrams for the 2006 GX470, PEAK-System's PCAN-USB documentation, and Lasto's own source, which the site reads every time it's built.
:::

## How it connects {#how-it-connects}

The GX470 is Lexus's version of the Toyota Land Cruiser Prado, and its diagnostic port is Toyota's standard DLC3, so the approach should carry over to related Toyota trucks. Only the GX470 is being built for and tested, though.

Everything will plug into the truck's OBD-II port through one 3-way splitter, so the scan tool, the PCAN-USB, and the MX+ can share it.

```figure chain
```

## Where it stands {#status}

{{ status_sentence }} Lasto gets built in ten phases, and each one stops for my review before the next starts.

Table: Build phases

| Phase | What it builds | Status |
|---|---|---|
{% for p in roadmap.phases %}| [Phase {{ p.number }}](/roadmap/#{{ p.anchor }}) | {{ p.name }} | {{ p.status_label }} |
{% endfor %}

The [roadmap](/roadmap/) has the details, including Phase 4, the first one that sends anything to the truck.

::: side
The roadmap updates each time a phase is signed off. To follow along, watch the repository on GitHub.
:::
