"""testpapersfree.com adapter.

Observed structure:

  /junior-college/maths-h2/            (and ?page=N for older entries)
      `table.alt` rows, one per document; the title cell links to
      `/junior-college/show.php?testpaperid=<id>` and the second cell holds a
      POST form with a hidden `testpaperid` input - the download itself is a
      POST, not a plain GET.
  titles look like
      "JC2 H2-Maths 2025 Prelim - Anglo Chinese - Questions"
      "JC2 H2-Maths 2025 Prelim - Anglo Chinese - Answers"

Questions and answers are separate entries, so paper_no is usually absent from
the title and is resolved later from the PDF itself.
"""

from __future__ import annotations

import re

from ..util import detect_doc_type, detect_exam_type, detect_paper_no, detect_school, detect_year
from .base import PaperLink, SourceAdapter, register

_ID_RE = re.compile(r"testpaperid=(\d+)")


@register
class TestPapersFree(SourceAdapter):
    name = "testpapersfree"
    base_url = "https://www.testpapersfree.com"
    listing_url = "https://www.testpapersfree.com/junior-college/maths-h2/"

    def discover(self) -> list[PaperLink]:
        target = int(self.cfg.get("discover_target_exams", 8))
        max_pages = int(self.cfg.get("max_listing_pages", 2))

        links: list[PaperLink] = []
        for page in range(1, max_pages + 1):
            url = self.listing_url if page == 1 else f"{self.listing_url}index.php?page={page}"
            links.extend(self._links_on(url))
            if len({link.exam_key for link in links}) >= target:
                break
        return self.sort_newest_first(links)

    def _links_on(self, url: str) -> list[PaperLink]:
        try:
            soup = self._soup(url)
        except Exception as exc:
            self._note_error(f"listing {url}: {type(exc).__name__}: {exc}")
            return []

        out: list[PaperLink] = []
        for table in soup.select("table.alt"):
            for a in table.select('a[href*="show.php?testpaperid="]'):
                m = _ID_RE.search(a["href"])
                if not m:
                    continue
                title = a.get_text(" ", strip=True)
                if not re.search(r"h2[\s\-]*maths?", title, re.IGNORECASE):
                    continue
                code, school = detect_school(title)
                out.append(
                    PaperLink(
                        source=self.name,
                        file_url=self._abs(a["href"]),
                        page_url=url,
                        title=title,
                        school=school,
                        school_code=code,
                        year=detect_year(title),
                        exam_type=detect_exam_type(title),
                        paper_no=detect_paper_no(title),
                        doc_type=detect_doc_type(title),
                        extra={
                            "container": "pdf",
                            "download_method": "post",
                            "post_field": "testpaperid",
                            "testpaperid": m.group(1),
                        },
                    )
                )
        return out

    def _abs(self, href: str) -> str:
        from urllib.parse import urljoin

        return urljoin(self.listing_url, href)
