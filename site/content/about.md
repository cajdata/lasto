---
stamp: info
order: 4
title: "About Lasto and its name | Lasto"
h1: About Lasto
description: "Who builds Lasto and why, what the name means, the GPL-3.0 license and warranty terms, trademark notes, privacy, and how to get in touch."
lede: "Lasto is a one-person project, built for one truck first. This page covers the name, the license, the fine print, and how to reach me."
page_type: AboutPage
---

## The name {#name}

{{ site.name_note }}

## Who builds it {#who}

I'm [Chris Johnson]({{ site.author_url }}), and I'm building Lasto for my own 2006 GX470. The first thing I want it to answer is how hot the transmission gets on long grades, and whether a regear is worth doing.

The [source code is on GitHub]({{ site.repo }}). The [lasto package on PyPI]({{ site.pypi }}) only reserves the name for now.

## License and warranty {#license}

Lasto is free software under [{{ site.license }}]({{ site.license_file }}). There's no paid version.

Lasto is read-only by design. Passive mode sends nothing, and polled mode will send only read-only diagnostic requests, through the rules on the [Safety page](/safety/). It comes with no warranty, to the extent the law allows. You use it on your own vehicle at your own risk. Plugging anything into the OBD-II port carries some risk of its own, so route cables away from the pedals and don't operate a laptop while driving.

## Trademarks {#trademarks}

{{ site.trademarks[:-1]|join(", ") }}, and {{ site.trademarks[-1] }} are trademarks of their respective owners. Lasto isn't affiliated with or endorsed by any of them, and uses their names only to say what it's built to work with. Other product names belong to their owners.

## Privacy {#privacy}

This site has no analytics and no cookies, and it loads nothing from other servers. GitHub Pages hosts it and logs visitor IP addresses for security, under GitHub's privacy statement. Lasto itself has no networking code and is built to run offline in the truck.

## Contact {#contact}

For bugs and corrections, [open an issue on GitHub]({{ site.repo }}/issues). For security problems, use [private vulnerability reporting]({{ site.security_contact }}) instead of a public issue.

::: side
The site's source lives in the same repository as the app, under `site/`. Its fonts are Archivo and Fragment Mono, both under the SIL Open Font License.
:::
