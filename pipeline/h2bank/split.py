"""Question splitter: `python -m h2bank.split`.

Boundary detection, in the priority order that survives real prelim papers:

1. Line-start question numbers (`^\\s*\\d{1,2}[.)]?\\s`) that **strictly
   increment** from 1. Only the longest consecutive chain is kept, which throws
   away stray matches.
2. Marks markers (`[4]` at a line end) validating that a candidate really has a
   question body under it, and giving `marks_total`.
3. Part labels ((i)/(ii)/(a)/(b)) collected under their parent question.
4. The x-position of the number, from pdfplumber word boxes: a real question
   number sits in the left gutter, so centred text like "3 hours" on the cover
   page is rejected.

Every paper gets a `data/pdfs/<name>.split.json` sidecar recording the detected
boundaries so a split can be reviewed without re-running the stage.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .db import init_db, replace_questions, set_parse_status
from .util import sha256_text

log = logging.getLogger("h2bank.split")

# A question number: 1-2 digits, optional . or ), then whitespace and content.
QNUM_RE = re.compile(r"^\s*(\d{1,2})\s*[.)]?\s+(\S.*)$")
# A question number alone on its line. This happens whenever a question opens
# with a diagram: the number is its own text line and the prose starts below the
# figure. Only trusted inside the left gutter, since centred bare numbers are
# page numbers.
QNUM_ONLY_RE = re.compile(r"^\s*(\d{1,2})\s*[.)]?\s*$")
# Marks at end of a line: [4] or (4) with an optional trailing bracket/space.
MARKS_RE = re.compile(r"[\[(](\d{1,2})[\])]\s*$")
# Part labels at the start of a line, e.g. (i), (ii), (a), (b).
PART_RE = re.compile(r"^\s*\(([ivx]{1,4}|[a-h])\)\s", re.IGNORECASE)


@dataclass
class Line:
    page: int          # 1-based
    top: float
    x0: float
    text: str


@dataclass
class Candidate:
    q_number: int
    line_index: int
    page: int
    x0: float
    text: str


@dataclass
class SplitResult:
    questions: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    needs_ocr_pages: list[int] = field(default_factory=list)
    confidence: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def marks_total(self) -> int:
        return sum(q.get("marks_total") or 0 for q in self.questions)


# --------------------------------------------------------------------------
# text extraction
# --------------------------------------------------------------------------

def extract_lines(pdf_path: Path, min_chars: int) -> tuple[list[Line], int, list[int]]:
    """Return (lines, page_count, pages_needing_ocr) using pdfplumber."""
    import pdfplumber

    lines: list[Line] = []
    low_text: list[int] = []
    with pdfplumber.open(pdf_path) as pdf:
        n_pages = len(pdf.pages)
        for page_no, page in enumerate(pdf.pages, start=1):
            try:
                raw = page.extract_text() or ""
            except Exception as exc:
                raw = ""
                log.warning("%s p%d: extract_text failed: %s", pdf_path.name, page_no, exc)
            if len(raw.strip()) < min_chars:
                low_text.append(page_no)

            try:
                page_lines = page.extract_text_lines()
            except Exception as exc:
                log.warning("%s p%d: extract_text_lines failed: %s",
                            pdf_path.name, page_no, exc)
                page_lines = []

            for pl in page_lines:
                text = (pl.get("text") or "").rstrip()
                if not text.strip():
                    continue
                lines.append(
                    Line(
                        page=page_no,
                        top=float(pl.get("top", 0.0)),
                        x0=float(pl.get("x0", 0.0)),
                        text=text,
                    )
                )
    return lines, n_pages, low_text


# --------------------------------------------------------------------------
# boundary detection
# --------------------------------------------------------------------------

def snap_to_modal_gutter(
    candidates: list[Candidate], tolerance: float
) -> list[Candidate]:
    """Signal 4, sharpened: keep only numbers on the paper's own margin.

    A fixed threshold is not enough. In HCI 2025 P1 the prose "...subdivided
    into 5 vertical strips..." wraps so that a literal "5" starts a line at
    x0=99, while every real question number sits at x0=72. Snapping to the modal
    x0 discards the impostor without discarding the indented part labels that
    share the wider margin.
    """
    if len(candidates) < 3:
        return candidates
    buckets = Counter(round(c.x0) for c in candidates)
    modal_x0, _ = buckets.most_common(1)[0]
    return [c for c in candidates if abs(c.x0 - modal_x0) <= tolerance]


def find_candidates(lines: list[Line], gutter_max_x0: float) -> list[Candidate]:
    """Signal 1 + signal 4: line-start numbers that sit in the left gutter."""
    out: list[Candidate] = []
    for i, line in enumerate(lines):
        if line.x0 > gutter_max_x0:
            continue  # centred / indented text such as "3 hours" on a cover page

        m = QNUM_RE.match(line.text)
        if m:
            # Needs a real body after the number, so a stray "2 ," fragment or a
            # left-aligned page number does not qualify.
            if len(m.group(2).strip()) < 3:
                continue
            number = int(m.group(1))
        else:
            bare = QNUM_ONLY_RE.match(line.text)
            if not bare:
                continue
            number = int(bare.group(1))

        out.append(
            Candidate(
                q_number=number,
                line_index=i,
                page=line.page,
                x0=line.x0,
                text=line.text,
            )
        )
    return out


def longest_increasing_chain(
    candidates: list[Candidate], max_step: int = 2, max_start: int = 2
) -> tuple[list[Candidate], int]:
    """Longest strictly increasing run of question numbers, in document order.

    Returns (chain, gaps). Steps of +1 are the norm; a step of +2 is allowed but
    counted as a gap, so one number that extraction failed to see does not
    truncate the whole paper. Chains may start at Q2 (also a gap) for the same
    reason. Among equal-length chains the one with fewest gaps wins.
    """
    if not candidates:
        return [], 0

    n = len(candidates)
    length = [0] * n
    gaps = [0] * n
    prev = [-1] * n

    for i, c in enumerate(candidates):
        if 1 <= c.q_number <= max_start:
            length[i] = 1
            gaps[i] = c.q_number - 1  # a chain starting at Q2 has missed Q1
        for j in range(i):
            step = c.q_number - candidates[j].q_number
            if length[j] > 0 and 1 <= step <= max_step:
                cand_len = length[j] + 1
                cand_gaps = gaps[j] + (step - 1)
                if (cand_len, -cand_gaps) > (length[i], -gaps[i]):
                    length[i], gaps[i], prev[i] = cand_len, cand_gaps, j

    if max(length) == 0:
        return [], 0

    end = max(range(n), key=lambda i: (length[i], -gaps[i], -i))
    total_gaps = gaps[end]
    chain: list[Candidate] = []
    while end != -1:
        chain.append(candidates[end])
        end = prev[end]
    return list(reversed(chain)), total_gaps


def build_questions(
    lines: list[Line], chain: list[Candidate], confidence: float, low_text: set[int]
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for idx, cand in enumerate(chain):
        start = cand.line_index
        end = chain[idx + 1].line_index if idx + 1 < len(chain) else len(lines)
        body = lines[start:end]
        text = "\n".join(l.text for l in body).strip()

        marks = [int(m.group(1)) for l in body if (m := MARKS_RE.search(l.text))]
        parts: list[str] = []
        for offset, l in enumerate(body):
            line_text = l.text
            if offset == 0:
                # The first part label usually shares the line with the question
                # number: "1 (a) Without using a calculator, ...". Strip the
                # number so the label is still seen.
                line_text = QNUM_RE.sub(r"\2", line_text, count=1)
            pm = PART_RE.match(line_text)
            if pm:
                label = f"({pm.group(1).lower()})"
                if label not in parts:
                    parts.append(label)

        pages = {l.page for l in body} or {cand.page}
        questions.append(
            {
                "q_number": cand.q_number,
                "part_labels": parts,
                "page_start": min(pages),
                "page_end": max(pages),
                "marks_total": sum(marks) if marks else None,
                "full_text": text,
                "needs_ocr": bool(pages & low_text),
                "extract_confidence": confidence,
                "text_sha256": sha256_text(text),
            }
        )
    return questions


def score(
    chain: list[Candidate],
    n_candidates: int,
    n_gaps: int,
    questions: list[dict[str, Any]],
    expected_min: int,
    expected_max: int,
    paper_marks: int,
) -> tuple[float, dict[str, float]]:
    """Confidence from four signals.

    numbering  how many gutter candidates the chain had to throw away
    sequence   how many question numbers are missing from the chain
    marks      fraction of questions that found a marks marker
    count      question count inside the expected range
    marks_sum  H2 Paper 1 and Paper 2 are both 100 marks, so a total near 100 is
               strong independent evidence that the whole paper was captured
    """
    n = len(chain)
    dropped = max(0, n_candidates - n)
    numbering = 1.0 - min(0.5, dropped / n_candidates) if n_candidates else 0.0
    sequence = max(0.0, 1.0 - 0.15 * n_gaps)

    with_marks = sum(1 for q in questions if q.get("marks_total"))
    marks = with_marks / n if n else 0.0

    if n == 0:
        count = 0.0
    elif expected_min <= n <= expected_max:
        count = 1.0
    else:
        distance = expected_min - n if n < expected_min else n - expected_max
        count = max(0.0, 1.0 - distance / 5.0)

    total_marks = sum(q.get("marks_total") or 0 for q in questions)
    marks_sum = max(0.0, 1.0 - abs(total_marks - paper_marks) / 20.0)

    total = (
        0.20 * numbering
        + 0.15 * sequence
        + 0.25 * marks
        + 0.20 * count
        + 0.20 * marks_sum
    )
    return round(total, 3), {
        "numbering": round(numbering, 3),
        "sequence": round(sequence, 3),
        "marks": round(marks, 3),
        "count": round(count, 3),
        "marks_sum": round(marks_sum, 3),
        "gaps": float(n_gaps),
        "total_marks": float(total_marks),
    }


def split_pdf(pdf_path: Path, split_cfg: dict[str, Any]) -> SplitResult:
    """Full split of one PDF. Pure function of the file plus config."""
    min_chars = int(split_cfg.get("min_chars_per_page", 50))
    gutter = float(split_cfg.get("gutter_max_x0", 120))
    exp_min = int(split_cfg.get("expected_questions_min", 10))
    exp_max = int(split_cfg.get("expected_questions_max", 12))

    lines, n_pages, low_text = extract_lines(pdf_path, min_chars)
    result = SplitResult(pages=n_pages, needs_ocr_pages=low_text)

    if low_text:
        result.notes.append(
            f"{len(low_text)} of {n_pages} pages yielded <{min_chars} chars "
            f"(pages {low_text[:10]}) - flagged needs_ocr, OCR not attempted"
        )

    paper_marks = int(split_cfg.get("paper_marks", 100))

    raw_candidates = find_candidates(lines, gutter)
    candidates = snap_to_modal_gutter(
        raw_candidates, float(split_cfg.get("gutter_tolerance", 3.0))
    )
    chain, n_gaps = longest_increasing_chain(candidates)
    if not chain:
        result.notes.append("no incrementing question numbers found in the gutter")
        return result

    questions = build_questions(lines, chain, 0.0, set(low_text))
    conf, signals = score(
        chain, len(candidates), n_gaps, questions, exp_min, exp_max, paper_marks
    )
    for q in questions:
        q["extract_confidence"] = conf

    result.questions = questions
    result.confidence = conf
    result.signals = signals
    if len(chain) < exp_min or len(chain) > exp_max:
        result.notes.append(
            f"{len(chain)} questions detected, outside the expected {exp_min}-{exp_max}"
        )
    if n_gaps:
        missing = sorted(
            set(range(1, chain[-1].q_number + 1)) - {c.q_number for c in chain}
        )
        result.notes.append(f"question number(s) not detected: {missing}")
    total_marks = sum(q.get("marks_total") or 0 for q in questions)
    if total_marks != paper_marks:
        result.notes.append(f"marks total {total_marks}, expected {paper_marks}")
    return result


# --------------------------------------------------------------------------
# stage entry point
# --------------------------------------------------------------------------

def sidecar_path(cfg: Config, rel_path: str) -> Path:
    return cfg.root / f"{rel_path.rsplit('.', 1)[0]}.split.json"


def run(cfg: Config, conn, only_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Split every question paper. Mark schemes are stored but not split."""
    sql = (
        "SELECT * FROM papers WHERE doc_type IN ('qp', 'combined') ORDER BY year DESC, "
        "school_code, paper_no"
    )
    rows = [dict(r) for r in conn.execute(sql)]
    if only_ids:
        rows = [r for r in rows if r["id"] in only_ids]

    summaries: list[dict[str, Any]] = []
    for row in rows:
        pdf_path = cfg.root / row["rel_path"]
        if not pdf_path.is_file():
            log.error("missing file for paper %d: %s", row["id"], pdf_path)
            set_parse_status(conn, row["id"], "failed", "file missing on disk")
            continue

        log.info("splitting %s", row["rel_path"])
        try:
            result = split_pdf(pdf_path, cfg.split)
        except Exception as exc:
            log.exception("split failed for %s", row["rel_path"])
            set_parse_status(conn, row["id"], "failed", f"{type(exc).__name__}: {exc}")
            continue

        n = replace_questions(conn, row["id"], result.questions)
        status = "ok" if result.questions and result.confidence >= 0.6 else (
            "partial" if result.questions else "failed"
        )
        notes = "; ".join(result.notes) or None
        set_parse_status(conn, row["id"], status, notes)

        sidecar = sidecar_path(cfg, row["rel_path"])
        sidecar.write_text(
            json.dumps(
                {
                    "paper_id": row["id"],
                    "rel_path": row["rel_path"],
                    "school_code": row["school_code"],
                    "year": row["year"],
                    "paper_no": row["paper_no"],
                    "doc_type": row["doc_type"],
                    "pages": result.pages,
                    "needs_ocr_pages": result.needs_ocr_pages,
                    "confidence": result.confidence,
                    "signals": result.signals,
                    "notes": result.notes,
                    "questions": [
                        {
                            "q_number": q["q_number"],
                            "page_start": q["page_start"],
                            "page_end": q["page_end"],
                            "marks_total": q["marks_total"],
                            "part_labels": q["part_labels"],
                            "chars": len(q["full_text"]),
                            "first_line": q["full_text"].split("\n", 1)[0][:120],
                        }
                        for q in result.questions
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        summaries.append(
            {
                "paper_id": row["id"],
                "paper": Path(row["rel_path"]).name,
                "school_code": row["school_code"],
                "year": row["year"],
                "paper_no": row["paper_no"],
                "questions": n,
                "marks_total": result.marks_total,
                "confidence": result.confidence,
                "signals": result.signals,
                "status": status,
                "flags": notes or "",
            }
        )
    return summaries


def print_summary(summaries: list[dict[str, Any]]) -> None:
    print(
        f"\n{'paper':34} {'q':>3} {'marks':>6} {'conf':>5} {'status':8} flags"
    )
    print("-" * 100)
    for s in summaries:
        print(
            f"{s['paper'][:34]:34} {s['questions']:>3} {s['marks_total']:>6} "
            f"{s['confidence']:>5.2f} {s['status']:8} {s['flags'][:34]}"
        )
    if summaries:
        avg = sum(s["confidence"] for s in summaries) / len(summaries)
        print(f"\n{len(summaries)} papers split, mean confidence {avg:.2f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Split papers into questions")
    parser.add_argument("--paper-id", type=int, action="append", dest="paper_ids")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config()
    conn = init_db(cfg.db_path, cfg.schema_path)
    summaries = run(cfg, conn, args.paper_ids)
    print_summary(summaries)

    # Re-splitting replaces question rows, and question_topics cascades on
    # delete - so a split always invalidates tags. Say so loudly: committing the
    # database between these two stages ships a bank with no topics.
    untagged = conn.execute(
        """SELECT COUNT(*) AS c FROM questions q
           WHERE NOT EXISTS (
               SELECT 1 FROM question_topics qt WHERE qt.question_id = q.id)"""
    ).fetchone()["c"]
    if untagged:
        print(
            f"\nWARNING: {untagged} question(s) now have no topic tag - splitting "
            f"dropped them.\n         Run `python -m h2bank.tag` before committing "
            f"data/bank.sqlite."
        )
    conn.close()
    # Deliberately exit 0: the tag stage runs straight after this one in the
    # pipeline, so a non-zero exit here would fail every run.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
