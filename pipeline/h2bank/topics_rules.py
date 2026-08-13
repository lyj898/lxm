"""Regex rule map for topic tagging (pass 1).

Every seeded 9758 topic has at least three patterns. Patterns are matched
case-insensitively against the extracted question text, which is why they
tolerate the spacing damage PDF extraction does to notation (`dy/dx` can come
out as `d y / d x`).
"""

from __future__ import annotations

import re

# topic_code -> list of regex patterns
RULES: dict[str, list[str]] = {
    # ---------------- pure ----------------
    "FUNC": [
        r"\binverse function\b",
        r"\bf\s*-\s*1\s*\(|\bf\^?\s*-\s*1\b",
        r"\bcomposite function\b|\bfg\s*\(\s*x\s*\)|\bgf\s*\(\s*x\s*\)",
        r"\bone-?\s?one\b|\bself-?inverse\b",
        r"\bdomain\b.{0,40}\brange\b|\brange of\s+f\b",
    ],
    "GRAPH": [
        r"\basymptote",
        r"sketch(?:es|ing)?\s+the\s+(?:curve|graph)",
        r"\btransformation",
        r"\bstretch(?:ed|ing)?\b.{0,30}\b(?:factor|scale)\b",
        r"\btranslat(?:e|ed|ion)\b.{0,30}\b(?:unit|vector)\b",
    ],
    "EQIN": [
        r"solve\s+the\s+inequalit",
        r"\bsimultaneous equations?\b",
        r"\bdiscriminant\b",
        r"solve\s+the\s+(?:equation|system)",
        r"\bhence\b.{0,30}solve.{0,30}\binequalit",
    ],
    "SEQS": [
        r"\barithmetic (?:progression|series|sequence)\b|\bAP\b",
        r"\bgeometric (?:progression|series|sequence)\b|\bGP\b",
        r"\bcommon (?:difference|ratio)\b",
        r"\bmethod of differences\b|\bsigma notation\b",
        r"\brecurrence relation\b|u\s*_?\s*\{?\s*n\s*\+\s*1",
        r"\bsum(?:mation)?\b.{0,30}\bfirst\s+n\s+terms\b|\bconverges?\b.{0,30}\bsum to infinity\b",
    ],
    "VEC": [
        # Plural matters: papers say "position vectors a, p and q".
        r"\bposition vectors?\b",
        r"\bscalar product\b|\bdot product\b|\bvector product\b|\bcross product\b",
        r"\bdirection vector\b",
        r"\bplane\b.{0,60}\b(?:line|angle|intersect|normal)\b",
        r"\bperpendicular distance\b|\bfoot of (?:the )?perpendicular\b",
    ],
    "CPLX": [
        r"\bargand\b",
        r"\bcomplex (?:number|conjugate|root)",
        r"locus.{0,40}\bz\b|\bz\b.{0,20}locus",
        r"\bmodulus\b.{0,30}\bargument\b|\barg\s*\(",
        r"\bconjugate\b",
    ],
    "DIFF": [
        r"\bd\s*y\s*/\s*d\s*x\b|\bdy\s*/\s*dx\b|\bd\s*2\s*y\s*/\s*d\s*x\s*2\b",
        r"\bstationary point",
        r"\brate(?:s)? of change\b|\bconnected rates\b",
        r"\bdifferentiat(?:e|ing|ion)\b|\bderivative\b",
        r"\btangent\b.{0,30}\bcurve\b|\bnormal to the curve\b",
        r"\bmaximum\b.{0,30}\bminimum\b.{0,30}\bvalue\b",
    ],
    "MACL": [
        r"\bmaclaurin",
        r"\bascending powers of\b",
        r"first\s+(?:two|three|four|\d+)\s+(?:non-?zero\s+)?terms",
        r"\bstandard series\b|\bseries expansion\b",
    ],
    "INTT": [
        r"\bintegration by parts\b",
        # Papers write "Using the substitution x = ..." far more often than
        # "by substitution".
        r"\b(?:using|by|with) the substitution\b|\bby substitution\b",
        r"\bpartial fractions\b",
        r"\bindefinite integral\b|\bintegrate\b",
    ],
    "DEFI": [
        r"\barea\b.{0,40}\b(?:bounded|enclosed|region)\b",
        r"\bvolume\b.{0,40}\b(?:revolution|rotated)\b",
        r"\brotated\b.{0,40}\babout the\b.{0,20}\baxis\b",
        r"\bdefinite integral\b",
        r"\bregion\b.{0,30}\bbounded by\b",
    ],
    "DIFFEQ": [
        r"\bdifferential equation\b",
        r"\bgeneral solution\b|\bparticular solution\b",
        r"\bproportional to\b.{0,40}\brate\b|\brate\b.{0,40}\bproportional to\b",
        r"\bseparat(?:e|ing) the variables\b",
    ],
    # ---------------- statistics ----------------
    "PNC": [
        r"\bpermutation|\bcombination",
        r"how many (?:different )?ways",
        r"\barrange(?:d|ment)\b.{0,40}\b(?:row|circle|line|shelf)\b",
        r"\bnCr\b|\bnPr\b|\bfactorial\b",
        r"\bcommittee\b|\bselected (?:at random )?from\b.{0,40}\bdifferent\b",
    ],
    "PROB": [
        r"\bprobability that\b",
        r"\bmutually exclusive\b|\bindependent events\b",
        r"\bconditional probability\b|P\s*\([^)]*\|[^)]*\)",
        r"\bvenn diagram\b|\btree diagram\b",
    ],
    "DRV": [
        r"\bdiscrete random variable\b",
        r"\bprobability distribution\b",
        r"\bE\s*\(\s*X\s*\)|\bexpectation\b",
        r"\bVar\s*\(\s*X\s*\)|\bvariance of\b.{0,20}\bX\b",
    ],
    "BINOM": [
        r"\bbinomial distribution\b",
        r"\bB\s*\(\s*\d+\s*,",
        r"\bindependent trials\b|\bn\s+trials\b",
        r"\bat least\b.{0,30}\bsuccess|exactly\b.{0,30}\bsuccess",
    ],
    "NORM": [
        r"\bnormal(?:ly)? distribut",
        r"\bN\s*\(\s*[\d.]+\s*,",
        r"\bz-?\s?score\b|\bstandardis(?:e|ed)\b",
        r"\bmean\b.{0,40}\bstandard deviation\b.{0,60}\bprobability\b",
    ],
    "SAMP": [
        r"\brandom sample\b",
        r"\bcentral limit theorem\b|\bCLT\b",
        r"\bsample mean\b|\bsampling distribution\b",
        r"\bunbiased estimate\b.{0,40}\bpopulation\b",
    ],
    "HYPO": [
        r"\btest statistic\b",
        r"\bH\s*_?\s*0\b|\bnull hypothesis\b|\balternative hypothesis\b",
        r"\bsignificance level\b|\blevel of significance\b",
        r"\bunbiased estimate\b",
        r"\bp-?\s?value\b|\bone-?tail(?:ed)?\b|\btwo-?tail(?:ed)?\b",
    ],
    "CORR": [
        r"\bproduct moment\b",
        r"\bregression line\b|\bleast squares\b",
        r"\bscatter diagram\b|\bscatter ?plot\b",
        r"\bcorrelation coefficient\b",
        r"\br\s*=\s*-?\s*0?\.\d+",
    ],
}

COMPILED: dict[str, list[re.Pattern[str]]] = {
    code: [re.compile(p, re.IGNORECASE) for p in patterns]
    for code, patterns in RULES.items()
}


def match_topics(text: str) -> dict[str, int]:
    """Return {topic_code: number of distinct patterns that matched}."""
    hits: dict[str, int] = {}
    for code, patterns in COMPILED.items():
        n = sum(1 for p in patterns if p.search(text))
        if n:
            hits[code] = n
    return hits


def rule_confidence(n_patterns: int) -> float:
    """More independent patterns matching -> more confidence, capped at 0.95."""
    return round(min(0.95, 0.55 + 0.15 * n_patterns), 2)
