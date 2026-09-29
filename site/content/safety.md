---
stamp: preliminary
phase: 1
order: 2
toc: true
title: "Safety: what Lasto will and won't send | Lasto"
h1: What Lasto will and won't send to your truck
description: "Passive mode sends nothing. Polled mode, planned for Phase 4, will send only read-only diagnostic requests, checked against allowlists and a kill switch."
lede: "Lasto is built to read and nothing else. It won't clear codes, reset modules, or write anything, and no setting changes that. This page lists each rule, the part of the code that enforces it, and how the tests check it."
page_type: TechArticle
about_app: true
---

## The short version {#summary}

- Passive mode sends nothing. The PCAN-USB is in hardware listen-only mode, and Lasto checks that before it records a single frame.
- Polled mode, planned for Phase 4, will send only read-only diagnostic requests, and the flow-control frames that go with them.
- {{ facts.never_services|length }} services, like clearing codes and security access, are on a never-list that no setting can turn on.
- Standard OBD-II requests can go to `0x{{ facts.functional_id|hex3 }}`, the address every module with OBD-II data answers. Everything else goes only to modules on an approved list, and today that list has {{ facts.ecus|length }} {{ "entry" if facts.ecus|length == 1 else "entries" }}: {% for e in facts.ecus %}the {{ e.name }} computer{% if not loop.last %}, {% endif %}{% endfor %}.
- Rate limits, a kill switch, a motion interlock, and a battery guard sit in front of every request.
- Everything it sends or refuses goes in the audit log first.

::: side
The service tables, rates, limits, timeouts, module list, and hotkey on this page are read from the safety core's source each time the site is built, so they can't drift from the code.
:::

## Can a data logger damage my truck? {#damage}

A logger in listen-only mode can't disturb the bus with anything it sends, because it sends nothing. Its CAN controller doesn't transmit, and it doesn't even acknowledge other modules' frames.

Most OBD-II apps and dongles don't just listen. They poll, sending read-only requests the way Lasto's polled mode will. Standard read requests are what the diagnostic port is for, but they still add traffic, and a cheap or misconfigured adapter can flood the bus, answer at the wrong bit rate, or keep modules awake after you walk away. The real damage comes from tools that send more than reads: clearing codes, running actuator tests, reflashing, or security access. Lasto's polled mode has rate limits, a kill switch, and a permanent never-list for exactly these reasons.

The physical side is real too. A bent pin, a miswired splitter, or a cable routed near the pedals can cause problems no software rule can stop. Pin 16 has battery power with the key off, so anything left plugged in can drain the battery while the truck sits. The PCAN-USB draws its power from the laptop's USB port. Even so, unplug the adapters before the truck sits for days.

## Passive mode sends nothing {#listen-only}

Before it records, Lasto opens the PCAN-USB in this order, and stops at the first step that fails:

1. The channel has to be free. If PCAN-View or another program holds it, the adapter might already be active, so Lasto refuses.
2. Turn on listen-only mode, before connecting to the bus.
3. Connect at 500 kbps.
4. Read the listen-only setting back. If it doesn't say on, disconnect and refuse to record.

There's no fallback that tries again after connecting. The passive code never loads the PCAN transmit function, and a structural test reads the source to make sure it stays that way.

```figure ack-slot 7E8#04410C0AF0000000
```

One thing PEAK doesn't promise in writing is that the adapter spends no time at all in normal mode as it joins the bus; its documentation says only that listen-only applies "as fast as possible." A bench test, with the PCAN-USB and the MX+ testing each other, will prove it on this adapter. It hasn't been done yet.

::: side
Enforced in {{ code_file("pcan_passive.py") }} and {{ code_file("pcan_dll.py") }}.
:::

## Polled mode asks read-only questions {#polled}

Polled mode is planned for Phase 4. Every request will go through these checks in order, and stop at the first one that fails:

1. The kill switch hasn't tripped.
2. The session has listened for {{ facts.listen_window|num }} seconds first, so it knows no other scan tool is talking. A passive session has no way to send at all.
3. It's a typed request built by Lasto's own request functions, and no earlier request is still waiting for an answer.
4. It's addressed to an approved module, or to `0x{{ facts.functional_id|hex3 }}`.
5. The interlocks allow it right now: parked-only work needs 0 mph, the battery guard has to be satisfied, and a logging request has to be in the logging profile picked when the session started.
6. It fits in one CAN frame. Lasto never sends multi-frame requests.
7. A separate check re-reads the encoded bytes. The frame has to be 8 bytes long, the ID has to be `0x{{ facts.functional_id|hex3 }}` or an approved module and never an ID the truck uses for its own traffic, and the service has to be on the read-only allowlist and off the never-list. Manufacturer services never go to `0x{{ facts.functional_id|hex3 }}`, and airbag and immobilizer modules only get trouble-code reads.
8. It's under the rate limit.
9. It's written to the audit log. If the log can't be written, nothing is sent.

Right before the frame goes out, the exact bytes are checked again.

### Flow control {#flow-control}

When a module starts a long answer, like the VIN, it waits for a flow-control frame before it sends the rest. Lasto sends that frame (`30 00 00`, padded to 8 bytes) only when a request of its own is waiting, only to that approved module's request ID and never to `0x{{ facts.functional_id|hex3 }}`, and at most once per answer. It's checked and logged like a request.

::: side
Enforced in {{ code_file("gate.py") }}.
:::

## Allowed and never-allowed requests {#services}

These tables come from the safety core's source when the site is built. The [service map](/#modes) on the home page shows the same thing as a map. Standard OBD-II services can go to one approved module or to every module at once on `0x{{ facts.functional_id|hex3 }}`. Manufacturer services only ever go to one approved module at a time, so a single manufacturer request can't reach every module.

Table: Services polled mode may send

| Service | What it asks for | Kind |
|---|---|---|
{% for s in facts.allowed_services|sort %}| `0x{{ s|hex2 }}` | {{ services.allowed[s] }} | {{ "OBD-II" if s in facts.obd_services else "Manufacturer read" }} |
{% endfor %}

Table: Services Lasto never sends, in any mode

| Service | What it does |
|---|---|
{% for s in facts.never_services|sort %}| `0x{{ s|hex2 }}` | {{ services.never[s] }} |
{% endfor %}

::: side
Reading memory by address (`0x23`) is a read, but it's on the never-list anyway, because it reaches raw memory instead of defined data.

Airbag and immobilizer modules only ever get trouble-code reads: {% for s in facts.dtc_read_services|sort %}`0x{{ s|hex2 }}`{% if not loop.last %}, {% endif %}{% endfor %}.

The allowlist gets trimmed in Phase 3 to what the Creader captures show Toyota actually uses on this truck.

Enforced in {{ code_link("NEVER_SERVICES", "policy.py") }}.
:::

## Which modules it will ask {#modules}

Manufacturer requests and flow control only go to modules on the approved list. Today it has {{ facts.ecus|length }} {{ "entry" if facts.ecus|length == 1 else "entries" }}: {% for e in facts.ecus %}the {{ e.name }} computer, with requests on `0x{{ e.request_id|hex3 }}` and answers on `0x{{ e.response_id|hex3 }}`{% if not loop.last %}; {% endif %}{% endfor %}. Each new module needs evidence from a Creader capture or a reviewed discovery result, and its own reviewed change to the safety core.

Standard OBD-II requests to `0x{{ facts.functional_id|hex3 }}` reach every module that answers OBD-II, which is how every generic scan tool works. Only the services in the OBD-II column above can go there.

Airbag and immobilizer modules only ever get asked for trouble codes, and they're left out of every identifier sweep.

If another scan tool starts talking on one of those addresses during a polled session, Lasto stops asking. Two testers at once can confuse a module.

::: side
Enforced in {{ code_link("APPROVED_ECUS", "ecus.py") }}.
:::

## Rate limits and the kill switch {#kill-switch}

Logging requests will go out at up to {{ facts.rates.logging|num }} per second. Snapshots get {{ facts.rates.snapshot|num }} per second, identification {{ facts.rates.identify|num }}, and discovery {{ facts.rates.discovery|num }}, and the speed, RPM, and voltage checks for the interlocks get {{ facts.rates.interlock_probe|num }}. A hard ceiling of {{ facts.ceiling|num }} per second sits under all of them.

If a module says it's busy (NRC `0x21`), Lasto backs off, starting at {{ facts.backoff_start|num }} seconds and doubling up to {{ facts.backoff_max|num }}. If it says it's still working (NRC `0x78`), Lasto waits up to {{ facts.response_pending_wait|num }} seconds without asking again, and gives up on the request after {{ facts.response_pending_max }} of those in a row.

During a polled session, the kill switch stops all sending the moment any of these happens:

- a CAN error frame, or the adapter going error-passive or bus-off
- a receive overrun, where frames were lost because the queue filled up
- an interface error, or the adapter coming unplugged
- a frame on one of Lasto's request addresses that Lasto didn't send, meaning another scan tool is talking
- an answer to a question it didn't ask, a malformed answer, or a module asking Lasto for flow control
- {{ facts.nrc_consecutive }} negative answers in a row to the same request, or {{ facts.nrc_window_limit }} in any {{ facts.nrc_window_seconds|num }} seconds (discovery counts "not supported" answers against a separate, larger limit)
- {{ facts.timeout_limit }} timeouts in a row
- an error in anything that reads the incoming frames
- you pressing {{ facts.hotkey }}

A request that fails a check is refused and logged, and the session carries on. Once the kill switch trips, it stays tripped until Lasto restarts. Nothing retries. Passive capture keeps recording unless the adapter itself failed.

::: side
{{ facts.hotkey }} works even when the terminal isn't focused. It's built and tested with fakes, and gets wired into real sessions in Phase 4.

Enforced in {{ code_link("PURPOSE_RATES", "ratelimit.py") }}, {{ code_link("CONSECUTIVE_NRC_LIMIT", "killswitch.py") }}, and {{ code_file("reader.py") }}.
:::

## Parked-only work, and driving {#driving}

Health snapshots, identification, and discovery only run at 0 mph, checked before every request, and an unknown speed counts as moving.

While you're driving, the only requests that go out are the ones in the logging profile you picked before you left, plus the speed, RPM, and voltage checks the interlocks need. You don't need to touch the laptop.

With the engine off, snapshots, identification, and discovery stop if module voltage drops below {{ facts.min_engine_off_voltage|num }} V, or if it can't be read.

Don't operate a laptop while driving. {{ facts.hotkey }} is for when you're parked, or when someone's in the passenger seat.

::: side
Enforced in {{ code_link("MIN_ENGINE_OFF_VOLTAGE", "interlocks.py") }}.
:::

## The OBDLink MX+ {#mx-plus}

The MX+ only gets setup and read commands from a fixed list, matched exactly after Lasto normalizes case and spacing. Commands that send arbitrary frames, change the adapter's saved settings, or touch its firmware are refused. Its silent monitoring mode can't be read back, so Lasto checks it indirectly before every monitor start. Right now there's no path that lets the MX+ send anything to the truck.

Whether Lasto ever asks K-line modules anything is a Phase 7 decision, because starting a K-line session needs a service (`0x81`) that isn't on the allowlist.

::: side
Enforced in {{ code_file("stn_policy.py") }} and {{ code_file("stn_port.py") }}.
:::

## Simulator by default {#simulator}

Every command uses the simulator unless you type `--live` plus the adapter's channel or port, every time. Shortened versions of `--live` are rejected, and there's no saved default channel. There's no raw console for real hardware; if one is ever added, it'll only talk to the simulator.

## The audit log {#audit}

Every frame Lasto sends and every request it refuses is written to the audit log with the time and the reason, before anything goes out. If the log can't be written, nothing is sent.

::: side
Enforced in {{ code_file("audit.py") }} and {{ code_file("gate.py") }}.
:::

## How the tests check the rules {#tests}

- Unit tests cover every allow and deny decision, for both adapters.
- Property-based tests (Hypothesis) throw random IDs, payloads, and adapter commands at the send path, and check that nothing outside the allowlists reaches the fake driver.
- Structural tests read the source: only one file can call the PCAN transmit function, only one file can open a serial port, and the passive code can reach neither.
- A simulated truck fails the whole test run if it ever receives something forbidden, even when the code under test caught the error.

The test command fails below 100 percent branch coverage on the safety core.

## What isn't proven yet {#not-proven}

- Nothing has run on the truck, or on a real PCAN-USB or MX+. Every test so far runs against the simulator.
- The listen-only and silent-mode bench tests haven't been done. There's no bench bus yet.
- The engine computer's address comes from the OBD-II standard and the service manual. No capture has confirmed it yet.
- The padding byte for requests is `0x{{ facts.padding_byte|hex2 }}` until a Creader capture confirms what Toyota uses.
- The {{ facts.hotkey }} hotkey is built and tested with fakes. It gets wired into real sessions in Phase 4.

Items come off this list as phases close them.

## Reporting a security problem {#report}

If you find a way to get Lasto to send something it shouldn't, please report it privately through [GitHub's private vulnerability reporting]({{ site.security_contact }}) instead of a public issue. The [security policy]({{ site.security_policy }}) has the details.

## No warranty {#warranty}

Lasto is read-only by design. Passive mode sends nothing, and polled mode will send only read-only diagnostic requests, through the rules on this page. It's free software under {{ site.license }} and comes with no warranty, to the extent the law allows. You use it on your own vehicle at your own risk. Plugging anything into the OBD-II port carries some risk of its own, so route cables away from the pedals and don't operate a laptop while driving.
