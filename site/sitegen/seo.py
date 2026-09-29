"""Structured data and the site-wide files: sitemap.xml, robots.txt, llms.txt,
llms-full.txt, and /.well-known/security.txt."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape as xml_escape

if TYPE_CHECKING:
    from sitegen.pages import Page


def jsonld(page: "Page", ctx: dict) -> str:
    """JSON-LD for a page, as a <script> body. Only types the page's front matter asks for."""
    site = ctx["site"]
    base = site["base_url"]
    person = {"@type": "Person", "@id": f"{base}/#author", "name": site["author"], "url": site["author_url"], "sameAs": [site["author_url"]]}
    website = {
        "@type": "WebSite",
        "@id": f"{base}/#website",
        "url": f"{base}/",
        "name": site["name"],
        "description": site["summary"],
        "inLanguage": site["language"],
        "publisher": {"@id": f"{base}/#author"},
    }
    app = {
        "@type": "SoftwareApplication",
        "@id": f"{base}/#app",
        "name": site["name"],
        "description": ctx["roadmap_summary"],
        "applicationCategory": "UtilitiesApplication",
        "operatingSystem": "Windows",
        "isAccessibleForFree": True,
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
        "license": site["license_url"],
        "url": f"{base}/",
        "sameAs": [site["repo"], site["pypi"]],
        "author": {"@id": f"{base}/#author"},
    }
    source = {
        "@type": "SoftwareSourceCode",
        "@id": f"{base}/#source",
        "name": f"{site['name']} source code",
        "codeRepository": site["repo"],
        "programmingLanguage": {"@type": "ComputerLanguage", "name": "Python"},
        "runtimePlatform": ctx["python_req"],
        "license": site["license_url"],
        "targetProduct": {"@id": f"{base}/#app"},
        "author": {"@id": f"{base}/#author"},
    }
    this_page = {
        "@type": page.meta.get("page_type", "WebPage"),
        "@id": f"{page.canonical}#page",
        "url": page.canonical,
        "name": page.title,
        "headline": page.h1,
        "description": page.description,
        "inLanguage": site["language"],
        "isPartOf": {"@id": f"{base}/#website"},
        "author": {"@id": f"{base}/#author"},
        "dateModified": page.modified.isoformat(),
        "image": page.og_image_url,
    }
    if page.meta.get("about_app"):
        this_page["about"] = {"@id": f"{base}/#app"}
    graph = [this_page]
    types = set(page.meta.get("jsonld", []))
    if "WebSite" in types:
        graph.insert(0, website)
    if "SoftwareApplication" in types:
        graph.append(app)
    if "SoftwareSourceCode" in types:
        graph.append(source)
    graph.append(person)
    if page.crumbs:
        graph.append(
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": i, "name": name, "item": f"{base}{url}"}
                    for i, (name, url) in enumerate(page.crumbs, 1)
                ],
            }
        )
    data = {"@context": "https://schema.org", "@graph": graph}
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def sitemap(pages: list["Page"], base: str) -> str:
    rows = []
    for p in sorted(pages, key=lambda p: p.url):
        if not p.indexable:
            continue
        rows.append(f"<url><loc>{xml_escape(base + p.url)}</loc><lastmod>{p.modified.isoformat()}</lastmod></url>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(rows)
        + "\n</urlset>\n"
    )


def robots(base: str) -> str:
    return (
        "# Search engines and AI crawlers are all welcome.\n"
        "User-agent: *\nAllow: /\n\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )


def llms_txt(pages: list["Page"], ctx: dict) -> str:
    site = ctx["site"]
    base = site["base_url"]
    lines = [
        f"# {site['name']}",
        "",
        f"> {ctx['roadmap_summary']}",
        "",
        f"Built by {site['author']}. {ctx['status_sentence']}",
        "",
        "Lasto is free software under GPL-3.0-or-later, with no warranty. It's built for one truck first: a 2006 Lexus GX470 "
        "(2UZ-FE V8, A750F automatic, KDSS). Passive mode, arriving in Phase 2, opens a PEAK PCAN-USB interface in hardware "
        "listen-only mode, confirms it, keeps checking it while it records, and sends nothing. Polled mode, arriving in "
        "Phase 4, will send only read-only diagnostic requests through allowlists, rate limits, and a kill switch, and one "
        "write function checks every frame again before it goes out. Nothing runs at the truck until those phases ship. "
        "Each page below is also available as Markdown.",
        "",
        "## Pages",
        "",
    ]
    for p in sorted((p for p in pages if p.indexable), key=lambda p: p.meta.get("order", 50)):
        lines.append(f"- [{p.h1}]({base}{p.mirror_url}): {p.description}")
    lines += [
        "",
        "## Optional",
        "",
        f"- [Source code on GitHub]({site['repo']}): the app, its safety core, and this site's source.",
        f"- [All pages in one file]({base}/llms-full.txt): every page above as Markdown.",
        f"- [Security policy]({site['security_policy']}): how to report a problem privately.",
        "",
    ]
    return "\n".join(lines)


def llms_full(pages: list["Page"], ctx: dict) -> str:
    site = ctx["site"]
    parts = [f"# {site['name']}\n\n> {ctx['roadmap_summary']}\n\nBuilt by {site['author']}. {ctx['status_sentence']}\n"]
    for p in sorted((p for p in pages if p.indexable), key=lambda p: p.meta.get("order", 50)):
        parts.append(p.mirror_text.rstrip() + "\n")
    return "\n---\n\n".join(parts)


def security_txt(site: dict, built: dt.datetime) -> str:
    expires = (built.astimezone(dt.timezone.utc) + dt.timedelta(days=330)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        f"Contact: {site['security_contact']}\n"
        f"Expires: {expires.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"Preferred-Languages: en\n"
        f"Canonical: {site['base_url']}/.well-known/security.txt\n"
        f"Policy: {site['security_policy']}\n"
    )
