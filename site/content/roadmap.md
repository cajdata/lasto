---
stamp: status
order: 3
toc: true
numbered: false
title: "Roadmap: what's built and what isn't | Lasto"
h1: "Roadmap: what's built and what isn't"
description: "Lasto is built in nine phases, and each one is reviewed before the next starts. See which phases are done, and which one first sends anything to the truck."
lede: "Lasto is pre-alpha. It's built in nine phases, and each one stops for my review before the next starts. A feature shows up in the docs only after its phase is signed off."
page_type: WebPage
---

## At a glance {#summary}

{{ status_sentence }}

Table: Phases and what they send

| Phase | What it builds | Status | Sends anything on the bus? |
|---|---|---|---|
{% for p in roadmap.phases %}| [Phase {{ p.number }}](#{{ p.anchor }}) | {{ p.name }} | {{ p.status_label }} | {{ p.transmits }} |
{% endfor %}

::: side
Phase 4 is the first time Lasto sends anything to the truck. Everything it sends goes through the rules on the [Safety page](/safety/).

There are no target dates. Most phases end with a test at the truck, and the truck doesn't care about my calendar.
:::

{% for p in roadmap.phases %}
## Phase {{ p.number }}: {{ p.name }} {#{{ p.anchor }}}

**{{ p.status_label }}.** {{ p.summary }}

{% for d in p.delivers %}
- {{ d }}
{% endfor %}

::: side
Table: Phase {{ p.number }} at a glance

| Item | Detail |
|---|---|
| Status | {{ p.status_label }} |
| Sends anything? | {{ p.transmits }} |
| Commands | {% if p.commands %}{% for c in p.commands %}`{{ c }}`{% if not loop.last %}, {% endif %}{% endfor %}{% else %}None{% endif %} |
| Test at the truck | {{ p.live_test or "Set when the phase starts." }} |
{% set docs = roadmap.reserved|selectattr("phase", "equalto", p.number)|list %}{% if docs %}| Docs it adds | {% for r in docs %}{% if loop.first %}{{ r.title }}{% else %}{{ r.title[:1]|lower }}{{ r.title[1:] }}{% endif %}{% if not loop.last %}{{ " and " if loop.revindex == 2 else ", " }}{% endif %}{% endfor %} |
{% endif %}
{% for o in p.open %}

**Open:** {{ o }}
{% endfor %}
{% if p.site_note %}

{{ p.site_note }}
{% endif %}

:::

{% endfor %}

## Not on the roadmap {#not-planned}

- Other vehicles, for now. The GX470 comes first.
- Mac and Linux. Lasto loads PEAK's Windows driver directly and uses Windows APIs for the {{ facts.hotkey }} hotkey.
- Anything that writes to the truck, ever. That's what the scan tool is for.

## Following along {#follow}

This page is generated from one data file in the repository every time the site is built, and it updates when a phase is signed off. To follow along, watch the repository on GitHub.
