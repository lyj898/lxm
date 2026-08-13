"""Remote-zip support.

sgtestpaper ships each school's papers as one .zip. Reading the zip's central
directory over HTTP Range requests lets discovery report exactly which
qp/ms PDFs a school offers *before* committing to a full download, which keeps
the crawl inside its politeness budget.
"""

from __future__ import annotations

import io
import logging
import posixpath
import struct
import zipfile
from dataclasses import dataclass

from .fetcher import Fetcher

log = logging.getLogger(__name__)

_EOCD_SIG = b"PK\x05\x06"
_CD_SIG = b"PK\x01\x02"
_TAIL_BYTES = 66_000  # EOCD plus a max-length comment


@dataclass(frozen=True)
class ZipMember:
    name: str
    uncompressed_size: int

    @property
    def is_pdf(self) -> bool:
        return self.name.lower().endswith(".pdf")


class ZipPeekUnsupported(RuntimeError):
    """Server does not support Range requests, or the tail is not a zip."""


def peek_zip_members(fetcher: Fetcher, url: str) -> list[ZipMember]:
    """List a remote zip's members using two Range requests."""
    tail = _range_get(fetcher, url, f"bytes=-{_TAIL_BYTES}")
    idx = tail.rfind(_EOCD_SIG)
    if idx < 0:
        raise ZipPeekUnsupported(f"no end-of-central-directory found in {url}")

    # EOCD layout from idx: sig(4) disk(2) cd_disk(2) entries_here(2)
    # entries_total(2) cd_size(4) cd_offset(4)
    n_entries = struct.unpack("<H", tail[idx + 10 : idx + 12])[0]
    cd_size, cd_offset = struct.unpack("<II", tail[idx + 12 : idx + 20])
    if cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        raise ZipPeekUnsupported(f"zip64 central directory not supported for {url}")

    cd = _range_get(fetcher, url, f"bytes={cd_offset}-{cd_offset + cd_size - 1}")
    members: list[ZipMember] = []
    pos = 0
    while pos + 46 <= len(cd) and cd[pos : pos + 4] == _CD_SIG:
        uncompressed = struct.unpack("<I", cd[pos + 24 : pos + 28])[0]
        n_len, e_len, c_len = struct.unpack("<HHH", cd[pos + 28 : pos + 34])
        flags = struct.unpack("<H", cd[pos + 8 : pos + 10])[0]
        encoding = "utf-8" if flags & 0x800 else "cp437"
        name = cd[pos + 46 : pos + 46 + n_len].decode(encoding, errors="replace")
        members.append(ZipMember(name=name, uncompressed_size=uncompressed))
        pos += 46 + n_len + e_len + c_len

    if len(members) != n_entries:
        log.debug("%s: parsed %d of %d central-directory entries",
                  url, len(members), n_entries)
    return members


def _range_get(fetcher: Fetcher, url: str, rng: str) -> bytes:
    """A Range GET that reuses the Fetcher's robots check and rate limiting."""
    original = fetcher.session.headers.get("Range")
    fetcher.session.headers["Range"] = rng
    try:
        resp = fetcher.get_binary(url)
    finally:
        if original is None:
            fetcher.session.headers.pop("Range", None)
        else:
            fetcher.session.headers["Range"] = original
    if resp.status != 206:
        raise ZipPeekUnsupported(
            f"{url} answered HTTP {resp.status} for Range request (want 206)"
        )
    return resp.content


def safe_member_name(name: str) -> str | None:
    """Reject absolute paths, traversal and non-PDF members (zip-slip guard)."""
    if name.endswith("/"):
        return None
    normalised = name.replace("\\", "/")
    if normalised.startswith("/") or ":" in normalised.split("/")[0]:
        return None
    parts = [p for p in normalised.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    leaf = posixpath.basename("/".join(parts))
    if not leaf.lower().endswith(".pdf"):
        return None
    return leaf


def extract_pdf(zip_bytes: bytes, member_name: str) -> bytes:
    """Extract one PDF member. Only PDFs are ever read out of the archive."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        if safe_member_name(member_name) is None:
            raise ValueError(f"refusing unsafe zip member: {member_name!r}")
        with zf.open(member_name) as fh:
            data = fh.read()
    if not data.startswith(b"%PDF"):
        raise ValueError(f"{member_name!r} is not a PDF (bad magic bytes)")
    return data
