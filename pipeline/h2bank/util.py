"""Shared helpers: hashing, school-code normalisation, filename conventions."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

# Canonical short codes for Singapore JCs / MI. Keys are lowercase substrings
# matched against the source page text or URL, longest match wins.
SCHOOL_CODES: dict[str, tuple[str, str]] = {
    "anderson serangoon": ("ASRJC", "Anderson Serangoon Junior College"),
    "anglo-chinese": ("ACJC", "Anglo-Chinese Junior College"),
    "anglo chinese": ("ACJC", "Anglo-Chinese Junior College"),
    "catholic junior": ("CJC", "Catholic Junior College"),
    "dunman high": ("DHS", "Dunman High School"),
    "eunoia": ("EJC", "Eunoia Junior College"),
    "hwa chong": ("HCI", "Hwa Chong Institution"),
    "jurong pioneer": ("JPJC", "Jurong Pioneer Junior College"),
    "millennia": ("MI", "Millennia Institute"),
    "nanyang junior": ("NYJC", "Nanyang Junior College"),
    "national junior": ("NJC", "National Junior College"),
    "ngee ann": ("NAP", "Ngee Ann Polytechnic"),
    "raffles institution": ("RI", "Raffles Institution"),
    "river valley": ("RVHS", "River Valley High School"),
    "st andrew": ("SAJC", "St Andrew's Junior College"),
    "st. andrew": ("SAJC", "St Andrew's Junior College"),
    "saint andrew": ("SAJC", "St Andrew's Junior College"),
    "st joseph": ("SJI", "St Joseph's Institution"),
    "singapore chinese girls": ("SCGS", "Singapore Chinese Girls' School"),
    "tampines meridian": ("TMJC", "Tampines Meridian Junior College"),
    "temasek junior": ("TJC", "Temasek Junior College"),
    "victoria junior": ("VJC", "Victoria Junior College"),
    "yishun innova": ("YIJC", "Yishun Innova Junior College"),
    "nus high": ("NUSH", "NUS High School of Math and Science"),
    "asrjc": ("ASRJC", "Anderson Serangoon Junior College"),
    "acjc": ("ACJC", "Anglo-Chinese Junior College"),
    "cjc": ("CJC", "Catholic Junior College"),
    "dhs": ("DHS", "Dunman High School"),
    "ejc": ("EJC", "Eunoia Junior College"),
    "hci": ("HCI", "Hwa Chong Institution"),
    "jpjc": ("JPJC", "Jurong Pioneer Junior College"),
    "nyjc": ("NYJC", "Nanyang Junior College"),
    "njc": ("NJC", "National Junior College"),
    "rvhs": ("RVHS", "River Valley High School"),
    "sajc": ("SAJC", "St Andrew's Junior College"),
    "sji": ("SJI", "St Joseph's Institution"),
    "tmjc": ("TMJC", "Tampines Meridian Junior College"),
    "tjc": ("TJC", "Temasek Junior College"),
    "vjc": ("VJC", "Victoria Junior College"),
    "yijc": ("YIJC", "Yishun Innova Junior College"),
    "ri": ("RI", "Raffles Institution"),
    "mi": ("MI", "Millennia Institute"),
    # Short forms used by testpapersfree listing titles ("... Prelim - Anderson").
    "anderson": ("ASRJC", "Anderson Serangoon Junior College"),
    "catholic": ("CJC", "Catholic Junior College"),
    "dunman": ("DHS", "Dunman High School"),
    "jurong": ("JPJC", "Jurong Pioneer Junior College"),
    "nanyang": ("NYJC", "Nanyang Junior College"),
    "national": ("NJC", "National Junior College"),
    "raffles": ("RI", "Raffles Institution"),
    "tampines": ("TMJC", "Tampines Meridian Junior College"),
    "temasek": ("TJC", "Temasek Junior College"),
    "victoria": ("VJC", "Victoria Junior College"),
    "yishun": ("YIJC", "Yishun Innova Junior College"),
}

# Codes short enough to produce false positives; require a word boundary.
_SHORT_CODES = {"ri", "mi", "cjc", "njc", "sji", "tjc", "vjc", "dhs", "ejc", "hci"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalise(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace and punctuation runs."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[_\-+%]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def detect_school(*texts: str) -> tuple[str, str]:
    """Return (school_code, school_name); ('UNK', 'Unknown') when no match."""
    blob = normalise(" ".join(t for t in texts if t))
    best: tuple[int, str, str] = (0, "UNK", "Unknown")
    for needle, (code, name) in SCHOOL_CODES.items():
        if needle in _SHORT_CODES:
            hit = re.search(rf"(?<![a-z]){re.escape(needle)}(?![a-z])", blob)
        else:
            hit = needle in blob
        if hit and len(needle) > best[0]:
            best = (len(needle), code, name)
    return best[1], best[2]


def detect_year(*texts: str, lo: int = 2010, hi: int = 2030) -> int | None:
    years = [int(m) for t in texts if t for m in re.findall(r"(?<!\d)(20\d{2})(?!\d)", t)]
    years = [y for y in years if lo <= y <= hi]
    return max(years) if years else None


def detect_paper_no(*texts: str) -> int | None:
    blob = normalise(" ".join(t for t in texts if t))
    m = re.search(r"\b(?:paper|p)\s*([12])\b", blob)
    if m:
        return int(m.group(1))
    m = re.search(r"\b([12])\s*(?:qp|ms)\b", blob)
    return int(m.group(1)) if m else None


def detect_doc_type(*texts: str) -> str:
    """'ms' for answers/solutions, 'combined' when one file holds both."""
    flat = re.sub(r"[^a-z0-9]+", " ", normalise(" ".join(t for t in texts if t)))
    blob = f" {flat} "
    ms_markers = (
        "answer", "solution", "soln", "marking scheme", "markscheme",
        "mark scheme", " ms ", "worked", "markers report", "marker report",
    )
    # Deliberately narrow: " paper " appears in solution filenames too, so only
    # explicit question-paper words count.
    qp_markers = (" qp ", "question", " qns ")
    has_ms = any(m in blob for m in ms_markers)
    has_qp = any(m in blob for m in qp_markers)
    if has_ms and has_qp:
        return "combined"
    return "ms" if has_ms else "qp"


def detect_exam_type(*texts: str) -> str:
    blob = normalise(" ".join(t for t in texts if t))
    if "prelim" in blob or "preliminary" in blob:
        return "prelim"
    if "promo" in blob:
        return "promo"
    if "mid year" in blob or "midyear" in blob or "mye" in blob:
        return "mye"
    if "common test" in blob or "ct" == blob.strip():
        return "ct"
    return "prelim"


def pdf_filename(
    year: int, school_code: str, exam_type: str, paper_no: int | None, doc_type: str
) -> str:
    """`{year}_{school_code}_{exam}_{paper_no}_{qp|ms}.pdf`."""
    pno = str(paper_no) if paper_no is not None else "x"
    return f"{year}_{school_code}_{exam_type}_{pno}_{doc_type}.pdf"


def paper_key(school_code: str, year: int, exam_type: str, paper_no: int | None) -> str:
    """Identity of a 'paper' for the download cap: qp + ms share one key."""
    return f"{school_code}|{year}|{exam_type}|{paper_no if paper_no is not None else 'x'}"
