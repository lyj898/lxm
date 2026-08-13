-- H2 Math question bank schema (SQLite).
-- Every stage is idempotent: re-running must not duplicate rows, so the
-- natural keys below carry UNIQUE constraints and writers use upserts.

PRAGMA foreign_keys = ON;

-- One row per downloaded PDF file. A "paper" in the crawl-cap sense is a
-- (school_code, year, exam_type, paper_no) group, which may have both a
-- qp and an ms row here.
CREATE TABLE IF NOT EXISTS papers (
    id            INTEGER PRIMARY KEY,
    source        TEXT    NOT NULL,          -- adapter name, e.g. 'freetestpaper'
    source_url    TEXT    NOT NULL,          -- direct URL the file came from
    school        TEXT    NOT NULL,
    school_code   TEXT    NOT NULL,          -- short slug, e.g. 'ACJC'
    year          INTEGER NOT NULL,
    exam_type     TEXT    NOT NULL,          -- 'prelim', 'promo', 'mye', ...
    paper_no      INTEGER,                   -- 1 or 2; NULL if not applicable
    doc_type      TEXT    NOT NULL CHECK (doc_type IN ('qp', 'ms', 'combined')),
    file_sha256   TEXT    NOT NULL,
    rel_path      TEXT    NOT NULL,          -- repo-relative, e.g. data/pdfs/...
    pages         INTEGER,
    size_bytes    INTEGER,
    downloaded_at TEXT    NOT NULL,          -- ISO-8601 UTC
    parse_status  TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (parse_status IN ('pending', 'ok', 'partial', 'failed', 'skipped')),
    parse_notes   TEXT
);

-- Dedupe keys: identical bytes are never stored twice, and a given source URL
-- is registered at most once.
CREATE UNIQUE INDEX IF NOT EXISTS ux_papers_sha       ON papers (file_sha256);
CREATE UNIQUE INDEX IF NOT EXISTS ux_papers_url       ON papers (source, source_url);
CREATE UNIQUE INDEX IF NOT EXISTS ux_papers_rel_path  ON papers (rel_path);
CREATE INDEX        IF NOT EXISTS ix_papers_group     ON papers (school_code, year, exam_type, paper_no);

CREATE TABLE IF NOT EXISTS questions (
    id                 INTEGER PRIMARY KEY,
    paper_id           INTEGER NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    q_number           INTEGER NOT NULL,
    part_labels        TEXT,                 -- JSON array, e.g. ["(i)","(ii)"]
    page_start         INTEGER,              -- 1-based page index in the PDF
    page_end           INTEGER,
    -- Crop box in PDF points from the top of the page, used by the site to
    -- render the question straight from the PDF (diagrams and typeset maths
    -- survive; extracted text cannot carry them).
    y_top              REAL,
    y_bottom           REAL,
    marks_total        INTEGER,
    full_text          TEXT NOT NULL,
    needs_ocr          INTEGER NOT NULL DEFAULT 0 CHECK (needs_ocr IN (0, 1)),
    extract_confidence REAL,                 -- 0..1, paper-level score copied per question
    text_sha256        TEXT NOT NULL         -- cache key for LLM tagging
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_questions_paper_q ON questions (paper_id, q_number);
CREATE INDEX        IF NOT EXISTS ix_questions_sha     ON questions (text_sha256);

CREATE TABLE IF NOT EXISTS topics (
    code   TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    strand TEXT NOT NULL CHECK (strand IN ('pure', 'stats'))
);

CREATE TABLE IF NOT EXISTS question_topics (
    question_id INTEGER NOT NULL REFERENCES questions (id) ON DELETE CASCADE,
    topic_code  TEXT    NOT NULL REFERENCES topics (code),
    confidence  REAL    NOT NULL DEFAULT 1.0,
    method      TEXT    NOT NULL CHECK (method IN ('rule', 'llm')),
    PRIMARY KEY (question_id, topic_code)
);

CREATE INDEX IF NOT EXISTS ix_question_topics_topic ON question_topics (topic_code);

CREATE TABLE IF NOT EXISTS crawl_log (
    id         INTEGER PRIMARY KEY,
    run_at     TEXT    NOT NULL,             -- ISO-8601 UTC
    source     TEXT    NOT NULL,
    urls_seen  INTEGER NOT NULL DEFAULT 0,
    new_papers INTEGER NOT NULL DEFAULT 0,
    errors     TEXT                          -- JSON array of error strings
);

-- Response cache for the LLM tagging pass, keyed by question text hash so that
-- re-running the stage costs nothing. Token counts double as the spend report.
CREATE TABLE IF NOT EXISTS llm_cache (
    text_sha256   TEXT NOT NULL,
    model         TEXT NOT NULL,
    response_json TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (text_sha256, model)
);

-- 9758 syllabus topics.
INSERT INTO topics (code, name, strand) VALUES
    ('FUNC',   'Functions',                             'pure'),
    ('GRAPH',  'Graphs and transformations',            'pure'),
    ('EQIN',   'Equations and inequalities',            'pure'),
    ('SEQS',   'Sequences and series (incl. AP/GP)',    'pure'),
    ('VEC',    'Vectors',                               'pure'),
    ('CPLX',   'Complex numbers',                       'pure'),
    ('DIFF',   'Differentiation and applications',      'pure'),
    ('MACL',   'Maclaurin series',                      'pure'),
    ('INTT',   'Integration techniques',                'pure'),
    ('DEFI',   'Definite integrals, areas and volumes', 'pure'),
    ('DIFFEQ', 'Differential equations',                'pure'),
    ('PNC',    'Permutations and combinations',         'stats'),
    ('PROB',   'Probability',                           'stats'),
    ('DRV',    'Discrete random variables',             'stats'),
    ('BINOM',  'Binomial distribution',                 'stats'),
    ('NORM',   'Normal distribution',                   'stats'),
    ('SAMP',   'Sampling and the Central Limit Theorem','stats'),
    ('HYPO',   'Hypothesis testing',                    'stats'),
    ('CORR',   'Correlation and linear regression',     'stats')
ON CONFLICT (code) DO UPDATE SET name = excluded.name, strand = excluded.strand;
