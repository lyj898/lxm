"""Discover + download stage: `python -m h2bank.crawl`.

Two halves, either of which can run alone:

  discovery  walks each source adapter and writes data/candidates.json
  download   picks the top `cap` school-exams from that file and ingests them

Both halves are idempotent. Discovery rewrites the candidates file; download
skips anything whose source URL or SHA-256 is already in `papers`.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .adapters import PaperLink, get_adapter
from .archive import extract_pdf
from .config import Config, load_config
from .db import connect, find_paper_by_sha, find_paper_by_url, init_db, log_crawl, upsert_paper
from .fetcher import Fetcher, fetcher_from_config
from .pdfops import compress, page_count
from .util import pdf_filename, sha256_bytes

log = logging.getLogger("h2bank.crawl")


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def discover(cfg: Config, conn) -> list[PaperLink]:
    """Run every configured adapter and return the union of their links."""
    adapter_cfg: dict[str, Any] = {
        "discover_target_exams": cfg.crawl.get("discover_target_exams", 8),
        "prefer_year_from": cfg.crawl.get("prefer_year_from"),
    }

    all_links: list[PaperLink] = []
    for name in cfg.crawl["sources"]:
        fetcher = fetcher_from_config(cfg.crawl)
        adapter = get_adapter(name)(fetcher, dict(adapter_cfg))
        log.info("=== discovering via %s ===", name)
        try:
            links = adapter.discover()
        except NotImplementedError as exc:
            adapter._note_error(f"adapter not implemented: {exc}")
            links = []
        except Exception as exc:
            adapter._note_error(f"discover failed: {type(exc).__name__}: {exc}")
            links = []

        n_exams = len({link.exam_key for link in links})
        log.info(
            "%s: %d links across %d school-exams (%d URLs fetched, %d errors)",
            name, len(links), n_exams, fetcher.urls_seen, len(adapter.errors),
        )
        log_crawl(conn, name, fetcher.urls_seen, n_exams, adapter.errors)
        all_links.extend(links)

        target = int(adapter_cfg["discover_target_exams"])
        if len({link.exam_key for link in all_links}) >= target:
            log.info("discovery target of %d school-exams met; stopping", target)
            break

    return all_links


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

@dataclass
class ExamGroup:
    """All PDFs belonging to one (school, year, exam_type) sitting."""

    exam_key: str
    school: str
    school_code: str
    year: int
    exam_type: str
    source: str
    links: list[PaperLink] = field(default_factory=list)

    @property
    def paper_numbers(self) -> list[int | None]:
        seen: list[int | None] = []
        for link in self.links:
            if link.paper_no not in seen:
                seen.append(link.paper_no)
        return sorted(seen, key=lambda p: (p is None, p))

    def docs_for(self, paper_no: int | None) -> set[str]:
        return {l.doc_type for l in self.links if l.paper_no == paper_no}

    @property
    def n_papers(self) -> int:
        return len(self.paper_numbers)

    @property
    def complete_papers(self) -> int:
        """Papers that have both a question paper and a mark scheme."""
        n = 0
        for pno in self.paper_numbers:
            docs = self.docs_for(pno)
            if "combined" in docs or {"qp", "ms"} <= docs:
                n += 1
        return n

    @property
    def has_any_qp(self) -> bool:
        return any(l.doc_type in ("qp", "combined") for l in self.links)

    @property
    def total_bytes(self) -> int:
        return sum(int(l.extra.get("member_bytes") or 0) for l in self.links)

    def summary(self) -> dict[str, Any]:
        return {
            "exam_key": self.exam_key,
            "school": self.school,
            "school_code": self.school_code,
            "year": self.year,
            "exam_type": self.exam_type,
            "source": self.source,
            "papers": {
                str(pno): sorted(self.docs_for(pno)) for pno in self.paper_numbers
            },
            "complete_papers": self.complete_papers,
            "n_pdfs": len(self.links),
            "uncompressed_bytes": self.total_bytes,
            "links": [l.to_dict() for l in self.links],
        }


def group_by_exam(links: Iterable[PaperLink]) -> list[ExamGroup]:
    groups: dict[str, ExamGroup] = {}
    for link in links:
        g = groups.get(link.exam_key)
        if g is None:
            g = ExamGroup(
                exam_key=link.exam_key,
                school=link.school,
                school_code=link.school_code,
                year=link.year or 0,
                exam_type=link.exam_type,
                source=link.source,
            )
            groups[link.exam_key] = g
        g.links.append(link)
    return list(groups.values())


def select(groups: list[ExamGroup], cap: int, only: list[str] | None = None) -> list[ExamGroup]:
    """Pick up to `cap` school-exams: newest first, most complete first, one per
    school so the pilot spans as many schools as possible."""
    usable = [g for g in groups if g.has_any_qp and g.school_code != "UNK"]
    if only:
        wanted = [c.strip().upper() for c in only if c.strip()]
        by_code = {g.school_code: g for g in usable}
        return [by_code[c] for c in wanted if c in by_code][:cap]

    usable.sort(
        key=lambda g: (-g.year, -g.complete_papers, -g.n_papers, g.school_code)
    )
    chosen: list[ExamGroup] = []
    seen_schools: set[str] = set()
    for g in usable:
        if len(chosen) >= cap:
            break
        if g.school_code in seen_schools:
            continue
        chosen.append(g)
        seen_schools.add(g.school_code)
    # Only if one-per-school could not fill the cap, allow repeats.
    for g in usable:
        if len(chosen) >= cap:
            break
        if g not in chosen:
            chosen.append(g)
    return chosen


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------

def source_url_for(link: PaperLink) -> str:
    """Stable per-PDF URL. Zip members get a fragment so each PDF is distinct."""
    member = link.extra.get("member")
    return f"{link.file_url}#{member}" if member else link.file_url


def _fetch_pdf_bytes(
    fetcher: Fetcher, link: PaperLink, zip_cache: dict[str, bytes]
) -> bytes:
    container = link.extra.get("container", "pdf")
    if container == "zip":
        data = zip_cache.get(link.file_url)
        if data is None:
            log.info("downloading archive %s", link.file_url)
            data = fetcher.get_binary(link.file_url).content
            zip_cache[link.file_url] = data
        return extract_pdf(data, link.extra["member"])

    if link.extra.get("download_method") == "post":
        field_name = link.extra.get("post_field", "testpaperid")
        value = link.extra.get(field_name) or link.extra.get("testpaperid")
        log.info("POST %s (%s=%s)", link.file_url, field_name, value)
        resp = fetcher.post_binary(link.file_url, {field_name: value})
        return resp.content

    return fetcher.get_binary(link.file_url).content


def _unique_rel_path(cfg: Config, conn, filename: str, sha: str) -> str:
    """Avoid clobbering a different PDF that maps to the same canonical name."""
    stem, ext = filename.rsplit(".", 1)
    for suffix in ("", "_b", "_c", "_d", "_e"):
        candidate = f"{stem}{suffix}.{ext}"
        rel = f"{cfg.raw['paths']['pdf_dir']}/{candidate}"
        row = conn.execute(
            "SELECT file_sha256 FROM papers WHERE rel_path = ?", (rel,)
        ).fetchone()
        if row is None or row["file_sha256"] == sha:
            return rel
    raise RuntimeError(f"could not find a free filename for {filename}")


def download_groups(cfg: Config, conn, groups: list[ExamGroup]) -> list[dict[str, Any]]:
    fetcher = fetcher_from_config(cfg.crawl)
    zip_cache: dict[str, bytes] = {}
    target = int(cfg.compress.get("target_bytes", 2 * 1024 * 1024))
    do_compress = bool(cfg.compress.get("enabled", True))
    results: list[dict[str, Any]] = []

    for group in groups:
        for link in group.links:
            src_url = source_url_for(link)
            existing = find_paper_by_url(conn, link.source, src_url)
            if existing:
                log.info("already ingested, skipping: %s", src_url)
                results.append({"status": "skipped", "rel_path": existing["rel_path"],
                                "link": link, "paper_id": existing["id"]})
                continue

            try:
                raw = _fetch_pdf_bytes(fetcher, link, zip_cache)
            except Exception as exc:
                log.error("download failed for %s: %s: %s", src_url, type(exc).__name__, exc)
                results.append({"status": "failed", "link": link,
                                "note": f"{type(exc).__name__}: {exc}"})
                continue

            if len(raw) >= int(cfg.crawl["max_file_bytes"]):
                log.error("refusing %s: %d bytes", src_url, len(raw))
                results.append({"status": "refused", "link": link,
                                "note": f"{len(raw)} bytes >= hard limit"})
                continue

            # SHA of the *original* bytes: stable source identity, independent of
            # which compressor version produced the stored file.
            sha = sha256_bytes(raw)
            dup = find_paper_by_sha(conn, sha)
            if dup:
                log.info("identical bytes already stored as %s", dup["rel_path"])
                results.append({"status": "duplicate", "rel_path": dup["rel_path"],
                                "link": link, "paper_id": dup["id"]})
                continue

            stored, note = (compress(raw, target) if do_compress else (raw, "compression disabled"))
            filename = pdf_filename(
                link.year or 0, link.school_code, link.exam_type,
                link.paper_no, link.doc_type,
            )
            rel = _unique_rel_path(cfg, conn, filename, sha)
            out = cfg.root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(stored)

            paper_id = upsert_paper(
                conn,
                source=link.source,
                source_url=src_url,
                school=link.school,
                school_code=link.school_code,
                year=link.year or 0,
                exam_type=link.exam_type,
                paper_no=link.paper_no,
                doc_type=link.doc_type,
                file_sha256=sha,
                rel_path=rel,
                pages=page_count(stored),
                size_bytes=len(stored),
                parse_status="pending",
                parse_notes=note,
            )
            log.info("ingested %s (%d bytes) -> paper id %d", rel, len(stored), paper_id)
            results.append({"status": "new", "rel_path": rel, "link": link,
                            "paper_id": paper_id, "note": note})

    return results


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover and download H2 Maths papers")
    parser.add_argument("--cap", type=int, default=None,
                        help="school-exams to download (default: config download_cap)")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--download-only", action="store_true",
                        help="reuse the existing candidates file")
    parser.add_argument("--schools", default=None,
                        help="comma-separated school codes to download instead of the automatic pick")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config()
    conn = init_db(cfg.db_path, cfg.schema_path)
    cap = args.cap if args.cap is not None else int(cfg.crawl["download_cap"])

    if args.download_only:
        payload = json.loads(cfg.candidates_path.read_text(encoding="utf-8"))
        links = [PaperLink(**{k: v for k, v in d.items()
                              if k not in ("key", "exam_key")})
                 for g in payload["groups"] for d in g["links"]]
    else:
        links = discover(cfg, conn)

    groups = group_by_exam(links)
    groups.sort(key=lambda g: (-g.year, -g.complete_papers, g.school_code))

    if not args.download_only:
        cfg.candidates_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.candidates_path.write_text(
            json.dumps(
                {"n_groups": len(groups), "groups": [g.summary() for g in groups]},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\ndiscovered {len(groups)} school-exams -> {cfg.candidates_path}")
        _print_candidates(groups)

    if args.discover_only:
        return 0
    if cap <= 0:
        print("\ndownload_cap is 0 - nothing downloaded (this is the intended "
              "no-op for the scheduled run).")
        return 0

    only = args.schools.split(",") if args.schools else None
    chosen = select(groups, cap, only)
    print(f"\nselected {len(chosen)} school-exam(s): "
          f"{', '.join(g.school_code for g in chosen)}")

    results = download_groups(cfg, conn, chosen)
    _print_results(results)
    conn.close()
    return 0


def _print_candidates(groups: list[ExamGroup]) -> None:
    print(f"\n{'school':7} {'year':5} {'papers':22} {'complete':8} {'pdfs':5} source")
    print("-" * 78)
    for g in groups:
        papers = "; ".join(
            f"P{pno if pno is not None else '?'}:{'+'.join(sorted(g.docs_for(pno)))}"
            for pno in g.paper_numbers
        )
        print(f"{g.school_code:7} {g.year:<5} {papers[:22]:22} "
              f"{g.complete_papers:<8} {len(g.links):<5} {g.source}")


def _print_results(results: list[dict[str, Any]]) -> None:
    print(f"\n{'status':10} {'paper':40} note")
    print("-" * 78)
    for r in results:
        name = Path(r.get("rel_path") or r["link"].title).name
        print(f"{r['status']:10} {name[:40]:40} {r.get('note', '')[:40]}")
    counts: dict[str, int] = defaultdict(int)
    for r in results:
        counts[r["status"]] += 1
    print("\n" + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    raise SystemExit(main())
