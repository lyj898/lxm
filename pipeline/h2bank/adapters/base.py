"""Adapter base class, the PaperLink record, and the adapter registry."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from ..fetcher import Fetcher

log = logging.getLogger(__name__)


@dataclass
class PaperLink:
    """One downloadable PDF discovered on a source site.

    A "paper" for cap-counting purposes is the (school_code, year, exam_type,
    paper_no) group; its qp and ms links are two PaperLinks sharing one key.
    """

    source: str
    file_url: str          # direct URL of the PDF
    page_url: str          # listing/detail page it was found on
    title: str             # raw link text, kept for auditing
    school: str = "Unknown"
    school_code: str = "UNK"
    year: int | None = None
    exam_type: str = "prelim"
    paper_no: int | None = None
    doc_type: str = "qp"   # qp | ms | combined
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Identity of one paper: qp and ms of the same paper share this."""
        from ..util import paper_key

        return paper_key(self.school_code, self.year or 0, self.exam_type, self.paper_no)

    @property
    def exam_key(self) -> str:
        """Identity of one sitting. Paper 1 and Paper 2 share this, because they
        are two parts of the same exam and arrive in the same download."""
        return f"{self.school_code}|{self.year or 0}|{self.exam_type}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        d["exam_key"] = self.exam_key
        return d


class SourceAdapter:
    """Common interface for every source site."""

    name: str = "base"
    base_url: str = ""

    def __init__(self, fetcher: Fetcher, cfg: dict[str, Any] | None = None) -> None:
        self.fetcher = fetcher
        self.cfg = cfg or {}
        self.errors: list[str] = []

    def discover(self) -> list[PaperLink]:  # pragma: no cover - interface
        """Return every H2 Maths PDF link this source exposes, newest first."""
        raise NotImplementedError

    # -- helpers shared by adapters -------------------------------------
    def _soup(self, url: str):
        from bs4 import BeautifulSoup

        resp = self.fetcher.get_html(url)
        return BeautifulSoup(resp.text, "html.parser")

    def _note_error(self, msg: str) -> None:
        log.warning("[%s] %s", self.name, msg)
        self.errors.append(msg)

    @staticmethod
    def sort_newest_first(links: Iterable[PaperLink]) -> list[PaperLink]:
        return sorted(links, key=lambda p: (p.year or 0, p.school_code), reverse=True)


registry: dict[str, type[SourceAdapter]] = {}


def register(cls: type[SourceAdapter]) -> type[SourceAdapter]:
    registry[cls.name] = cls
    return cls


def get_adapter(name: str) -> type[SourceAdapter]:
    if name not in registry:
        raise KeyError(f"unknown source adapter: {name!r} (have {sorted(registry)})")
    return registry[name]
