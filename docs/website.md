# lasto.dev website plan

Status: **W0 approved 2026-09-27. W1 built 2026-09-27, waiting on approval.** The decisions below override anything later in this document. The brief is `lasto-website-prompt.md`. How to build and edit the site is in `site/README.md`; the going-live steps are in section 10.

## 0. Decisions (W0, 2026-09-27)

- **Direction:** Datasheet. The service map (Fig. 1) is generated at build time from the safety core's allowlist and never hand-coded, so it can't drift from what the app actually sends.
- **Security contact:** GitHub private vulnerability reporting, now enabled on cajdata/lasto.
- **Deploy order:** Phase 1 gets reviewed, approved, and pushed before the site deploys.
- **Trademarks:** Subaru is dropped from the non-affiliation note until Subaru support appears on the site.
- **Analytics:** none. Search Console, Bing Webmaster Tools, and GitHub's traffic page are enough.
- **DNS:** the wildcard `*.lasto.dev` record is deleted. W1's steps verify the domain on the GitHub account before the custom domain is set on the repo.
- **Fonts:** confirm each license allows self-hosting and redistribution in a public repo, and commit the license files next to the fonts.
- **Everything else in section 9:** the defaults stand. Question 13 (whether About says anything about how the copy was drafted) had no default and stays open, so About says nothing about it for now.

The short version of the original proposal:

- **Stack:** a small Python build (Jinja2 and markdown-it-py), about 900 lines, deployed to GitHub Pages by one Actions workflow. No Node in the repo.
- **Navigation:** the wordmark (home), Safety, Docs, Roadmap, GitHub. Docs appears in W2.
- **Launch pages (W1):** Home, Safety, Roadmap, About (the name note is its first section), and 404.
- **Three visual directions** with mocks you can open locally: Datasheet, Bus Transcript, and Shop Labels. See section 7.
- **Questions:** section 9 lists them, grouped, each with a default, so you can answer inline.

## 1. Stack

**Recommendation:** a custom Python build, not a static site generator.

Most of what makes this site specific is glue: a hand-built design with full control of markup and CSS, a Markdown mirror of every page, llms.txt and llms-full.txt, per-page social images, sitemap dates from git, pages generated from the roadmap data, the vehicle definitions and GitHub releases, and a 100 KB page budget. Each piece is 30 to 150 lines of Python. No generator does all of it without a docs theme to fight or a second template language. A build around Jinja2 and markdown-it-py keeps the project in one language, installs as wheels on Windows and ubuntu with no system libraries, and can enforce the brief at build time the same way the safety core enforces its rules: page budget, one H1, no em dashes, no third-party requests, and no doc page published before its phase is done.

The generator landscape is a poor bet right now. MkDocs 1.x hasn't had a release since August 2024, Material for MkDocs has been in maintenance mode since November 2025 (critical fixes end 2027-05-05), and its successor Zensical is alpha (0.0.65) with social cards and git dates still unfinished. Hugo is the strongest alternative and the runner-up, but it adds Go templates and a pinned binary on the desktop, the truck laptop, and CI. The content (CommonMark with YAML front matter) moves to any generator unchanged if the docs ever need search or versioning.

| Package | Version | Use |
|---|---|---|
| Jinja2 | 3.1.6 | HTML, Markdown, SVG, and text templates |
| markdown-it-py + mdit-py-plugins | 4.2.0 + 0.6.1 | CommonMark (renders the same as GitHub), tables, footnotes, stable heading anchors |
| PyYAML | 6.0.3 | front matter and the repo's `definitions/` files (same pin the app adopts in Phase 3) |
| tomllib | stdlib | `site.toml` and `roadmap.toml` |
| fontTools + Brotli | 4.66.0 + 1.2.0 | subset the OFL fonts to WOFF2 |
| resvg-py | 0.5.0 | render per-page 1200 x 630 social images and the 1280 x 640 GitHub preview from an SVG template, with the site's own fonts |

They go in a `site` dependency group in `pyproject.toml`, so none of them become lasto runtime dependencies or ship to PyPI. Pillow comes later, only for photo derivatives.

**Layout:**

```
site/
  build.py        build [--strict] [--drafts] | serve [--watch] | check
  sitegen/        content, pages, data, seo, assets, og, checks, serve
  content/        Markdown with front matter; the file path is the URL
  data/           site.toml, roadmap.toml, hardware.toml, vehicles/lexus-gx470.toml
  templates/      base, home, page, doc, roadmap, vehicle, changelog, 404, partials, md/*.md.j2
  static/         css/, img/ (logo, favicon, diagrams), fonts/src/*.ttf + OFL.txt
  tests/          own pytest config; never loosens the app's coverage gate
  dist/ .cache/   build output and cache (already gitignored)
```

**What a build produces:** every page at `dir/index.html` with a Markdown mirror at `dir/index.md` and `dir.md`; `404.html`; `sitemap.xml` with `lastmod` from git; `robots.txt`; `llms.txt` and `llms-full.txt`; `/.well-known/security.txt`; social images; subset fonts. Preview locally with `python site/build.py serve --watch`, which mimics GitHub Pages (content types, 404 status). No live-reload script: it would add JavaScript, and the guardrails deny any command containing `--live` anyway.

**Checks on every build:** one H1 and heading order; unique titles and descriptions; canonical URLs; internal links and anchors (mirrors too); the page budget; no third-party requests; font coverage; copy lint (em dashes, en dashes, the banned hype words); the honesty rule (no page for an unfinished phase); JSON-LD shape; sitemap matches the page set; security.txt `Expires` in range.

**Deploy:** `.github/workflows/site.yml`, every action pinned by commit SHA, running on `ubuntu-24.04` (`ubuntu-latest` moves to 26.04 in October). It builds with `--strict`, checks internal links offline with lychee, uploads with `include-hidden-files: true` (otherwise `/.well-known/` is silently dropped), and deploys from `main` only. Pull requests build but never deploy. A monthly scheduled run refreshes security.txt `Expires` and the changelog. Release events re-dispatch the workflow on `main`, because the Pages environment rejects tag refs. `concurrency` sits on the deploy job, so a PR build can't cancel a pending deploy. The push trigger includes `src/lasto/**`, because the Safety page reads its numbers from the source (section 8).

**W3 checks without Node in the repo:** Lighthouse 13.5.0 through a one-off `npx` step in CI, the Nu HTML validator from a pinned Docker image, lychee for external links, PageSpeed Insights against the deployed site as the score of record, and the Schema.org validator and Rich Results Test by hand. Expect Google to call SoftwareApplication "not eligible" for rich results, since it wants ratings or reviews and we won't invent any. The markup is still valid.

**Repo fixes that land first in W1 (not safety core):**

1. `.gitignore` line 168 is `/site`, a MkDocs leftover from GitHub's Python template. It ignores the whole `site/` folder, so it goes.
2. `pyproject.toml` gets `[tool.hatch.build.targets.sdist] exclude = ["/site"]`, or fonts and site sources would ship in the lasto sdist.

## 2. Navigation and URLs

| Nav item | Target | Notes |
|---|---|---|
| wordmark | `/` | accessible name "Lasto, home" |
| Safety | `/safety/` | first text link; it's the trust page |
| Docs | `/docs/` | added in W2; an empty docs index at launch would be a thin page |
| Roadmap | `/roadmap/` | every link to an unpublished doc falls back to its phase here |
| GitHub | `github.com/cajdata/lasto` | outbound marker, same tab |

The footer on every page carries: About and the name note, Vehicles (W2), Changelog (once a release exists), license, security contact, "View as Markdown", "Last updated", the short liability line, and the trademark note.

URLs are lowercase directories with a trailing slash. Every H2 gets a fixed id written in the source, so rewording a heading never breaks an inbound link (`/safety/#listen-only`, `/roadmap/#phase-4`). Feature doc slugs are reserved now so no URL ever has to move: `/docs/passive-capture/` (Phase 2), `/docs/mapping/` (3), `/docs/polled-reads/` and `/docs/logging-profiles/` (4), `/docs/broadcast-decoding/` (5), `/docs/discovery/` (6), `/docs/k-line/` (7), `/docs/reports-and-exports/` (8). Vehicle pages live at `/vehicles/lexus-gx470/`, outside `/docs/`, since they're generated from data and are the pages forums will link to.

## 3. Pages

| Page | Ships | H1 | Status label |
|---|---|---|---|
| `/` | W1 | A read-only data logger for a 2006 Lexus GX470 | from roadmap.toml |
| `/safety/` | W1 | What Lasto will and won't send to your truck | built and tested against the simulator; not run at the truck |
| `/roadmap/` | W1 | Roadmap: what's built and what isn't | generated |
| `/about/` (name note at `#name`) | W1 | About Lasto | none |
| `/404.html` | W1 | Nothing answered at this address | none |
| `/docs/` | W2 | Documentation | pages appear as phases ship |
| `/docs/getting-started/` | W2 | Getting started with Lasto | nothing runs at the truck yet |
| `/docs/hardware/` | W2 | The hardware Lasto is built around | planned setup, untested at the truck |
| `/docs/faq/` | W2 | Frequently asked questions | status sentences generated |
| `/docs/contributing/` | W2 | Contributing | per your answer on contributions |
| `/vehicles/` and `/vehicles/lexus-gx470/` | W2 | Lexus GX470: what's on the diagnostic bus | research notes, unverified |
| `/changelog/` | W2 | Changelog | noindex until the first release |
| `/docs/passive-capture/` and later feature docs | when their phase is done | | drafted early, published by the roadmap |

A standalone name page would be one line, which search engines treat as thin content. So the name note is the first section of About, which also holds the full license, trademark, and privacy notes that every footer points to.

## 4. Launch page outlines (W1)

Sample lines are drafts in your voice; you'll see all of them in W1.

**Home (`/`).** Title: "Lasto: read-only data logger for a 2006 Lexus GX470".
- Lead: the H1, the one-sentence description, a generated status line, and one clear next step ("Read what it will never send to the truck", linking Safety), with the roadmap and GitHub as quieter links. No two-button hero.
- How it connects: the hardware chain as an inline SVG in the chosen style, then the same chain as an ordered list so every fact is also text. Ends with "The planned setup. None of it has run at the truck yet."
- Two modes: passive (listen-only, readback, refuse otherwise) and polled (Phase 4: read-only requests plus flow control, through the rules). Labeled example frames.
- What it will never do: five plain bullets, then "No setting turns these on. That's your scan tool's job."
- Where it stands: phases 0 to 8 from roadmap.toml, status as words, never color alone.
- JSON-LD: WebSite, Person (Chris Johnson, linked to github.com/cajdata), SoftwareApplication (free, Windows, GPL-3.0-or-later, GitHub and PyPI; no version or download URL until a real release), SoftwareSourceCode.

**Safety (`/safety/`).** The trust page: every rule, the file that enforces it, and the test that checks it.
- The short version (six bullets a skimmer can stop at).
- Can a data logger damage my truck? Answer first: a controller in listen-only mode can't disturb the bus with anything it sends. Then the physical risks software can't stop: bent pins, a miswired splitter, a cable near the pedals, battery draw on pin 16.
- Listening mode sends nothing: the open sequence (channel free, listen-only on, connect at 500 kbps, read it back, refuse otherwise), no fallback, the structural test, and the caveat that PEAK doesn't promise in writing and the bench test hasn't run.
- Polled mode asks read-only questions: the ordered checks, including the flow-control frame (sent only to the approved physical request ID, once per first frame, audited like a request).
- Allowed and never-allowed requests: a table generated from the source (7 OBD-II and 7 manufacturer read services allowed, 22 on the never-list), captioned "Today's allowlist. Phase 3 captures may trim it."
- Which modules it will talk to (today only 0x7E0/0x7E8), rate limits and the kill switch (3 negative answers in a row to the same request, or 10 in any 30 seconds), parked-only work and driving, the battery guard (12.0 V), the OBDLink MX+ command allowlist, simulator by default, the audit log, how the tests check the rules ("enforced by the test command", never a result nobody ran), what isn't proven yet, reporting a security problem, no warranty.
- JSON-LD: TechArticle.

**Roadmap (`/roadmap/`).** Generated from `site/data/roadmap.toml`.
- Lead: pre-alpha, nine phases, each reviewed before the next starts; a feature shows up in the docs only after its phase is signed off.
- At a glance: Phase, what it builds, status, and "Sends anything on the bus?" (No for 0 to 3, 5 and 8; read-only requests for 4; read-only requests, parked only, for 6; decided in the phase for 7).
- One section per phase with status and date, summary, deliverables, commands, the test at the truck, open decisions, and the docs it adds.
- Not on the roadmap: other vehicles for now, Mac and Linux, anything that writes to the truck. No target dates.

**About (`/about/`).** The name (one line), who builds it (a draft line about transmission temperature on long grades and whether a regear is worth it, for you to confirm), license and warranty, trademarks, privacy (no analytics, no cookies, nothing loaded from other servers; GitHub Pages logs IPs under GitHub's privacy statement), contact.

**404.** "There's no page here. If you followed a link to a doc, it may be for a feature that hasn't been built yet." A static list of reserved doc slugs and the phase each arrives with, links home, Safety, Roadmap, GitHub, and a link to report a broken link. No JavaScript, noindex, not in the sitemap.

**Roadmap data file.** TOML, one `[[phase]]` per phase: `number`, `slug`, `name`, `status` (`done`, `awaiting-approval`, `in-progress`, `planned`), `built` and `approved` dates, `public` (commits are on GitHub `main`), `transmits`, `summary`, `delivers`, `commands` (checked against `cli.py`), `live_test`, `open`, `publishes` (the docs that go live when the phase is done). Today: Phase 0 done, Phase 1 awaiting sign-off, 2 to 8 planned. Promoting a phase's docs is one commit to this file.

## 5. Docs (W2) and how gating works

Getting started, hardware, FAQ, contributing, the vehicles index and GX470 page, and the changelog ship in W2. Feature docs are drafted ahead of time but not published until the roadmap marks their phase done:

1. Front matter carries `requires_phase`. A page whose phase isn't done produces nothing: no HTML, no mirror, no sitemap or llms.txt entry. Sections inside a page can be gated the same way.
2. Internal links use a `doc:` scheme. A link to an unpublished doc resolves to `/roadmap/#phase-N` with "(planned, Phase N)" appended. An unknown slug fails the build.
3. Status labels are generated, never hand-typed, and repeated as the first sentence of the lead so snippets carry them.
4. `python site/build.py --drafts` previews gated pages locally with a "DRAFT, not published" banner. CI never passes `--drafts`.
5. The build fails if a published page shows a `--live` command before Phase 2 is done, links code from an unpushed phase, or disagrees with `cli.py` or the safety source.

The GX470 page answers the search questions honestly now (DLC3 pins, what's on the CAN bus, ATF temperature, KDSS) as labeled research notes, and its signals table fills in from `definitions/` after Phase 3.

## 6. Search and AI discoverability

- Every page: unique title and meta description, one H1, canonical URL, Open Graph and Twitter tags, a per-page social image, a visible byline and "Last updated" from git that matches the sitemap and JSON-LD.
- JSON-LD: WebSite on home; SoftwareApplication and SoftwareSourceCode where relevant; BreadcrumbList on docs and vehicle pages, with a visible breadcrumb to match; no FAQ or HowTo markup.
- `robots.txt`: `User-agent: *`, `Allow: /`, and the Sitemap line. Per-agent lines aren't needed and become a maintenance trap. Allowing everything includes training crawlers (GPTBot, ClaudeBot, CCBot and so on), which the brief asks for.
- `llms.txt` follows the v2 proposal (revised 2026-08-10): H1, a blockquote summary, then link lists. `llms-full.txt` holds the published pages in the same order. Neither affects Google ranking, but both help agents and user-triggered fetchers.
- Markdown mirrors at `dir/index.md` (the proposal's form) and `dir.md` (the common guess), linked with `rel="alternate" type="text/markdown"`, kept out of the sitemap, and never blocked in robots.txt. GitHub Pages serves them as `text/markdown`.
- `/.well-known/security.txt`: Contact is GitHub private vulnerability reporting, `Expires` is the build date plus 330 days, plus Canonical, Policy (SECURITY.md), and Preferred-Languages. Unsigned for now. A matching SECURITY.md lands in the repo.
- GitHub Pages can't set response headers, so the CSP goes in a meta tag: `default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'none'`. That's why the mocks have no JavaScript, no `<style>` blocks, and no inline styles.
- Google doesn't use SVG favicons in search results, so the SVG mark ships with PNG and ICO companions.

## 7. Visual directions

Eight concepts were generated from different source material, scored by three judges (distinctiveness, subject fit and honesty, buildability), and cut to three that differ in source material, type, color, and layout. Service Manual scored second but read as a near-twin of Datasheet; its best ideas (Toyota's wire colors only where they're real, and the EWD part-tag conventions) were grafted into Shop Labels. ACK Slot was the most distinctive, but its signature collided with Bus Transcript's; its bit-exact timing diagram moves into Datasheet as a figure on the Safety page.

All three mocks use the same key copy, so you're comparing design rather than wording. Each has `index.html` (the top of the home page, plus the footer) and `docs.html` (a sample docs page, since the site grows into documentation). Open them straight from disk; there's no server and no JavaScript. Screenshots are in each folder's `screens/`.

Start at `w0-mocks/index.html`. The mocks are untracked and get deleted once W1 builds the real templates.

Each mock went through two independent reviews (one visual, one for copy, honesty, facts, and accessibility), then a fix pass and a fresh-eyes verification. All three pass the mechanical checker: no dashes, no scripts, no inline styles, no third-party requests, landmarks, one H1, a skip link, and HTML plus CSS under 100 KB. The mock fonts are unsubset Fontsource files, so the totals with fonts run over the budget until W1 subsets them.

### 7.1 Datasheet (`w0-mocks/datasheet/`)

- **Concept:** the site is written as Lasto's own chip data sheet: numbered clauses, figure and table numbers, footnoted sources, and a status stamp on every page with a plain gloss ("Objective data sheet: what Lasto is being built to do", "Research notes"). A page can't claim more than its stamp.
- **Type:** Archivo in two widths, normal for reading and condensed for labels, plus Fragment Mono (a monospaced Helvetica) for hex and frames.
- **Color:** black ink on white, with one red that only ever means "never". The dark theme, Graphite, uses tonal row bands where the light theme uses rules.
- **Layout:** clause numbers hang in the margin, then a reading column, then a side column with status, next step, the quick reference table, and notes.
- **Signature:** the service map. Every possible service byte as two 16 by 16 grids: passive mode 0 of 256, polled mode 14 of 256, and the 22 never-list bytes hatched red. The caption says it's today's allowlist and Phase 3 may trim it.
- **Tradeoffs:** the most rigorous, and the best long-docs reading of the three. The service map carries the safety argument by itself. The chip-industry status words only work if the gloss always travels with them. The heaviest fonts (115 KB before subsetting, about 35 to 47 KB after).

### 7.2 Bus Transcript (`w0-mocks/bus-transcript/`)

- **Concept:** every page is set as the log Lasto writes, on candump's columns (time, TX, RX, ID, DLC, data, note), with the writing in a wide decoded column.
- **Type:** two Monaspace faces on one character grid: Krypton for what the machine says, Argon for what a person reads, so decoding shows up as cells changing face. Monaspace's license reserves its names, so W1 ships renamed subsets ("Lasto Raw" and "Lasto Decoded").
- **Color:** cool printout paper with a pink highlighter that only means "decoded"; unknown bytes stay grey. Dark is a dim green-grey screen with lit and dim bytes.
- **Signature:** the TX column. It runs empty down every page in passive mode and ends in a 0: "0 frames is what passive mode is built to send on the CAN bus."
- **Tradeoffs:** the tightest idea, and it's literally what Lasto sees. Everything is monospace, docs prose included, so open `docs.html` and judge whether 3,000 words would read comfortably. Its dark theme is the closest of the three to the dark-plus-one-accent look the brief warns about.

### 7.3 Shop Labels (`w0-mocks/shop-labels/`)

- **Concept:** the site as a careful harness bench. Every identifier is a label on the thing it names, and the material says what it is: printed heat-shrink sleeves for permanent names, masking tape for anything provisional (status, example frames, hypotheses), yellow for safety. When a phase ships, its tape comes off.
- **Type:** Atkinson Hyperlegible Next for reading and Atkinson Hyperlegible Mono for labels, because a label can't let 0 pass for O or 8 for B.
- **Color:** cool bench grey with white sleeves; dark is black sleeves on rubber-mat charcoal. Toyota wire colors appear only on real factory circuits. Everything drawn in grey is a part you're adding.
- **Signature:** section headings printed on sleeves shrunk onto the truck's pink and violet CAN pair (DLC3 pins 6 and 14), running full width. The hardware chain is laid out like a harness on a layout board, with W1 to W4 cable tags and a DLC3 face drawing.
- **Tradeoffs:** the warmest, and the one that most looks like one person's truck project. The lightest fonts (43 KB before subsetting). Atkinson's slashed zeros show up in prose and headlines ("2006", "GX470"), which is the typeface doing its job but reads a little odd in an H1. It'll take discipline to keep it from drifting into kitsch as the site grows.

### 7.4 My lean

Datasheet. The design itself makes the safety case (the service map shows default deny at a glance), it reads best as long documentation, and the status stamps make the honesty rule visible on every page. Shop Labels is a close second if you'd rather the site feel like a garage project than a spec. It's your call, and another round is fine too.

## 8. Decisions I made (tell me if you disagree)

| Topic | Decision | Why |
|---|---|---|
| Name note URL | `/about/#name` | a one-line page is thin content |
| Output folder | `site/dist/` | already gitignored |
| Asset fingerprinting | none (social images get a content hash) | GitHub Pages caps caching at 10 minutes anyway |
| Social images | resvg-py | identical output on Windows and ubuntu; Pillow on Windows has no text shaping |
| Roadmap status values | `done`, `awaiting-approval`, `in-progress`, `planned`, plus `public` | "done" means you signed it off; only done publishes docs |
| Where Safety page numbers come from | the build parses the safety source files with `ast` and never imports them | always current, no change to the app's tests; the deploy workflow triggers on `src/lasto/**` |
| Changelog | W2 | per the brief |
| security.txt Expires | build date + 330 days, monthly rebuild | stays under a year with margin if the schedule lapses |
| Page budget | 100,000 bytes per page for HTML, CSS, and fonts | no JavaScript to count |
| JavaScript | none at launch | the 404 hints are a static list instead |
| Dates shown | your local commit date from git; sitemap uses the full timestamp with offset | one source, no UTC/Denver mismatch |
| Hardware facts | `site/data/hardware.toml` (adapters, chain) and `site/data/vehicles/lexus-gx470.toml` (pins, wires, nodes) | one source each for the diagram, tables, vehicle page, and llms.txt |
| Example frames | standard OBD-II frames, always labeled, until a scrubbed Phase 2 capture replaces them | no invented truck data |
| Unpushed commits | never linked or printed on the site | they lead nowhere on GitHub |
| Toyota documents | facts only (pins, wire colors, connector codes); every diagram drawn from scratch | they're copyrighted |

## 9. Questions

**To pick a direction**

1. Which direction: Datasheet, Bus Transcript, Shop Labels, or another round? Default: none, it's your call.

**Before W1**

2. Private vulnerability reporting is off on cajdata/lasto (the API returned `enabled: false`). Can you turn it on (Settings, Advanced Security, Private vulnerability reporting)? security.txt and every footer point there. Default: you enable it before the W1 deploy.
3. The website branch sits on the two unpushed Phase 1 commits, and Pages deploys only from `main`. Approve and push Phase 1 before the site deploys, or rebase the site onto `origin/main`? Default: push Phase 1 first, so the Safety page can link real code.
4. Name casing: "Lasto" in running text and `lasto` for the command, with the wordmark styled by the direction? Default: yes. The title, `og:site_name`, H1, and JSON-LD will all use "Lasto".
5. Subaru is in the brief's non-affiliation list, but nothing else mentions a Subaru. Keep it or drop it? Default: keep it, since the brief lists it.
6. Analytics: none, or GoatCounter's no-JavaScript pixel (cookieless, but a request to another server on every page view)? Search Console, Bing Webmaster Tools, and GitHub's traffic page cover queries and referrers with nothing on the site. Default: none.
7. Voice: first person ("my sign-off") on the roadmap and About, second person in the docs? Default: yes.
8. README, `pyproject.toml`, the GitHub repo description, and the PyPI summary say "Passive, read-only CAN bus logger for Toyota and Lexus trucks ... never writes to the vehicle." The site will say read-only, keep "passive" for passive mode, and name only the GX470. Want the app-side text aligned in a separate small commit? Default: yes, I propose it and you approve.
9. The About draft says the first thing you want answered is how hot the transmission gets on long grades and whether a regear is worth it. Accurate, and OK to say? Default: yes.
10. Repo settings you'd change yourself: homepage `https://lasto.dev`, a few topics (can-bus, obd2, lexus-gx470, python). Default: yes.
11. Domain hygiene: lasto.dev won't send mail, so add a null MX, `v=spf1 -all`, and a DMARC reject record with the W1 DNS changes? Default: yes.
12. Should `lasto-website-prompt.md` be gitignored like PROMPT.md? Default: yes.
13. Google's guidance asks whether automation used in writing is disclosed where readers would expect it. Do you want About to say anything about how the site copy was drafted? No default; your call either way.

**Later (W2 and after)**

14. Parts for the hardware page: is the OBD cable PEAK's PCAN-Cable OBD-2 (IPEK-003004), and what make and model is the splitter? Does it pass all 16 pins?
15. Next time you're at the truck, by eye only: which DLC3 cavities have terminals (the drawings assume 4, 5, 6, 7, 12, 13, 14, 16), and which ground pin (4, 5, or both) your cable uses?
16. Contributions: issues only for now, or pull requests too (and a DCO sign-off)?
17. Will each phase end with a GitHub release and tag? The changelog is built from releases.
18. A CI job that runs the simulator test suite on a Windows runner would let the Safety page show a real, dated result. Worth adding later? Default: later, after W1.
19. Photos: which spots matter most (the setup under the dash on Home, the parts on the hardware page)? The build will strip EXIF, and anything showing the VIN or plates gets cropped.
20. Can the FAQ mention that PEAK's free PCAN-View has a listen-only option and can record a trace in the meantime? Default: yes, one sentence, clearly labeled as PEAK's tool.

## 10. Going live (W1)

Everything here is yours to do. Steps 1 and 2 can happen now. Steps 3 to 6 wait until you've approved W1 and Phase 1 is approved and pushed.

### The DNS records

Checked 2026-09-27 against 1.1.1.1. The apex and `www` records are already in place, and the wildcard is gone.

| Type | Host | Answer | Status |
|---|---|---|---|
| A | (blank, the apex) | `185.199.108.153` | in place |
| A | (blank) | `185.199.109.153` | in place |
| A | (blank) | `185.199.110.153` | in place |
| A | (blank) | `185.199.111.153` | in place |
| AAAA | (blank) | `2606:50c0:8000::153` | in place |
| AAAA | (blank) | `2606:50c0:8001::153` | in place |
| AAAA | (blank) | `2606:50c0:8002::153` | in place |
| AAAA | (blank) | `2606:50c0:8003::153` | in place |
| CNAME | `www` | `cajdata.github.io` | in place |
| TXT | `_github-pages-challenge-cajdata` | the code GitHub shows you in step 1 | **missing** |

Keep nothing else at the apex (no ALIAS, no parking record) and no wildcard. If you ever add CAA records, one must allow `letsencrypt.org`.

Right now `http://lasto.dev` answers with GitHub's "Site not found". With the records pointing at GitHub and no verification, that's the takeover window: any GitHub account could attach lasto.dev to its own Pages site until step 1 is done.

### Step 1: verify the domain on your GitHub account (do this first, and soon)

1. On GitHub, open your profile menu, then **Settings**, then **Pages** (github.com/settings/pages).
2. Click **Add a domain**, enter `lasto.dev`, and click **Add domain**.
3. GitHub shows a TXT record. At Porkbun (Domain Management, lasto.dev, DNS), add it: Type `TXT`, Host `_github-pages-challenge-cajdata`, Answer the code GitHub showed you.
4. Wait a few minutes, then click **Verify**. Leave the TXT record in place for good; removing it undoes the protection.

Once verified, only repositories you own can publish to lasto.dev and its immediate subdomains, so a deleted repo or a disabled Pages site can't hand the domain to someone else.

### Step 2: mail records (optional, recommended)

lasto.dev won't send email, so tell receivers to reject anything claiming to come from it:

| Type | Host | Answer |
|---|---|---|
| TXT | (blank, the apex) | `v=spf1 -all` |
| TXT | `_dmarc` | `v=DMARC1; p=reject; sp=reject; adkim=s; aspf=s` |
| MX | (blank) | priority `0`, answer `.` (a "null MX", RFC 7505), only if Porkbun's form accepts `.` as the answer |

Leave Porkbun's email forwarding off for this domain; it adds its own MX records.

### Step 3: turn on Pages, before anything merges to main

The first push to `main` that touches `site/` starts the deploy workflow, so Pages has to be ready first.

1. In the repo: **Settings**, **Pages**, **Build and deployment**, **Source**: GitHub Actions.
2. Under **Custom domain**, enter `lasto.dev` and save. GitHub checks DNS and requests a certificate, which can take up to an hour. Don't add a CNAME file to the repo; with Actions deployments it's ignored.
3. When the certificate is ready, tick **Enforce HTTPS**. (.dev is on the HSTS preload list, so browsers only use HTTPS anyway.)

### Step 4: merge and deploy

1. When you approve Phase 1, I set it to done and public in `site/data/roadmap.toml` on this branch, so the first deploy doesn't say it's still waiting on sign-off.
2. Push Phase 1 to `main`, then merge the `website` branch into `main`. It sits on top of the Phase 1 commits, so it fast-forwards.
3. The `site` workflow builds, checks, and deploys. Its last step confirms Pages reports `https://lasto.dev/` and fetches the main pages, the mirrors, llms.txt, the sitemap, robots.txt, and security.txt from it. If the certificate or Enforce HTTPS isn't ready yet, that step fails; rerun the workflow once it is.

### Step 5: repo settings

- **About** (the gear on the repo page): website `https://lasto.dev`, and topics such as `can-bus`, `obd2`, `lexus`, `gx470`, `python`.
- **Settings**, **General**, **Social preview**: upload `og/github-social-preview.png` from the built site (`site/dist/og/` locally, or https://lasto.dev/og/github-social-preview.png once it's live).

### Step 6: check

- https://lasto.dev/ loads with a valid certificate, and https://www.lasto.dev/ redirects to it.
- https://lasto.dev/.well-known/security.txt and https://lasto.dev/llms.txt load.
- Submitting the sitemap to Google Search Console and Bing Webmaster Tools is W3.

### Keeping security.txt current

security.txt carries an Expires date 330 days after each build, and a monthly scheduled run rebuilds the site to keep it fresh. GitHub turns off scheduled workflows in a public repo after 60 days with no activity, so if the repo goes quiet for months, run the `site` workflow by hand (Actions, site, Run workflow) at least once before the date in https://lasto.dev/.well-known/security.txt.
