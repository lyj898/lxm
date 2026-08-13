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
    extract_lines,
    find_candidates,
    longest_increasing_chain,
    snap_to_modal_gutter,
    split_pdf,
    strip_boilerplate,
)

CFG = load_config()
PDF_DIR = CFG.pdf_dir


def line(
    page: int, x0: float, text: str, top: float = 0.0, bottom: float | None = None
) -> Line:
    return Line(
        page=page,
        top=top,
        x0=x0,
        text=text,
        bottom=top + 12 if bottom is None else bottom,
        page_height=842.0,
    )


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
# reading order and page furniture
# --------------------------------------------------------------------------

def test_extracted_lines_are_in_reading_order() -> None:
    """pdfplumber returns text-object order, which put a page footer between the
    first and second line of a question and scrambled every question body."""
    pdf = PDF_DIR / "2025_ACJC_prelim_1_qp.pdf"
    if not pdf.is_file():
        pytest.skip("ACJC P1 not present")
    lines, _, _ = extract_lines(pdf, 50)
    keys = [(l.page, round(l.top, 1)) for l in lines]
    assert keys == sorted(keys)


def test_repeated_running_footer_is_stripped() -> None:
    footer = "ANGLO-CHINESE JUNIOR COLLEGE 2025 H2 MATHEMATICS 9758/01"
    lines = [
        line(1, 72.0, "1 A curve has equation", top=100.0),
        line(1, 72.0, footer, top=798.0, bottom=806.0),
        line(2, 72.0, "2 Another question", top=100.0),
        line(2, 72.0, footer, top=798.0, bottom=806.0),
    ]
    kept, dropped = strip_boilerplate(lines, n_pages=2, gutter_max_x0=120.0)
    assert len(dropped) == 2
    assert all(footer not in l.text for l in kept)


def test_centred_page_number_is_stripped_but_gutter_question_number_is_not() -> None:
    """Regression: ACJC Q6 and HCI Q5 are bare numbers near the top of a page and
    were being deleted as page numbers."""
    lines = [
        line(2, 302.0, "2", top=37.0, bottom=49.0),    # centred page number
        line(3, 72.0, "6", top=60.0, bottom=72.0),     # question 6, in the gutter
        line(3, 100.0, "Figure 1 shows a tank", top=90.0),
    ]
    kept, _ = strip_boilerplate(lines, n_pages=3, gutter_max_x0=120.0)
    texts = [l.text for l in kept]
    assert "2" not in texts, "centred page number should be dropped"
    assert "6" in texts, "gutter question number must survive"


def test_question_number_matching_a_page_number_survives() -> None:
    """HCI's Q5 is a lone '5' in the gutter and page 5's page number is also '5';
    the repetition rule must not condemn the question."""
    lines = [
        line(2, 72.0, "5", top=62.0, bottom=74.0),
        line(2, 100.0, "The tourism board plans a tram track", top=90.0),
        line(5, 294.0, "5", top=37.0, bottom=49.0),
    ]
    kept, _ = strip_boilerplate(lines, n_pages=5, gutter_max_x0=120.0)
    gutter_fives = [l for l in kept if l.text == "5" and l.x0 < 120]
    assert gutter_fives, "the gutter '5' must survive"


def test_single_page_documents_are_left_alone() -> None:
    lines = [line(1, 72.0, "1 Only page", top=100.0)]
    kept, dropped = strip_boilerplate(lines, n_pages=1)
    assert kept == lines and dropped == []


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
    # Every question must carry text, land on a real page, and have a usable
    # crop box, in order.
    last_page = 0
    for q in result.questions:
        assert q["full_text"].strip(), f"{name} Q{q['q_number']} has no text"
        assert 1 <= q["page_start"] <= q["page_end"] <= result.pages
        assert q["page_start"] >= last_page
        last_page = q["page_start"]
        assert q["y_top"] is not None and q["y_bottom"] is not None
        assert q["y_top"] >= 0 and q["y_bottom"] > 0
        if q["page_start"] == q["page_end"]:
            # Same page: the box must enclose something.
            assert q["y_top"] < q["y_bottom"], (
                f"{name} Q{q['q_number']} has an empty crop box"
            )
        # Across pages the two edges live on different pages, so they do not
        # compare; the renderer slices each page separately.


@pytest.mark.parametrize("name, n_questions, marks, min_conf", PINNED)
def test_pinned_split_text_has_no_page_furniture(
    name: str, n_questions: int, marks: int, min_conf: float
) -> None:
    """Headers and footers were landing inside question text."""
    pdf = PDF_DIR / name
    if not pdf.is_file():
        pytest.skip(f"{name} not present")
    result = split_pdf(pdf, CFG.split)
    for q in result.questions:
        text = q["full_text"]
        for junk in ("JUNIOR COLLEGE", "Millennia Institute", "Turn over", "9758/0"):
            assert junk not in text, (
                f"{name} Q{q['q_number']} still contains page furniture: {junk!r}"
            )


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
