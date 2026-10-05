---
stamp: status
order: 2.6
numbered: false
title: "Documentation: what's built so far | Lasto"
h1: Documentation
description: "Lasto's docs, published as each phase ships: passive capture first, then mapping, polled reads, decoding, and the rest, with where each phase stands."
lede: "Docs appear as their phases ship, and describe only what's built and approved. Until then, the roadmap covers what each phase will do."
---

## Published {#published}

{% for d in docs %}
- [{{ d.h1 }}]({{ d.url }}): {{ d.description }}
{% endfor %}

## Coming with later phases {#planned}

Table: Docs waiting on their phase

| Doc | Arrives with |
|---|---|
{% for r in roadmap.reserved if r.phase is not none and not roadmap.phase(r.phase).done %}| {{ r.title }} | [Phase {{ r.phase }}](/roadmap/#{{ roadmap.phase(r.phase).anchor }}), {{ roadmap.phase(r.phase).status_label|lower }} |
{% endfor %}

## Also on this site {#reference}

- [What Lasto will and won't send](/safety/), and [how the safety core enforces it](/safety/core/).
- [The roadmap](/roadmap/): every phase and where it stands.
