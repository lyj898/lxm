"""PDF size hygiene and metadata. Compression is best-effort, never fatal."""

from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)


def page_count(data: bytes) -> int | None:
    try:
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return doc.page_count
    except Exception as exc:
        log.warning("page count failed: %s: %s", type(exc).__name__, exc)
        return None


def compress(data: bytes, target_bytes: int = 2 * 1024 * 1024) -> tuple[bytes, str]:
    """Shrink a PDF losslessly. Returns (bytes, note).

    Tries pikepdf's stream recompression first, then pymupdf's garbage
    collection + deflate, and keeps whichever is smallest. Image downsampling is
    deliberately not attempted - it would degrade scanned papers, and the note
    records when the target could not be met so the caller can flag it.
    """
    original = len(data)
    best = data
    tried: list[str] = []

    try:
        import pikepdf

        buf = io.BytesIO()
        with pikepdf.open(io.BytesIO(data)) as pdf:
            pdf.save(
                buf,
                compress_streams=True,
                recompress_flate=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                linearize=False,
            )
        candidate = buf.getvalue()
        tried.append(f"pikepdf={len(candidate)}")
        if candidate.startswith(b"%PDF") and len(candidate) < len(best):
            best = candidate
    except Exception as exc:
        tried.append(f"pikepdf-failed({type(exc).__name__})")
        log.warning("pikepdf compression failed: %s: %s", type(exc).__name__, exc)

    try:
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as doc:
            candidate = doc.tobytes(garbage=4, deflate=True, clean=True)
        tried.append(f"pymupdf={len(candidate)}")
        if candidate.startswith(b"%PDF") and len(candidate) < len(best):
            best = candidate
    except Exception as exc:
        tried.append(f"pymupdf-failed({type(exc).__name__})")
        log.warning("pymupdf compression failed: %s: %s", type(exc).__name__, exc)

    saved = original - len(best)
    note = (
        f"compressed {original}->{len(best)} bytes "
        f"({saved * 100 // original if original else 0}% saved; {', '.join(tried)})"
    )
    if len(best) > target_bytes:
        note += f"; still above target {target_bytes}"
    return best, note
