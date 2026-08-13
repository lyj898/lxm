"""freetestpaper.com adapter.

Observed structure (SMF forum):

  /jc2-h2-maths-<year>/
      board listing, one topic per school; topic links live in
      `td.subject span[id^="msg_"] > a`
  topic page
      post body carries Year / Level / Subject / School / File Size metadata,
      but the actual download link is replaced with
      "You are not allowed to view links. Register or Login".

So the site is discoverable but **login-gated**: no PDF URL is reachable
without an account. This adapter therefore probes one topic page, records the
gate, and returns no links rather than pretending otherwise. Supply a session
cookie via `[crawl.freetestpaper] cookie = "..."` if you have your own account
and want to use it.
"""

from __future__ import annotations

import re

from ..util import detect_exam_type, detect_school, detect_year
from .base import PaperLink, SourceAdapter, register

_GATE_MARKERS = (
    "you are not allowed to view links",
    "registered users are able to view",
)


@register
class FreeTestPaper(SourceAdapter):
    name = "freetestpaper"
    base_url = "https://freetestpaper.com"
    board_url_tmpl = "https://freetestpaper.com/jc2-h2-maths-{year}/"

    def discover(self) -> list[PaperLink]:
        year = int(self.cfg.get("prefer_year_from") or 2025)
        board_url = self.board_url_tmpl.format(year=year)

        topics = self._topics(board_url)
        if not topics:
            return []

        # Probe the newest topic to see whether downloads are reachable.
        probe_url, probe_title = topics[0]
        try:
            resp = self.fetcher.get_html(probe_url)
        except Exception as exc:
            self._note_error(f"topic {probe_url}: {type(exc).__name__}: {exc}")
            return []

        body = resp.text.lower()
        if any(marker in body for marker in _GATE_MARKERS):
            self._note_error(
                f"login-gated: {len(topics)} topics listed for {year} but download "
                f"links require a registered account (probed {probe_url})"
            )
            return []

        # Not gated (e.g. a cookie was supplied): collect attachment links.
        links: list[PaperLink] = []
        for topic_url, title in topics:
            links.extend(self._links_for_topic(topic_url, title))
        return self.sort_newest_first(links)

    def _topics(self, board_url: str) -> list[tuple[str, str]]:
        try:
            soup = self._soup(board_url)
        except Exception as exc:
            self._note_error(f"board {board_url}: {type(exc).__name__}: {exc}")
            return []
        out: list[tuple[str, str]] = []
        for cell in soup.select("td.subject"):
            for span in cell.select('span[id^="msg_"]'):
                a = span.find("a", href=True)
                if a:
                    out.append((a["href"], a.get_text(strip=True)))
        return out

    def _links_for_topic(self, topic_url: str, title: str) -> list[PaperLink]:
        try:
            soup = self._soup(topic_url)
        except Exception as exc:
            self._note_error(f"topic {topic_url}: {type(exc).__name__}: {exc}")
            return []
        code, school = detect_school(title, topic_url)
        year = detect_year(title, topic_url)
        out: list[PaperLink] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "dlattach" not in href or "type=avatar" in href:
                continue
            name = a.get_text(strip=True)
            out.append(
                PaperLink(
                    source=self.name,
                    file_url=href,
                    page_url=topic_url,
                    title=name or title,
                    school=school,
                    school_code=code,
                    year=year,
                    exam_type=detect_exam_type(title),
                    paper_no=None,
                    doc_type="combined",
                    extra={"container": "pdf"},
                )
            )
        return out
