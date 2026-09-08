"""Fast, order-preserving page alignment for localized manga editions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class Match:
    jp: list[int]
    en: list[int]
    confidence: float


def similarity(left: str | None, right: str | None) -> float:
    """Return normalized Hamming similarity for equal-sized binary hashes."""
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    if not size:
        return 0.0
    return sum(a == b for a, b in zip(left[:size], right[:size])) / size


def _feature(page: str | Mapping[str, str | None], name: str = "fingerprint") -> str | None:
    if isinstance(page, str):
        return page
    return page.get(name)


def _page_similarity(a: str | Mapping[str, str | None], b: str | Mapping[str, str | None]) -> float:
    return similarity(_feature(a), _feature(b))


def _spread_similarity(
    single: str | Mapping[str, str | None],
    first: str | Mapping[str, str | None],
    second: str | Mapping[str, str | None],
) -> float:
    """Compare a combined spread's halves with two consecutive pages."""
    left = _feature(single, "fingerprint_left")
    right = _feature(single, "fingerprint_right")
    if not left or not right:
        return 0.0
    direct = (similarity(left, _feature(first)) + similarity(right, _feature(second))) / 2
    reverse = (similarity(right, _feature(first)) + similarity(left, _feature(second))) / 2
    return max(direct, reverse)


def detect_offset(
    jp: Sequence[str | Mapping[str, str | None]],
    en: Sequence[str | Mapping[str, str | None]],
    max_offset: int = 16,
) -> tuple[int, float] | None:
    """Find a strong constant edition offset before running full alignment."""
    best: tuple[int, float, int] | None = None
    for offset in range(-max_offset, max_offset + 1):
        scores = []
        for j in range(len(jp)):
            e = j + offset
            if 0 <= e < len(en):
                scores.append(_page_similarity(jp[j], en[e]))
        if len(scores) < min(3, len(jp), len(en)):
            continue
        strong = [score for score in scores if score >= 0.78]
        ranked = sorted(scores)
        trim = len(ranked) // 5
        average = sum(ranked[trim:]) / max(1, len(ranked) - trim)
        candidate = (offset, average, len(strong))
        if best is None or (candidate[2], candidate[1]) > (best[2], best[1]):
            best = candidate
    if best and best[1] >= 0.80 and best[2] >= max(2, int(min(len(jp), len(en)) * 0.55)):
        return best[0], best[1]
    return None


def align(
    jp: Sequence[str | Mapping[str, str | None]],
    en: Sequence[str | Mapping[str, str | None]],
) -> list[Match]:
    """Monotonic dynamic-programming alignment with gaps and spread groups."""
    n, m = len(jp), len(en)
    neg = -10**9
    dp = [[neg] * (m + 1) for _ in range(n + 1)]
    move: list[list[str | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    gap = -0.42
    for i in range(1, n + 1):
        dp[i][0], move[i][0] = dp[i - 1][0] + gap, "j"
    for k in range(1, m + 1):
        dp[0][k], move[0][k] = dp[0][k - 1] + gap, "e"
    for i in range(1, n + 1):
        for k in range(1, m + 1):
            options = [
                (dp[i - 1][k - 1] + _page_similarity(jp[i - 1], en[k - 1]), "x"),
                (dp[i - 1][k] + gap, "j"),
                (dp[i][k - 1] + gap, "e"),
            ]
            if k >= 2:
                score = _spread_similarity(jp[i - 1], en[k - 2], en[k - 1])
                if score >= 0.68:
                    options.append((dp[i - 1][k - 2] + score - 0.08, "je2"))
            if i >= 2:
                score = _spread_similarity(en[k - 1], jp[i - 2], jp[i - 1])
                if score >= 0.68:
                    options.append((dp[i - 2][k - 1] + score - 0.08, "j2e"))
            dp[i][k], move[i][k] = max(options, key=lambda item: item[0])

    result: list[Match] = []
    i, k = n, m
    while i or k:
        direction = move[i][k]
        if direction == "x":
            result.append(Match([i - 1], [k - 1], _page_similarity(jp[i - 1], en[k - 1])))
            i -= 1
            k -= 1
        elif direction == "je2":
            confidence = _spread_similarity(jp[i - 1], en[k - 2], en[k - 1])
            result.append(Match([i - 1], [k - 2, k - 1], confidence))
            i -= 1
            k -= 2
        elif direction == "j2e":
            confidence = _spread_similarity(en[k - 1], jp[i - 2], jp[i - 1])
            result.append(Match([i - 2, i - 1], [k - 1], confidence))
            i -= 2
            k -= 1
        elif direction == "j":
            result.append(Match([i - 1], [], 0.0))
            i -= 1
        elif direction == "e":
            result.append(Match([], [k - 1], 0.0))
            k -= 1
        else:
            if i:
                result.append(Match([i - 1], [], 0.0))
                i -= 1
            elif k:
                result.append(Match([], [k - 1], 0.0))
                k -= 1
    return list(reversed(result))
