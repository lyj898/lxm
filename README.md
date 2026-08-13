# H2 Maths Question Bank

A searchable archive of Singapore JC **H2 Mathematics (9758)** prelim papers,
split into individual questions and tagged by syllabus topic. Everything lives
in this repo: the pipeline, the PDFs, the SQLite database, and the static site.

Deployed via GitHub Pages at **lowxianming.com**.

> Personal study archive. Papers © their respective schools. Contact for removal.
> The site is `noindex,nofollow` with a deny-all `robots.txt` and is not
> submitted anywhere.

---

## Layout

```
pipeline/h2bank/      the pipeline package
  adapters/           one module per source site, common discover() interface
  fetcher.py          polite HTTP: robots.txt, >=5s delays, backoff on 429/5xx
  archive.py          remote-zip peeking over HTTP Range; safe extraction
  crawl.py            stage 1: discover + download
  split.py            stage 2: PDF -> questions
  tag.py              stage 3: topic tagging (rules, then LLM)
data/pdfs/            compressed PDFs, {year}_{school}_{exam}_{paper}_{qp|ms}.pdf
data/pdfs/*.split.json  per-paper boundary sidecars for review
data/bank.sqlite      the database (committed)
data/candidates.json  latest discovery output
site/                 static site: vanilla JS + sql.js + pdf.js, no build step
schema.sql            database schema and seeded 9758 topics
config.toml           all tunables, including the download cap
```

## Setup

Python 3.12 (3.11 also works).

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pip install -e .
```

The `pip install -e .` puts `h2bank` on the path so the stage entry points work
from any directory. On macOS/Linux use `.venv/bin/python` instead.

## Running the pipeline

Every stage is idempotent — re-running never duplicates rows, and downloads are
skipped when the source URL or SHA-256 is already known.

```bash
python -m h2bank.initdb                    # create/migrate data/bank.sqlite
python -m h2bank.crawl --discover-only     # walk sources, write candidates.json
python -m h2bank.crawl --cap 5             # download the top 5 school-exams
python -m h2bank.split                     # split question papers into questions
python -m h2bank.tag                       # tag topics (rules, then LLM)
python -m pytest -q                        # tests
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `--cap N` | Download N school-exams, overriding `config.toml` |
| `--schools ACJC,EJC` | Download specific schools instead of the automatic pick |
| `--download-only` | Reuse `data/candidates.json`, skip re-crawling listings |
| `--discover-only` | Walk listings and stop before downloading |
| `--no-llm` (tag) | Rules only; leaves ambiguous questions untagged |

### The API key

The LLM tagging pass needs `ANTHROPIC_API_KEY`. Locally, put it in a
`.env` at the repo root (already gitignored):

```
ANTHROPIC_API_KEY=sk-ant-...
```

In CI it comes from the repo secret of the same name
(**Settings → Secrets and variables → Actions → New repository secret**).
Without a key the tag stage warns and runs rules only.

## How questions are displayed

Extracted text cannot represent an exam question. Fractions linearise into
nonsense (an integral comes out as `Find dx`), the integral and sigma signs land
in a private-use glyph range and are lost, and diagrams disappear entirely — 73%
of the pilot's questions show at least one of these, and 17 contain a figure.

So the splitter records a **crop box** per question (`page_start`, `page_end`,
`y_top`, `y_bottom` in PDF points from the page top), and the site renders that
region straight from the PDF with pdf.js, lazily as cards scroll into view.
Multi-page questions render as stacked per-page slices.

The extracted text is still stored and is what powers **search** and **topic
tagging**; each card can show it via "Extracted text". Two extraction hazards are
handled in `split.py`, both found the hard way:

- `pdfplumber.extract_text_lines()` returns **text-object order, not reading
  order** — a page footer came back between the first and second line of a
  question, so bodies were positionally scrambled. Lines are sorted by
  `(page, top, x0)`.
- Running headers and footers were landing inside question text. They are
  stripped by band position plus repetition across pages, with bare numbers
  excluded from the repetition rule (a lone `5` in the gutter is HCI's Q5, while
  page 5's page number is also `5`).

## Viewing the site locally

```bash
python -m http.server 8000
```

Then open <http://localhost:8000> — the root `index.html` hops to `site/`. The
page looks for `data/bank.sqlite` and falls back to `../data/bank.sqlite`, so
the same files work both locally and deployed at the domain root.

## Deployment

### Enable Pages

**Settings → Pages → Build and deployment → Source: GitHub Actions.**
Then set **Custom domain** to `lowxianming.com` and tick **Enforce HTTPS** once
the certificate is issued (it can take a few minutes after DNS resolves).

`pages.yml` assembles `site/` plus `data/` into the artifact, copies the root
`CNAME`, and deploys on every push to `main` that touches those paths.

### DNS for lowxianming.com

At your DNS provider, point the apex at GitHub Pages' four IPv4 addresses:

| Type | Name | Value |
| --- | --- | --- |
| A | `@` | `185.199.108.153` |
| A | `@` | `185.199.109.153` |
| A | `@` | `185.199.110.153` |
| A | `@` | `185.199.111.153` |
| CNAME | `www` | `lyj898.github.io.` |

Optionally add the IPv6 AAAA records for `@`: `2606:50c0:8000::153`,
`2606:50c0:8001::153`, `2606:50c0:8002::153`, `2606:50c0:8003::153`.

Verify with `nslookup lowxianming.com` before enabling HTTPS. GitHub's current
IPs are documented under "Managing a custom domain for your GitHub Pages site" —
check there if a record ever stops resolving.

### Scheduled pipeline

`pipeline.yml` runs weekly (Mondays 03:17 UTC) and on manual dispatch. It runs
tests, then crawl → split → tag, and commits only when something actually
changed. Concurrency-grouped so two runs cannot race on the committed database.

**The cron is currently a deliberate no-op**: `download_cap = 0` in
`config.toml`, so the scheduled run re-discovers candidates, downloads nothing,
and exits.

## Scaling past the pilot

The pilot is 5 school-exams — 10 papers with solutions, 115 questions. To grow it:

1. **Raise the cap.** Set `download_cap` in `config.toml` (it counts
   *school-exams*; Paper 1 and Paper 2 arrive together in one archive). Or
   dispatch `pipeline.yml` with a `cap` input for a one-off run.
2. **Widen discovery.** `discover_target_exams` bounds how many school-exams
   discovery collects; `max_years` (per adapter, default 2) bounds how far back
   it walks. There are 17 schools on the 2025 page alone, 12 with complete
   qp + ms, and year pages back to 2020.
3. **Nothing else needs to change.** Adapters share a `discover() -> list[PaperLink]`
   interface, dedupe is by SHA-256 plus source URL, and every stage is
   idempotent, so scaling is a config change rather than a code change.

Two things to expect at larger scale:

- **Scanned papers.** Some schools (ASRJC 2025) publish image-only PDFs. Those
  are flagged `needs_ocr` per page and produce no questions; OCR is not
  implemented.
- **Incomplete uploads.** Some entries are solutions-only (DHS) or answer-keys-only
  (CJC). Selection prefers complete school-exams, so these sort to the bottom.

### Adding a source

Subclass `SourceAdapter`, implement `discover()`, decorate with `@register`, and
add the name to `sources` in `config.toml`. **Fetch the real listing HTML and
read it before writing selectors** — every adapter here was written against
saved HTML, and two of the three sites turned out to need special handling
(UTF-16 encoding; POST-only downloads).

### Source notes

| Source | Status |
| --- | --- |
| `sgtestpaper.com` | Works. Pages are UTF-16; each school is one `.zip` of PDFs, whose contents are listed via HTTP Range before downloading. |
| `freetestpaper.com` | **Login-gated.** Listings are readable but every download link is replaced with "Register or Login". The adapter detects this and reports it; supply your own cookie via config if you have an account. |
| `testpapersfree.com` | Implemented, unused in the pilot. Questions and answers are separate entries and downloads require a form POST. |

## Crawling etiquette

`config.toml` sets a descriptive `user_agent`, a `min_delay_seconds` of 5 per
host, `respect_robots = true`, and exponential backoff on 429/5xx. Files
&ge;95 MB are refused outright. PDFs are recompressed with pikepdf/pymupdf on
ingest, targeting under 2 MB. Requests send `Connection: close` because these
hosts expire keep-alive sockets in 5 seconds — reusing one would fail and
silently double the real request count.
