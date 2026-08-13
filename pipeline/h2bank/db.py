"""SQLite helpers. All writers are upserts so stages can be re-run safely."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path, schema_path: Path) -> sqlite3.Connection:
    """Create/refresh the schema. Safe to call repeatedly."""
    conn = connect(db_path)
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# papers
# --------------------------------------------------------------------------

def find_paper_by_sha(conn: sqlite3.Connection, sha: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM papers WHERE file_sha256 = ?", (sha,)
    ).fetchone()


def find_paper_by_url(conn: sqlite3.Connection, source: str, url: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM papers WHERE source = ? AND source_url = ?", (source, url)
    ).fetchone()


def upsert_paper(conn: sqlite3.Connection, **fields: Any) -> int:
    """Insert a paper row, or return the id of the existing identical file.

    Dedupe is by file_sha256 first (identical bytes from any URL) and then by
    (source, source_url).
    """
    existing = find_paper_by_sha(conn, fields["file_sha256"])
    if existing is None:
        existing = find_paper_by_url(conn, fields["source"], fields["source_url"])
    if existing is not None:
        conn.execute(
            """UPDATE papers SET school = ?, school_code = ?, year = ?,
                   exam_type = ?, paper_no = ?, doc_type = ?, rel_path = ?,
                   pages = ?, size_bytes = ?
               WHERE id = ?""",
            (
                fields["school"], fields["school_code"], fields["year"],
                fields["exam_type"], fields["paper_no"], fields["doc_type"],
                fields["rel_path"], fields.get("pages"), fields.get("size_bytes"),
                existing["id"],
            ),
        )
        conn.commit()
        return int(existing["id"])

    cur = conn.execute(
        """INSERT INTO papers (source, source_url, school, school_code, year,
                exam_type, paper_no, doc_type, file_sha256, rel_path, pages,
                size_bytes, downloaded_at, parse_status, parse_notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fields["source"], fields["source_url"], fields["school"],
            fields["school_code"], fields["year"], fields["exam_type"],
            fields["paper_no"], fields["doc_type"], fields["file_sha256"],
            fields["rel_path"], fields.get("pages"), fields.get("size_bytes"),
            fields.get("downloaded_at") or utc_now(),
            fields.get("parse_status", "pending"), fields.get("parse_notes"),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def set_parse_status(
    conn: sqlite3.Connection, paper_id: int, status: str, notes: str | None = None
) -> None:
    conn.execute(
        "UPDATE papers SET parse_status = ?, parse_notes = ? WHERE id = ?",
        (status, notes, paper_id),
    )
    conn.commit()


# --------------------------------------------------------------------------
# questions
# --------------------------------------------------------------------------

def replace_questions(
    conn: sqlite3.Connection, paper_id: int, questions: Iterable[dict[str, Any]]
) -> int:
    """Replace the whole question set for a paper (idempotent re-split).

    Topic tags are keyed on question id, so they are dropped by the cascade and
    rebuilt by the tag stage.
    """
    conn.execute("DELETE FROM questions WHERE paper_id = ?", (paper_id,))
    rows = 0
    for q in questions:
        conn.execute(
            """INSERT INTO questions (paper_id, q_number, part_labels,
                   page_start, page_end, marks_total, full_text, needs_ocr,
                   extract_confidence, text_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                paper_id, q["q_number"], json.dumps(q.get("part_labels", [])),
                q.get("page_start"), q.get("page_end"), q.get("marks_total"),
                q["full_text"], 1 if q.get("needs_ocr") else 0,
                q.get("extract_confidence"), q["text_sha256"],
            ),
        )
        rows += 1
    conn.commit()
    return rows


# --------------------------------------------------------------------------
# topics
# --------------------------------------------------------------------------

def topic_codes(conn: sqlite3.Connection) -> set[str]:
    return {r["code"] for r in conn.execute("SELECT code FROM topics")}


def set_question_topics(
    conn: sqlite3.Connection,
    question_id: int,
    tags: Iterable[tuple[str, float, str]],
) -> int:
    """Replace the tag set for one question. tags = (topic_code, conf, method)."""
    conn.execute("DELETE FROM question_topics WHERE question_id = ?", (question_id,))
    n = 0
    for code, conf, method in tags:
        conn.execute(
            """INSERT INTO question_topics (question_id, topic_code, confidence, method)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (question_id, topic_code) DO UPDATE SET
                   confidence = excluded.confidence, method = excluded.method""",
            (question_id, code, conf, method),
        )
        n += 1
    conn.commit()
    return n


# --------------------------------------------------------------------------
# crawl_log / llm_cache
# --------------------------------------------------------------------------

def log_crawl(
    conn: sqlite3.Connection,
    source: str,
    urls_seen: int,
    new_papers: int,
    errors: list[str] | None = None,
) -> None:
    conn.execute(
        """INSERT INTO crawl_log (run_at, source, urls_seen, new_papers, errors)
           VALUES (?, ?, ?, ?, ?)""",
        (utc_now(), source, urls_seen, new_papers, json.dumps(errors or [])),
    )
    conn.commit()


def cache_get(conn: sqlite3.Connection, sha: str, model: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT response_json FROM llm_cache WHERE text_sha256 = ? AND model = ?",
        (sha, model),
    ).fetchone()
    return json.loads(row["response_json"]) if row else None


def cache_put(
    conn: sqlite3.Connection,
    sha: str,
    model: str,
    response: dict[str, Any],
    input_tokens: int,
    output_tokens: int,
) -> None:
    conn.execute(
        """INSERT INTO llm_cache (text_sha256, model, response_json,
               input_tokens, output_tokens, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (text_sha256, model) DO UPDATE SET
               response_json = excluded.response_json,
               input_tokens = excluded.input_tokens,
               output_tokens = excluded.output_tokens""",
        (sha, model, json.dumps(response), input_tokens, output_tokens, utc_now()),
    )
    conn.commit()


def cache_token_totals(conn: sqlite3.Connection, model: str) -> tuple[int, int]:
    row = conn.execute(
        """SELECT COALESCE(SUM(input_tokens), 0) AS i,
                  COALESCE(SUM(output_tokens), 0) AS o
           FROM llm_cache WHERE model = ?""",
        (model,),
    ).fetchone()
    return int(row["i"]), int(row["o"])
