"""Tagging tests: rule coverage, rule behaviour, and LLM output validation.

The LLM tests use no network - they exercise the JSON validator that guards
whatever the model returns.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from h2bank.config import load_config
from h2bank.db import init_db, topic_codes
from h2bank.tag import MAX_RULE_TOPICS, LLMTagger, rule_tags
from h2bank.topics_rules import RULES, match_topics, rule_confidence

CFG = load_config()


@pytest.fixture(scope="module")
def seeded_topics() -> set[str]:
    """Topic codes straight from schema.sql, in a throwaway in-memory database."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(CFG.schema_path.read_text(encoding="utf-8"))
    codes = topic_codes(conn)
    conn.close()
    return codes


# --------------------------------------------------------------------------
# rule coverage
# --------------------------------------------------------------------------

def test_every_seeded_topic_has_at_least_three_patterns(seeded_topics: set[str]) -> None:
    missing = sorted(seeded_topics - set(RULES))
    assert not missing, f"topics with no rules: {missing}"
    thin = {code: len(pats) for code, pats in RULES.items() if len(pats) < 3}
    assert not thin, f"topics with fewer than 3 patterns: {thin}"


def test_no_rule_targets_an_unknown_topic(seeded_topics: set[str]) -> None:
    unknown = sorted(set(RULES) - seeded_topics)
    assert not unknown, f"rules for topics not in the schema: {unknown}"


def test_all_patterns_compile() -> None:
    for code, patterns in RULES.items():
        for p in patterns:
            re.compile(p)  # raises on a bad pattern


# --------------------------------------------------------------------------
# rule behaviour
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("State the null hypothesis and find the test statistic at the 5% "
         "significance level.", "HYPO"),
        ("Sketch the locus of z on an Argand diagram.", "CPLX"),
        ("Calculate the product moment correlation coefficient and find the "
         "regression line.", "CORR"),
        ("Find dy/dx and hence the coordinates of the stationary point.", "DIFF"),
        ("Write down the first four terms of the Maclaurin series.", "MACL"),
        ("The points have position vectors a, p and q.", "VEC"),
        ("Using the substitution x = 4 tan u, evaluate the integral.", "INTT"),
        ("The random variable X follows a binomial distribution.", "BINOM"),
        ("Find the volume of the solid formed when R is rotated about the "
         "x-axis.", "DEFI"),
        ("Solve the inequality for real values of x.", "EQIN"),
    ],
)
def test_rules_fire_on_canonical_phrasing(text: str, expected: str) -> None:
    assert expected in match_topics(text)


def test_position_vectors_plural() -> None:
    """Regression: '\\bposition vector\\b' missed 'position vectors a, p and q'."""
    assert "VEC" in match_topics("the points A, P and Q have position vectors a, p, q")


def test_labelled_point_is_not_a_binomial_distribution() -> None:
    """Regression, spotted on the live site: ACJC P1 Q9 is a planes question and
    'the point B ( 2 , - 3 , 7 )' was tagged Binomial distribution."""
    text = (
        "Find the equations of the planes which are a distance of 3 2 from "
        "the point B ( 2 , − 3 , 7 ) . [4]"
    )
    assert "BINOM" not in match_topics(text)


def test_real_binomial_parameters_still_match() -> None:
    """Tightening B(n, p) must not lose the genuine case, including the spaced
    digits that PDF extraction produces."""
    assert "BINOM" in match_topics("X follows B ( 1 0 , 0 . 3 5 ) .")
    assert "BINOM" in match_topics("X ~ B(20, 0.4)")


def test_labelled_point_is_not_a_normal_distribution() -> None:
    assert "NORM" not in match_topics("the point N ( 1 , 2 , 5 ) lies on the line")
    assert "NORM" in match_topics("X ~ N ( 50 , 16 )")


def test_using_the_substitution_phrasing() -> None:
    """Regression: papers say 'Using the substitution', not 'by substitution'."""
    assert "INTT" in match_topics("(i) Using the substitution u = x^2 + 3, show that")


def test_rule_tags_returns_method_and_confidence() -> None:
    tags, reason = rule_tags("Find the test statistic and state the significance level.")
    assert reason == ""
    assert all(method == "rule" for _, _, method in tags)
    assert all(0.0 < conf <= 0.95 for _, conf, _ in tags)


def test_rule_tags_defers_when_hits_conflict() -> None:
    """Too many topics at once means the keywords are fighting; defer to the LLM."""
    kitchen_sink = (
        "Argand diagram locus of z. Find the test statistic and significance "
        "level. The product moment correlation coefficient and regression line. "
        "Find dy/dx at the stationary point. Maclaurin series in ascending "
        "powers. The position vectors and scalar product. Binomial distribution."
    )
    assert len(match_topics(kitchen_sink)) > MAX_RULE_TOPICS
    tags, reason = rule_tags(kitchen_sink)
    assert tags == []
    assert "conflicting" in reason


def test_rule_tags_defers_when_nothing_matches() -> None:
    tags, reason = rule_tags("Frederick starts a new online account this month.")
    assert tags == []
    assert reason == "no rule hits"


def test_rule_confidence_grows_and_is_capped() -> None:
    assert rule_confidence(1) < rule_confidence(3)
    assert rule_confidence(50) <= 0.95


# --------------------------------------------------------------------------
# LLM output validation (no network)
# --------------------------------------------------------------------------

@pytest.fixture
def tagger(seeded_topics: set[str]) -> LLMTagger:
    t = LLMTagger("claude-haiku-4-5", seeded_topics)
    # Pre-seed the prompt's topic list so classify() never needs a connection,
    # keeping these tests hermetic.
    t._topic_list = "\n".join(f"  {code} - {code} (pure)" for code in sorted(seeded_topics))
    return t


def test_validate_accepts_clean_json(tagger: LLMTagger) -> None:
    got = tagger._validate('{"topic_codes": ["VEC"], "confidence": 0.8}')
    assert got == {"topic_codes": ["VEC"], "confidence": 0.8}


def test_validate_survives_prose_and_fences(tagger: LLMTagger) -> None:
    body = 'Sure!\n```json\n{"topic_codes": ["CPLX", "GRAPH"], "confidence": 0.7}\n```'
    got = tagger._validate(body)
    assert got is not None
    assert got["topic_codes"] == ["CPLX", "GRAPH"]


def test_validate_drops_codes_outside_the_schema(tagger: LLMTagger) -> None:
    got = tagger._validate('{"topic_codes": ["VEC", "TRIGONOMETRY"], "confidence": 1}')
    assert got is not None
    assert got["topic_codes"] == ["VEC"]


def test_validate_uppercases_and_dedupes(tagger: LLMTagger) -> None:
    got = tagger._validate('{"topic_codes": ["vec", "VEC", "cplx"], "confidence": 0.5}')
    assert got is not None
    assert got["topic_codes"] == ["VEC", "CPLX"]


def test_validate_caps_at_three_topics(tagger: LLMTagger) -> None:
    got = tagger._validate(
        '{"topic_codes": ["VEC","CPLX","DIFF","NORM"], "confidence": 0.5}'
    )
    assert got is not None
    assert len(got["topic_codes"]) == 3


def test_validate_clamps_confidence(tagger: LLMTagger) -> None:
    assert tagger._validate('{"topic_codes":["VEC"],"confidence":9}')["confidence"] == 1.0
    assert tagger._validate('{"topic_codes":["VEC"],"confidence":-3}')["confidence"] == 0.0
    # Non-numeric confidence falls back rather than exploding.
    assert tagger._validate('{"topic_codes":["VEC"],"confidence":"high"}') is not None


@pytest.mark.parametrize(
    "body",
    [
        "no json here at all",
        '{"topic_codes": [], "confidence": 0.9}',
        '{"topic_codes": ["NOPE"], "confidence": 0.9}',
        '{"topic_codes": "VEC", "confidence": 0.9}',
        '{"confidence": 0.9}',
        '{"topic_codes": [1, 2]}',
    ],
)
def test_validate_rejects_unusable_replies(tagger: LLMTagger, body: str) -> None:
    assert tagger._validate(body) is None


# --------------------------------------------------------------------------
# token accounting (stubbed client, no network)
# --------------------------------------------------------------------------

class _Usage:
    def __init__(self, i: int, o: int) -> None:
        self.input_tokens = i
        self.output_tokens = o


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Resp:
    def __init__(self, text: str, i: int, o: int) -> None:
        self.content = [_Block(text)]
        self.usage = _Usage(i, o)


class _FakeMessages:
    def __init__(self, scripted: list[_Resp]) -> None:
        self._scripted = scripted
        self.calls = 0

    def create(self, **_kwargs):
        resp = self._scripted[min(self.calls, len(self._scripted) - 1)]
        self.calls += 1
        return resp


class _FakeClient:
    def __init__(self, scripted: list[_Resp]) -> None:
        self.messages = _FakeMessages(scripted)


def test_classify_reports_per_question_tokens(tagger: LLMTagger, monkeypatch) -> None:
    """Regression: cache_put was handed the tagger's *running totals*, so the
    cached ledger summed to about 11x the real spend."""
    good = '{"topic_codes": ["VEC"], "confidence": 0.9}'
    tagger._client = _FakeClient([_Resp(good, 500, 20)])

    first = tagger.classify(None, "position vectors question")
    assert first == ({"topic_codes": ["VEC"], "confidence": 0.9}, 500, 20)

    # A second question must report its own usage, not the accumulated figure.
    second = tagger.classify(None, "another question")
    assert second[1] == 500 and second[2] == 20, (
        f"per-question tokens leaked a running total: {second}"
    )
    # The tagger still tracks run totals separately.
    assert tagger.input_tokens == 1000
    assert tagger.output_tokens == 40
    assert tagger.calls == 2


def test_classify_counts_tokens_from_a_retried_reply(tagger: LLMTagger) -> None:
    """A retry after unparseable JSON must add to that question's own count."""
    tagger._client = _FakeClient([
        _Resp("sorry, no json", 400, 10),
        _Resp('{"topic_codes": ["CPLX"], "confidence": 0.6}', 450, 15),
    ])
    result, tin, tout = tagger.classify(None, "argand locus")
    assert result is not None and result["topic_codes"] == ["CPLX"]
    assert (tin, tout) == (850, 25)


def test_classify_returns_none_when_every_attempt_is_unusable(tagger: LLMTagger) -> None:
    tagger._client = _FakeClient([_Resp("still not json", 100, 5)])
    result, tin, tout = tagger.classify(None, "whatever")
    assert result is None
    assert tin > 0 and tout > 0


def test_missing_api_key_is_a_clear_error(tagger: LLMTagger, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _ = tagger.client
