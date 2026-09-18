from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


_KEEP = re.compile(r"[0-9a-z가-힣]+", re.IGNORECASE)


def normalize_text(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(_KEEP.findall(folded))


def _bigrams(value: str) -> list[str]:
    if len(value) < 2:
        return [value] if value else []
    return [value[index : index + 2] for index in range(len(value) - 1)]


def dice_score(left: str, right: str) -> float:
    left_grams = _bigrams(normalize_text(left))
    right_grams = _bigrams(normalize_text(right))
    if not left_grams and not right_grams:
        return 1.0
    if not left_grams or not right_grams:
        return 0.0
    overlap = 0
    remaining = list(right_grams)
    for gram in left_grams:
        if gram in remaining:
            remaining.remove(gram)
            overlap += 1
    return (2 * overlap) / (len(left_grams) + len(right_grams))


@dataclass(frozen=True, slots=True)
class DuplicateVerdict:
    exact: bool
    score: float
    incomplete: bool = False


def compare_manuscripts(
    title: str,
    body: str,
    other_title: str,
    other_body: str,
    *,
    incomplete: bool = False,
) -> DuplicateVerdict:
    title_norm = normalize_text(title)
    body_norm = normalize_text(body)
    other_title_norm = normalize_text(other_title)
    other_body_norm = normalize_text(other_body)
    exact = title_norm == other_title_norm and body_norm == other_body_norm
    score = 1.0 if exact else (
        dice_score(title, other_title) + dice_score(body, other_body)
    ) / 2
    return DuplicateVerdict(exact=exact, score=score, incomplete=incomplete)


def highest_match(
    title: str,
    body: str,
    corpus: list[tuple[str, str]],
    *,
    incomplete: bool = False,
) -> DuplicateVerdict:
    if not corpus:
        return DuplicateVerdict(exact=False, score=0.0, incomplete=incomplete)
    best = DuplicateVerdict(exact=False, score=0.0, incomplete=incomplete)
    for other_title, other_body in corpus:
        verdict = compare_manuscripts(
            title,
            body,
            other_title,
            other_body,
            incomplete=incomplete,
        )
        if verdict.exact:
            return verdict
        if verdict.score > best.score:
            best = verdict
    return best
