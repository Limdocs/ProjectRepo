"""Stratified passage planning and shrink-only boundary snapping.

Position and text arithmetic only. No budgets, models, or I/O. Callers pass
an already-seeded ``random.Random`` so this module never touches the global
RNG, the clock, or ``hash()``.
"""

from __future__ import annotations

import unicodedata
from typing import Sequence, Tuple

RawRange = Tuple[int, int]

_SENTENCE_TERMINATORS = set(".!?;:\u05c3\u05c0")  # . ! ? ; : ׃ ׀


def plan_passages(
    *,
    text_length: int,
    allowance: int,
    rng: "random.Random",
    target_passage_chars: int,
    min_passage_chars: int,
    max_passages: int,
) -> Tuple[RawRange, ...]:
    """Return strictly ascending half-open ranges whose sizes sum to ``allowance``.

    When ``allowance`` covers the whole text, the result is ``[0, text_length)``.
    Otherwise passages are placed one per equal stratum, so the last passage
    always starts at or after ``(P - 1) * text_length // P``.
    """
    if text_length <= 0 or allowance <= 0:
        return ()
    if allowance >= text_length:
        return ((0, text_length),)

    passage_count = _passage_count(
        allowance,
        target_passage_chars=target_passage_chars,
        min_passage_chars=min_passage_chars,
        max_passages=max_passages,
    )
    sizes = _passage_sizes(allowance, passage_count)
    ranges = []
    for index, size in enumerate(sizes):
        stratum_start = (index * text_length) // passage_count
        stratum_end = ((index + 1) * text_length) // passage_count
        lo = stratum_start
        hi = max(lo, min(stratum_end - size, text_length - size))
        if lo + size > text_length:
            start = lo
            end = text_length
        else:
            start = rng.randrange(lo, hi + 1)
            end = start + size
        ranges.append((start, end))
    return tuple(ranges)


def snap_range(
    text: str, start: int, end: int, *, min_passage_chars: int
) -> RawRange:
    """Move ``start`` forward and ``end`` backward onto a nearby boundary.

    The window is ``min(200, size // 4)``. Priority is newline, then a sentence
    terminator followed by whitespace or end, then whitespace. If the snapped
    span falls below ``max(min_passage_chars, size // 2)``, the raw range is
    restored. Leading Unicode combining marks are then skipped so Hebrew
    niqqud is not left at the start of a passage. That last step is also
    shrink-only.
    """
    length = len(text)
    start = max(0, min(start, length))
    end = max(start, min(end, length))
    size = end - start
    if size == 0:
        return (start, end)

    window = min(200, size // 4)
    snapped_start, snapped_end = start, end
    if window > 0:
        snapped_start = _snap_start(text, start, end, window)
        snapped_end = _snap_end(text, start, end, window)
        threshold = max(min_passage_chars, size // 2)
        if snapped_end - snapped_start < threshold:
            snapped_start, snapped_end = start, end

    snapped_start = _skip_leading_combining(text, snapped_start, snapped_end)
    if snapped_start >= snapped_end:
        snapped_start = _skip_leading_combining(text, start, end)
        snapped_end = end
        if snapped_start >= snapped_end:
            return (start, end)
    return (snapped_start, snapped_end)


def coalesce(ranges: Sequence[RawRange]) -> Tuple[RawRange, ...]:
    """Sort by start and merge ranges that touch or overlap.

    Overlap merges strictly reduce the covered character count. The result is
    strictly ascending and non-overlapping.
    """
    ordered = sorted((start, end) for start, end in ranges if end > start)
    if not ordered:
        return ()
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _passage_count(
    allowance: int,
    *,
    target_passage_chars: int,
    min_passage_chars: int,
    max_passages: int,
) -> int:
    if target_passage_chars <= 0 or min_passage_chars <= 0 or max_passages <= 0:
        raise ValueError("passage shape must be positive")
    rounded = int(round(allowance / float(target_passage_chars)))
    count = max(1, min(rounded, max_passages))
    return min(count, max(1, allowance // min_passage_chars))


def _passage_sizes(allowance: int, passage_count: int) -> Tuple[int, ...]:
    nominal = allowance // passage_count
    extra = allowance % passage_count
    return tuple(nominal + (1 if index < extra else 0) for index in range(passage_count))


def _is_newline(text: str, index: int) -> bool:
    return text[index] == "\n"


def _is_whitespace(text: str, index: int) -> bool:
    return text[index].isspace()


def _is_sentence_end(text: str, index: int) -> bool:
    if text[index] not in _SENTENCE_TERMINATORS:
        return False
    next_index = index + 1
    return next_index >= len(text) or text[next_index].isspace()


def _snap_start(text: str, start: int, end: int, window: int) -> int:
    region_end = min(end, start + window)
    for matcher in (_is_newline, _is_sentence_end, _is_whitespace):
        for index in range(start, region_end):
            if not matcher(text, index):
                continue
            candidate = index + 1
            if matcher is _is_sentence_end and candidate < len(text) and text[candidate].isspace():
                if candidate + 1 <= start + window:
                    candidate += 1
            if start < candidate < end and candidate <= start + window:
                return candidate
    return start


def _snap_end(text: str, start: int, end: int, window: int) -> int:
    region_start = max(start + 1, end - window)
    for matcher in (_is_newline, _is_sentence_end, _is_whitespace):
        for index in range(end - 1, region_start - 1, -1):
            if not matcher(text, index):
                continue
            if matcher is _is_whitespace:
                candidate = index
            else:
                candidate = index + 1
            if start < candidate < end:
                return candidate
    return end


def _skip_leading_combining(text: str, start: int, end: int) -> int:
    while start < end and unicodedata.combining(text[start]) != 0:
        start += 1
    return start
