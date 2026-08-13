"""Topic tagging: `python -m h2bank.tag`.

Pass 1 - rules: regex/keyword map (see `topics_rules.py`).
Pass 2 - LLM: questions with zero rule hits, or with rule hits so diffuse they
conflict, go to `claude-haiku-4-5` for a strict-JSON verdict validated against
the `topics` table. Responses are cached by question text SHA-256, so re-runs
cost nothing and the cache doubles as the token-spend ledger.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections import Counter
from typing import Any

from .config import Config, load_config
from .db import (
    cache_get,
    cache_put,
    cache_token_totals,
    init_db,
    set_question_topics,
    topic_codes,
)
from .topics_rules import match_topics, rule_confidence

log = logging.getLogger("h2bank.tag")

# More than this many rule topics on one question means the keywords are
# fighting each other; hand it to the model instead.
MAX_RULE_TOPICS = 3

SYSTEM_PROMPT = (
    "You classify Singapore A-Level H2 Mathematics (syllabus 9758) exam "
    "questions by syllabus topic. Reply with JSON only - no prose, no code "
    "fences."
)

USER_TEMPLATE = """Classify this H2 Maths exam question into 1-3 syllabus topics.

Valid topic codes (use these exact codes and nothing else):
{topic_list}

Question text:
---
{question}
---

Reply with exactly this JSON shape:
{{"topic_codes": ["CODE", ...], "confidence": 0.0}}

confidence is your overall confidence in the classification, 0 to 1."""


# --------------------------------------------------------------------------
# pass 1
# --------------------------------------------------------------------------

def rule_tags(text: str) -> tuple[list[tuple[str, float, str]], str]:
    """Return (tags, reason). reason is '' when the rules are trusted."""
    hits = match_topics(text)
    if not hits:
        return [], "no rule hits"
    if len(hits) > MAX_RULE_TOPICS:
        return [], f"conflicting rule hits ({len(hits)} topics: {','.join(sorted(hits))})"
    return [(code, rule_confidence(n), "rule") for code, n in hits.items()], ""


# --------------------------------------------------------------------------
# pass 2
# --------------------------------------------------------------------------

class LLMTagger:
    def __init__(self, model: str, valid: set[str], max_tokens: int = 300) -> None:
        self.model = model
        self.valid = valid
        self.max_tokens = max_tokens
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self._client = None
        self._topic_list = ""

    def topic_list(self, conn) -> str:
        if not self._topic_list:
            rows = conn.execute(
                "SELECT code, name, strand FROM topics ORDER BY strand, code"
            ).fetchall()
            self._topic_list = "\n".join(
                f"  {r['code']} - {r['name']} ({r['strand']})" for r in rows
            )
        return self._topic_list

    @property
    def client(self):
        if self._client is None:
            import anthropic

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def classify(self, conn, text: str) -> tuple[dict[str, Any] | None, int, int]:
        """Classify one question.

        Returns (result, input_tokens, output_tokens) for **this question only**.
        The per-question counts matter: they are what gets cached, and caching a
        running total made the spend report sum to ~11x the real cost.
        """
        from tenacity import retry, stop_after_attempt, wait_exponential

        prompt = USER_TEMPLATE.format(
            topic_list=self.topic_list(conn), question=text[:4000]
        )

        @retry(reraise=True, stop=stop_after_attempt(3),
               wait=wait_exponential(multiplier=2, min=2, max=30))
        def _call(extra: str = "") -> Any:
            return self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt + extra}],
            )

        call_in = call_out = 0
        for attempt, extra in enumerate(
            ("", "\n\nYour previous reply was not valid JSON. Reply with JSON only.")
        ):
            resp = _call(extra)
            self.calls += 1
            call_in += resp.usage.input_tokens
            call_out += resp.usage.output_tokens
            self.input_tokens += resp.usage.input_tokens
            self.output_tokens += resp.usage.output_tokens
            body = "".join(
                block.text for block in resp.content if getattr(block, "type", "") == "text"
            )
            parsed = self._validate(body)
            if parsed is not None:
                return parsed, call_in, call_out
            log.warning("unparseable LLM reply (attempt %d): %s", attempt + 1, body[:200])
        return None, call_in, call_out

    def _validate(self, body: str) -> dict[str, Any] | None:
        m = re.search(r"\{.*\}", body, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        codes_raw = data.get("topic_codes")
        if not isinstance(codes_raw, list):
            return None
        codes = [c for c in codes_raw if isinstance(c, str) and c.upper() in self.valid]
        codes = list(dict.fromkeys(c.upper() for c in codes))
        if not codes:
            return None
        try:
            conf = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        return {"topic_codes": codes[:3], "confidence": max(0.0, min(1.0, conf))}


# --------------------------------------------------------------------------
# stage
# --------------------------------------------------------------------------

def run(cfg: Config, conn, use_llm: bool = True) -> dict[str, Any]:
    valid = topic_codes(conn)
    model = cfg.tag["model"]
    tagger = LLMTagger(model, valid, int(cfg.tag.get("max_output_tokens", 300)))

    rows = conn.execute(
        "SELECT id, full_text, text_sha256 FROM questions ORDER BY paper_id, q_number"
    ).fetchall()

    stats = Counter()
    llm_reasons = Counter()
    untagged: list[int] = []

    for row in rows:
        qid, text, sha = row["id"], row["full_text"], row["text_sha256"]
        tags, reason = rule_tags(text)

        if tags:
            set_question_topics(conn, qid, tags)
            stats["rule"] += 1
            continue

        llm_reasons[reason] += 1
        cached = cache_get(conn, sha, model)
        if cached is not None:
            stats["llm_cached"] += 1
        elif not use_llm:
            stats["skipped_no_llm"] += 1
            untagged.append(qid)
            continue
        else:
            try:
                cached, call_in, call_out = tagger.classify(conn, text)
            except Exception as exc:
                log.error("LLM call failed for question %d: %s: %s",
                          qid, type(exc).__name__, exc)
                stats["llm_error"] += 1
                untagged.append(qid)
                continue
            if cached is None:
                stats["llm_unusable"] += 1
                untagged.append(qid)
                continue
            cache_put(conn, sha, model, cached, call_in, call_out)
            stats["llm_new"] += 1

        conf = float(cached["confidence"])
        set_question_topics(
            conn, qid, [(code, conf, "llm") for code in cached["topic_codes"]]
        )

    in_tok, out_tok = cache_token_totals(conn, model)
    return {
        "questions": len(rows),
        "stats": dict(stats),
        "llm_reasons": dict(llm_reasons),
        "untagged": untagged,
        "model": model,
        "calls": tagger.calls,
        "input_tokens": tagger.input_tokens,
        "output_tokens": tagger.output_tokens,
        "cached_input_tokens": in_tok,
        "cached_output_tokens": out_tok,
    }


def print_report(conn, report: dict[str, Any]) -> None:
    print(f"\nquestions processed: {report['questions']}")
    print("outcomes:", ", ".join(f"{k}={v}" for k, v in sorted(report["stats"].items())))
    if report["llm_reasons"]:
        print("sent to LLM because:",
              ", ".join(f"{k} ({v})" for k, v in report["llm_reasons"].items()))

    print(f"\n{'topic':8} {'name':40} {'strand':6} questions")
    print("-" * 72)
    rows = conn.execute(
        """SELECT t.code, t.name, t.strand, COUNT(qt.question_id) AS n
           FROM topics t LEFT JOIN question_topics qt ON qt.topic_code = t.code
           GROUP BY t.code ORDER BY t.strand, n DESC, t.code"""
    ).fetchall()
    for r in rows:
        print(f"{r['code']:8} {r['name'][:40]:40} {r['strand']:6} {r['n']}")

    n_untagged = conn.execute(
        """SELECT COUNT(*) AS c FROM questions q
           WHERE NOT EXISTS (SELECT 1 FROM question_topics qt WHERE qt.question_id = q.id)"""
    ).fetchone()["c"]
    print(f"\nquestions with no topic tag: {n_untagged}")

    print(f"\nmodel: {report['model']}  calls this run: {report['calls']}")
    print(f"tokens this run:   in={report['input_tokens']} out={report['output_tokens']}")
    print(f"tokens in cache:   in={report['cached_input_tokens']} "
          f"out={report['cached_output_tokens']}")
    # claude-haiku-4-5 list price: $1/MTok input, $5/MTok output.
    cost = (report["cached_input_tokens"] / 1e6) * 1.0 + (
        report["cached_output_tokens"] / 1e6
    ) * 5.0
    print(f"estimated spend (haiku-4.5 list price): ${cost:.4f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tag questions with syllabus topics")
    parser.add_argument("--no-llm", action="store_true",
                        help="rules only; leaves ambiguous questions untagged")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config()
    conn = init_db(cfg.db_path, cfg.schema_path)
    use_llm = bool(cfg.tag.get("llm_enabled", True)) and not args.no_llm
    report = run(cfg, conn, use_llm=use_llm)
    print_report(conn, report)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
