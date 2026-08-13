"""Unit tests for metadata detection and filename conventions."""

from __future__ import annotations

import pytest

from h2bank.fetcher import decode_html
from h2bank.util import (
    detect_doc_type,
    detect_exam_type,
    detect_paper_no,
    detect_school,
    detect_year,
    paper_key,
    pdf_filename,
)


@pytest.mark.parametrize(
    "text, code",
    [
        ("y25_jc2_Maths_h2_prelim_ACJC.html", "ACJC"),
        ("JC2 Maths H2 2025 EJC", "EJC"),
        ("HCI_9758_2025_Prelim Paper 1.pdf", "HCI"),
        ("y25_jc2_Maths_h2_prelim_RI.html", "RI"),
        ("MI 9758 2025 Prelim P1.pdf", "MI"),
        ("JC2 H2-Maths 2025 Prelim - Anglo Chinese - Questions", "ACJC"),
        ("JC2 H2-Maths 2025 Prelim - Anderson - Answers", "ASRJC"),
        ("JC2 H2-Maths 2025 Prelim - Hwa Chong - Questions", "HCI"),
        ("something with no school in it", "UNK"),
    ],
)
def test_detect_school(text: str, code: str) -> None:
    assert detect_school(text)[0] == code


def test_detect_school_prefers_longest_match() -> None:
    # "Anderson Serangoon" must win over the bare "anderson" alias.
    assert detect_school("Anderson Serangoon Junior College")[0] == "ASRJC"


def test_short_codes_need_word_boundaries() -> None:
    # 'ri' inside 'prelim' must not be read as Raffles Institution.
    assert detect_school("2025 prelim paper")[0] == "UNK"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("ACJC 2025 JC2 H2 Prelim Paper 1 QP.pdf", "qp"),
        ("ACJC 2025 H2 Prelims Paper 1 Solutions and Markers' Report.pdf", "ms"),
        ("EJC_9758_2025_Prelim_P1.pdf", "qp"),
        ("EJC_9758_2025_Prelim_P1_Solutions.pdf", "ms"),
        ("2025 CJC JC2 H2 Math Prelim Paper 2 answer key.pdf", "ms"),
        ("2025 DHS H2 Math Prelim P2 Soln.pdf", "ms"),
        ("2025 RI Prelim+P2+Qns.pdf", "qp"),
        ("2025 YIJC Prelim H2 Maths Papers 1&2 (QP & Soln).pdf", "combined"),
    ],
)
def test_detect_doc_type(name: str, expected: str) -> None:
    assert detect_doc_type(name) == expected


def test_paper_word_alone_does_not_imply_question_paper() -> None:
    """Regression: ' paper ' appears in solution filenames too."""
    assert detect_doc_type("HCI_9758_2025_Prelim Paper 2_Solutions.pdf") == "ms"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("EJC_9758_2025_Prelim_P1.pdf", 1),
        ("HCI_9758_2025_Prelim Paper 2_Solutions.pdf", 2),
        ("ASRJC H2 Math Prelim Paper 2-1.pdf", 2),
        ("2025 YIJC Prelim H2 Maths Papers.pdf", None),
    ],
)
def test_detect_paper_no(name: str, expected: int | None) -> None:
    assert detect_paper_no(name) == expected


def test_detect_year_takes_the_latest_plausible_year() -> None:
    assert detect_year("JC2 Maths H2 2025 ACJC") == 2025
    assert detect_year("no year here") is None


def test_detect_exam_type() -> None:
    assert detect_exam_type("2025 Preliminary Examination") == "prelim"
    assert detect_exam_type("2025 Promotional Exam") == "promo"


def test_pdf_filename_convention() -> None:
    assert pdf_filename(2025, "EJC", "prelim", 1, "qp") == "2025_EJC_prelim_1_qp.pdf"
    assert pdf_filename(2025, "YIJC", "prelim", None, "combined") == (
        "2025_YIJC_prelim_x_combined.pdf"
    )


def test_paper_key_groups_qp_and_ms_together() -> None:
    assert paper_key("EJC", 2025, "prelim", 1) == paper_key("EJC", 2025, "prelim", 1)
    assert paper_key("EJC", 2025, "prelim", 1) != paper_key("EJC", 2025, "prelim", 2)


def test_decode_html_handles_utf16_bom() -> None:
    """sgtestpaper serves UTF-16; a blind utf-8 decode yields NUL garbage."""
    html = "<html><title>JC2</title></html>"
    assert decode_html(html.encode("utf-16")) == html
    assert decode_html(html.encode("utf-8")) == html
    assert decode_html(b"\xef\xbb\xbf" + html.encode("utf-8")) == html
