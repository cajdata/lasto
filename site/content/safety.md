---
stamp: preliminary
phase: 2
order: 2
toc: true
title: "Safety: what Lasto will and won't send | Lasto"
h1: What Lasto will and won't send to your truck
description: "Passive mode sends nothing. Polled mode, planned for Phase 4, will send only read-only diagnostic requests, checked against allowlists and a kill switch."
lede: "Lasto is built to read and nothing else. It won't clear codes, reset modules, or write anything, and no setting changes that. This page lists each rule, where the code enforces it, and what the design does and doesn't claim."
page_type: TechArticle
about_app: true
---

## The short version {#summary}

- Passive mode sends nothing. The PCAN-USB is in hardware listen-only mode, and Lasto checks that before it records a single frame, then again every {{ facts.status_interval|num }} seconds while it records.
- Polled mode, planned for Phase 4, will send only read-only diagnostic requests, and the flow-control frames that go with them.
- {{ facts.never_services|length }} services, like clearing codes and security access, are on a never-list that no setting can turn on.
- Standard OBD-II requests can go to `0x{{ facts.functional_id|hex3 }}`, the address every module with OBD-II data answers. Everything else goes only to modules on an approved list that's fixed when Lasto starts, and today that list has {{ facts.ecus|length }} {{ "entry" if facts.ecus|length == 1 else "entries" }}: {% for e in facts.ecus %}the {{ e.name }} computer{% if not loop.last %}, {% endif %}{% endfor %}.
- Rate limits, a kill switch, a motion interlock, and a battery guard sit in front of every request, and one write function checks every frame again before it goes out.
- Everything it sends goes in the audit log before it goes out. Refusals are logged too, or held until a log opens.

::: side
Every table, limit, and module on this page that the safety core defines is read from its source when the site is built, so none can drift from the code. The truck results come from Phase 2's tests.
:::

## Can a data logger damage my truck? {#damage}

A logger in listen-only mode can't disturb the bus with anything it sends, because it sends nothing. Its CAN controller doesn't transmit, and it doesn't even acknowledge other modules' frames.

Most OBD-II apps and dongles poll, sending read-only requests the way Lasto's polled mode will. Standard read requests are what the diagnostic port is for, but they still add traffic, and a cheap or misconfigured adapter can flood the bus, answer at the wrong bit rate, or keep modules awake after you walk away. The real damage comes from tools that send more than reads: clearing codes, running actuator tests, reflashing, or security access.

The physical side is real too. A bent pin, a miswired splitter, or a cable routed near the pedals can cause problems no software rule can stop. Pin 16 has battery power with the key off, so anything left plugged in can drain the battery. Unplug the adapters before the truck sits for days, and route cables away from the pedals.

## Passive mode sends nothing {#listen-only}

On real hardware, Lasto first checks that it runs on the system's own clock and that its audit log is the kind that syncs every record to disk. For now that check reads only the log's type ([a known gap](/safety/core/#audit)). Then it opens the PCAN-USB in this order, and stops at the first step that fails:

1. The channel has to be free. If PCAN-View or another program holds it, the adapter might already be active, so Lasto refuses.
2. Turn on listen-only mode, before connecting to the bus.
3. Connect at 500 kbps.
4. Read the listen-only setting back. If it doesn't say on, disconnect and refuse to record.

It never turns listen-only on after connecting, reopens included. The passive code never loads the PCAN transmit function, and a structural test reads the source to make sure it stays that way.

While it records, Lasto reads the setting again every {{ facts.status_interval|num }} seconds, and right away whenever the driver reports the adapter rejoining the bus. If it ever reads anything but on, or the adapter fails, Lasto stops trusting the channel. It logs why and closes the channel, which throws away anything the driver restarted on its own. Then it runs the whole sequence above again, up to {{ facts.reopen_attempts }} tries, and at most {{ facts.max_reopens }} reopens in one capture. If the channel can't be closed, or listen-only can't be confirmed again, the capture ends and the log says why. [A timing diagram](/safety/core/#on-the-wire) shows what listen-only means for one frame, bit by bit.

At the truck, in Phase 2's tests with PCAN-Basic 5.1.0.1194, listen-only read back on and stayed on in every run, with no error frames or overruns. [Passive capture](/docs/passive-capture/) describes a capture from start to finish.

One thing PEAK doesn't promise in writing is that the adapter spends no time at all in normal mode as it joins the bus; its documentation says only that listen-only applies "as fast as possible." The truck tests can't show that. A bench test, with the PCAN-USB and the MX+ testing each other, can. It was optional for Phase 2's passive tests, and it's still to come.

::: side
Enforced in {{ code_file("pcan_passive.py") }}, {{ code_file("reader.py") }}, and {{ code_file("session.py") }}.
:::

## Polled mode asks read-only questions {#polled}

Polled mode is planned for Phase 4. Every request will go through these checks in order, and stop at the first one that fails:

1. The session is still open, and the kill switch hasn't tripped.
2. The session has listened for {{ facts.listen_window|num }} seconds first, so it knows no other scan tool is talking. On real hardware that can't be skipped or shortened.
3. It's a typed request built by Lasto's own request functions, and no earlier request is still waiting for an answer.
4. It's addressed to a module on the approved list, or to `0x{{ facts.functional_id|hex3 }}`.
5. The interlocks allow it: parked-only work needs 0 mph, the battery guard has to be satisfied, and a logging request has to be in the profile picked when the session started.
6. It fits in one CAN frame. Lasto never sends multi-frame requests.
7. The encoded bytes pass the frame check: 8 bytes, an ID of `0x{{ facts.functional_id|hex3 }}` or an approved module and never one on the gate's list of the truck's own broadcast IDs (Phase 4 adds the 17 Phase 2 found), and a service on the allowlist and off the never-list, within the limits for each kind of module below.
8. Its turn under the rate limit comes within {{ facts.max_rate_wait|num }} {{ "second" if facts.max_rate_wait == 1 else "seconds" }}, or it's refused. While it waits, Lasto keeps reading the bus, and stops at once if the kill switch trips.
9. The interlocks still allow it, since the readings may have aged while it waited.

Then the gate checks the bytes and the kill switch once more, and hands the frame to [the one write function](/safety/core/#writer), which checks it all again. Nothing else can send it. [Flow-control frames](/safety/core/#flow-control), which let a module finish a long answer, go out only to answer one.

::: side
Enforced in {{ code_file("gate.py") }}.
:::

## Allowed and never-allowed requests {#services}

These tables come from the safety core's source when the site is built. Only the OBD-II services can go to `0x{{ facts.functional_id|hex3 }}`. Manufacturer services only ever go to one approved module at a time.

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

When Lasto starts, those addresses are copied into a fixed route table. Every module address it sends to comes from that table, and the only other one it uses is `0x{{ facts.functional_id|hex3 }}`. A copy or a look-alike of an entry has no route, so it can't send anywhere.

Standard OBD-II requests to `0x{{ facts.functional_id|hex3 }}` reach every module that answers OBD-II, which is how every generic scan tool works. Airbag and immobilizer modules only ever get asked for trouble codes, and they're left out of every identifier sweep.

::: side
Enforced in {{ code_link("APPROVED_ECUS", "ecus.py") }}.
:::

## Rate limits and the kill switch {#kill-switch}

Logging requests will go out at up to {{ facts.rates.logging|num }} per second. Snapshots get {{ facts.rates.snapshot|num }} per second, identification {{ facts.rates.identify|num }}, and discovery {{ facts.rates.discovery|num }}, and the speed, RPM, and voltage checks for the interlocks get {{ facts.rates.interlock_probe|num }}. A hard ceiling of {{ facts.ceiling|num }} per second sits under all of them together, and the write function enforces it again on its own.

If a module says it's busy (NRC `0x21`), Lasto backs off, starting at {{ facts.backoff_start|num }} seconds and doubling up to {{ facts.backoff_max|num }}. If it says it's still working (NRC `0x78`), Lasto waits up to {{ facts.response_pending_wait|num }} seconds without asking again, and gives up after {{ facts.response_pending_max }} of those in a row.

During a polled session, the kill switch stops all sending the moment any of these happens:

- a CAN error frame, or the adapter going error-passive or bus-off
- a receive overrun, where frames were lost because the queue filled up
- an interface error, or the adapter coming unplugged
- a frame on one of Lasto's request addresses that Lasto didn't send, meaning another scan tool is talking (two testers at once can confuse a module)
- an answer to a question it didn't ask, a malformed answer, or a module asking Lasto for flow control
- {{ facts.nrc_consecutive }} negative answers in a row to the same request, or {{ facts.nrc_window_limit }} in any {{ facts.nrc_window_seconds|num }} seconds (discovery counts "not supported" answers against a separate, larger limit)
- {{ facts.timeout_limit }} timeouts in a row
- an error in anything that reads the incoming frames
- you pressing {{ facts.hotkey }}

A request that fails a check is refused and logged, and the session carries on. A kill is different. There's one kill switch for the whole program, and once it trips it stays tripped until Lasto restarts. Nothing retries, no polled session can open again, and the trip is logged in every open audit log, or held for the next one to open. The polled session switches the adapter back to listen-only and checks that it took, or closes the channel. Saving and reporting on a kill come with Phase 4, through the stop path passive capture already uses. Passive capture has no kill switch, because it can't send. Error frames and bus faults don't stop it, and an adapter failure gets the reopen [described above](#listen-only).

::: side
{{ facts.hotkey }} works even when the terminal isn't focused.

Enforced in {{ code_link("PURPOSE_RATES", "ratelimit.py") }} and {{ code_link("CONSECUTIVE_NRC_LIMIT", "killswitch.py") }}, with the triggers in {{ code_file("reader.py") }}, the gate, and the hotkey.
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

## How the code holds to these rules {#enforced}

[How the safety core enforces the rules](/safety/core/) covers the machinery:

- [Real hardware only when you ask](/safety/core/#simulator): every command uses the simulator unless you type `--live` and the adapter's channel or port.
- [One write function](/safety/core/#writer) checks every frame again, and holds all requests to {{ facts.ceiling_frames }} in any {{ facts.ceiling_window|num }}-second window.
- [The rules are sealed](/safety/core/#frozen) against changes while Lasto runs, and the safety core keeps its own clock.
- [The serial guard](/safety/core/#serial-guard), installed before every command but the GUI, refuses opening a COM or AUX port by name with Python's file functions. It's built and tested. Its independent review has to happen before any MX+ code touches hardware.
- [The audit log](/safety/core/#audit) gets every frame before it's sent, and every refusal where it happens, held for the next log if none is open. On real hardware a session refuses any log but the kind that syncs every record to disk, checked by type for now.
- [Passive capture](/safety/core/#passive-capture) stops cleanly if its database or raw files can't be written, and its data folder refuses names that point at a device.
- [The tests](/safety/core/#tests): a simulated truck fails the run on a denied service, a frame on an ID Lasto may not use, or any frame while it only listens. Other tests cover timing and the parked-only rules.

## What the design does and doesn't claim {#threat-model}

The safety core is built to stop mistakes and shortcuts in Lasto's own code: a bug, a careless change, or code that a future Claude Code session writes, trying to send something it shouldn't or to skip a check. The checks refuse and log those when they run, and the tests are built to catch them. Gaps that reviews find are listed as open items, each scheduled for a later phase or accepted as low risk under this model.

It doesn't claim to stop code running inside Lasto that sets out on purpose to get around it. Python can't prevent that, since code in the same program can reach anything. What the design does instead is make the usual routes stand out: the structural tests look for a long list of known techniques, and the suite fails if they find one outside a short list of reviewed exceptions. Reviews of the safety core rate their findings against this model.

In practice, the rules protect you from Lasto's own code going wrong. They can't protect you from a modified copy of Lasto, and other programs on the laptop, like PCAN-View, don't follow Lasto's rules at all.

## What isn't proven yet {#not-proven}

- Only passive capture has run at the truck. Polled mode and the MX+ haven't, and every test of them so far runs against the simulator.
- The listen-only and silent-mode bench tests are still to come. There's no bench bus yet.
- The serial guard hasn't had its independent review. It comes before any MX+ code touches hardware, in Phase 3.
- The engine computer's address comes from the OBD-II standard and the service manual. No Creader capture has confirmed it yet.
- What each of the truck's broadcast IDs carries is a hypothesis until Phases 3 and 5, and conditions Phase 2 didn't test, like 4WD low, may add IDs ([the list](/docs/passive-capture/#bus)).
- The padding byte for requests is `0x{{ facts.padding_byte|hex2 }}` until a Creader capture confirms what Toyota uses.
- The {{ facts.hotkey }} hotkey is built and tested with fakes. It gets wired into real sessions in Phase 4.

Items come off this list as phases close them.

## Reporting a security problem {#report}

If you find a way to get Lasto to send something it shouldn't, please report it privately through [GitHub's private vulnerability reporting]({{ site.security_contact }}) instead of a public issue. The [security policy]({{ site.security_policy }}) has the details.

## No warranty {#warranty}

Lasto is free software under {{ site.license }} and comes with no warranty, to the extent the law allows. You use it on your own vehicle at your own risk.
