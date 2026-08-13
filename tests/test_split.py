"""Splitter tests.

Unit tests cover the boundary-detection primitives on synthetic input. The
integration tests pin the expected split for five real 2025 papers, so a
regression in any heuristic shows up as a concrete diff rather than a vague
confidence wobble. They skip if the PDFs are not checked out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from h2bank.config import load_config
from h2bank.split import (
    Candidate,
    Line,
    find_candidates,
    longest_increasing_chain,
    snap_to_modal_gutter,
    split_pdf,
)

CFG = load_config()
PDF_DIR = CFG.pdf_dir


def line(page: int, x0: float, text: str, top: float = 0.0) -> Line:
    return Line(page=page, top=top, x0=x0, text=text)


def cand(n: int, x0: float, idx: int = 0) -> Candidate:
    return Candidate(q_number=n, line_index=idx, page=1, x0=x0, text=str(n))


# --------------------------------------------------------------------------
# candidate detection
# --------------------------------------------------------------------------

def test_gutter_rejects_centred_cover_page_text() -> None:
    """'3 hours' on a cover page is centred, so it is not a question."""
    lines = [
        line(1, 470.7, "3 Sept 2025"),
        line(1, 66.6, "Paper 1 3 hours"),
        line(2, 56.6, "1 The curve C has equation y = x + 1"),
    ]
    got = find_candidates(lines, gutter_max_x0=120)
    assert [c.q_number for c in got] == [1]


def test_bare_number_line_is_a_candidate() -> None:
    """A question opening with a diagram puts the number on its own line."""
    lines = [
        line(2, 56.6, "3"),
        line(2, 186.1, "Top of ladder"),
        line(2, 91.2, "A ladder of length 3.12 m is sliding down a wall. [4]"),
    ]
    got = find_candidates(lines, gutter_max_x0=120)
    assert [c.q_number for c in got] == [3]


def test_bare_number_outside_gutter_is_a_page_number() -> None:
    lines = [line(3, 294.6, "3")]
    assert find_candidates(lines, gutter_max_x0=120) == []


def test_short_fragment_after_number_is_rejected() -> None:
    """Stray inline fragments like '2 ,' must not open a question."""
    lines = [line(3, 100.0, "2 ,")]
    assert find_candidates(lines, gutter_max_x0=120) == []


# --------------------------------------------------------------------------
# modal gutter snapping
# --------------------------------------------------------------------------

def test_modal_gutter_drops_prose_number_at_an_indent() -> None:
    """Regression: HCI 2025 P1 wraps '...subdivided into 5 vertical strips...'
    so a literal '5' starts a line at x0=99 while questions sit at x0=72."""
    cands = [cand(n, 72.02, i) for i, n in enumerate([1, 2, 3, 4])]
    impostor = cand(5, 99.02, 10)
    real_five = cand(5, 72.02, 20)
    kept = snap_to_modal_gutter([*cands, impostor, real_five], tolerance=6.0)
    assert impostor not in kept
    assert real_five in kept


def test_modal_gutter_keeps_normal_margin_wander() -> None:
    """Real numbers drift a few points; 4.56pt must survive."""
    cands = [cand(n, 72.02, i) for i, n in enumerate([1, 3, 4, 5, 6])]
    wobbly = cand(2, 67.46, 99)
    assert wobbly in snap_to_modal_gutter([*cands, wobbly], tolerance=6.0)


def test_modal_gutter_noop_for_tiny_candidate_sets() -> None:
    cands = [cand(1, 72.0), cand(2, 99.0)]
    assert snap_to_modal_gutter(cands, tolerance=6.0) == cands


# --------------------------------------------------------------------------
# chain building
# --------------------------------------------------------------------------

def test_chain_prefers_the_longest_run() -> None:
    cands = [cand(7, 72, 0), cand(1, 72, 1), cand(2, 72, 2), cand(3, 72, 3)]
    chain, gaps = longest_increasing_chain(cands)
    assert [c.q_number for c in chain] == [1, 2, 3]
    assert gaps == 0


def test_chain_tolerates_one_missing_number() -> None:
    """A number extraction missed must not truncate the rest of the paper."""
    cands = [cand(n, 72, i) for i, n in enumerate([1, 2, 4, 5, 6])]
    chain, gaps = longest_increasing_chain(cands)
    assert [c.q_number for c in chain] == [1, 2, 4, 5, 6]
    assert gaps == 1


def test_chain_may_start_at_question_two() -> None:
    cands = [cand(n, 72, i) for i, n in enumerate([2, 3, 4])]
    chain, gaps = longest_increasing_chain(cands)
    assert [c.q_number for c in chain] == [2, 3, 4]
    assert gaps == 1


def test_chain_rejects_repeats_and_decreases() -> None:
    cands = [cand(n, 72, i) for i, n in enumerate([1, 2, 2, 1, 3])]
    chain, _ = longest_increasing_chain(cands)
    assert [c.q_number for c in chain] == [1, 2, 3]


def test_empty_candidates() -> None:
    assert longest_increasing_chain([]) == ([], 0)


# --------------------------------------------------------------------------
# pinned real-paper splits
# --------------------------------------------------------------------------

# (filename, question count, marks total, minimum confidence)
PINNED = [
    ("2025_ACJC_prelim_1_qp.pdf", 12, 100, 0.99),
    ("2025_EJC_prelim_2_qp.pdf", 10, 100, 0.99),
    ("2025_HCI_prelim_1_qp.pdf", 13, 100, 0.99),
    ("2025_JPJC_prelim_1_qp.pdf", 12, 100, 0.95),
    ("2025_MI_prelim_2_qp.pdf", 12, 100, 0.99),
]


@pytest.mark.parametrize("name, n_questions, marks, min_conf", PINNED)
def test_pinned_split(name: str, n_questions: int, marks: int, min_conf: float) -> None:
    pdf = PDF_DIR / name
    if not pdf.is_file():
        pytest.skip(f"{name} not present")

    result = split_pdf(pdf, CFG.split)

    assert len(result.questions) == n_questions, (
        f"{name}: expected {n_questions} questions, got {len(result.questions)}"
    )
    # Numbering must be exactly 1..N with no gaps for these papers.
    assert [q["q_number"] for q in result.questions] == list(range(1, n_questions + 1))
    assert result.marks_total == marks
    assert result.confidence >= min_conf
    # Every question must carry text and land on a real page, in order.
    last_page = 0
    for q in result.questions:
        assert q["full_text"].strip(), f"{name} Q{q['q_number']} has no text"
        assert 1 <= q["page_start"] <= q["page_end"] <= result.pages
        assert q["page_start"] >= last_page
        last_page = q["page_start"]


def test_every_pinned_paper_exists() -> None:
    """Guards against the pilot corpus silently disappearing from the repo."""
    missing = [name for name, *_ in PINNED if not (PDF_DIR / name).is_file()]
    if missing and len(missing) == len(PINNED):
        pytest.skip("no PDFs checked out")
    assert not missing, f"pinned papers missing: {missing}"


def test_jpjc_q3_opens_with_a_diagram() -> None:
    """The bare-number case, pinned on the paper that exposed it."""
    pdf = PDF_DIR / "2025_JPJC_prelim_1_qp.pdf"
    if not pdf.is_file():
        pytest.skip("JPJC P1 not present")
    result = split_pdf(pdf, CFG.split)
    q3 = next(q for q in result.questions if q["q_number"] == 3)
    assert "ladder" in q3["full_text"].lower()
    assert q3["marks_total"] == 4
