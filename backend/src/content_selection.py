"""Select quiz source text within a token budget.

Pure policy: dedupe, full-text vs stratified sampling, global scale-down, and
context assembly. No AWS, OpenAI, credentials, logging, or persistence.

``read_text`` may be called at most ``1 + MAX_BUDGET_ITERATIONS`` times per
accepted document. In sampled mode the full text of at most one document is
held at a time, plus the already extracted passage slices. Full-text mode
(at most three documents) keeps those texts and the joined context; peak
memory is about twice the total source characters.

The calibration probe used to pick a starting scale is the leading
``PROBE_CHARS`` of each document. The full text is not retained between the
length scan and passage planning, and the assembled context is always
measured exactly afterwards.
"""

from __future__ import annotations

import hashlib
import random
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple

from passage_sampling import coalesce, plan_passages, snap_range
from token_budget import (
    PROBE_CHARS,
    ContentSelectionError,
    InvalidBudgetConfigurationError,
    OverheadTokens,
    TokenCounter,
    TokenizerUnavailableError,
    UnsupportedModelError,
    encoding_name_for_model,
    get_token_counter,
    source_token_allowance,
)

ALGORITHM_VERSION = "content-selection/1"

FULL_TEXT_MAX_DOCUMENTS = 3
BASELINE_NUMERATOR = 3
MIN_PASSAGE_CHARS = 400
TARGET_PASSAGE_CHARS = 1200
MAX_PASSAGES_PER_DOCUMENT = 12
MAX_BUDGET_ITERATIONS = 4
GAP_MARKER = "\n[...]\n"
_LABEL_MAX_CHARS = 120

MODE_FULL_TEXT = "FULL_TEXT"
MODE_SAMPLED = "SAMPLED"
TOKEN_ESTIMATE_KIND = "MEASURED_TEXT_PLUS_DECLARED_OVERHEAD"


@dataclass(frozen=True)
class SourceDocument:
    document_id: str
    text: str
    label: str = ""


@dataclass(frozen=True)
class DocumentRef:
    document_id: str
    label: str = ""


@dataclass(frozen=True)
class SelectionConfig:
    model_name: str
    context_window_tokens: int
    max_output_tokens: int
    operational_input_token_budget: int
    seed: int
    min_passage_chars: int = MIN_PASSAGE_CHARS
    target_passage_chars: int = TARGET_PASSAGE_CHARS
    max_passages_per_document: int = MAX_PASSAGES_PER_DOCUMENT
    full_text_max_documents: int = FULL_TEXT_MAX_DOCUMENTS


@dataclass(frozen=True)
class PassageRange:
    """Half-open ``[start, end)`` character offsets into the original string."""

    start: int
    end: int


@dataclass(frozen=True)
class DocumentSelection:
    document_id: str
    mode: str
    original_chars: int
    selected_chars: int
    baseline_allowance_chars: int
    effective_allowance_chars: int
    passage_count: int
    ranges: Tuple[PassageRange, ...]
    reduction_reason: Optional[str]


@dataclass(frozen=True)
class SelectionMetadata:
    algorithm_version: str
    seed: int
    mode: str
    model_name: str
    encoding_name: str
    unique_document_count: int
    documents: Tuple[DocumentSelection, ...]
    skipped_empty_document_ids: Tuple[str, ...]
    source_chars_total: int
    selected_source_chars_total: int
    formatting_chars: int
    context_tokens_measured: int
    overhead_tokens_declared: int
    safety_margin_tokens: int
    source_token_allowance: int
    operational_input_token_budget: int
    context_window_tokens: int
    token_estimate_kind: str
    budget_iterations: int
    global_scale_permille: int


@dataclass(frozen=True)
class SelectionResult:
    context: str
    metadata: SelectionMetadata


class NoDocumentsSelectedError(ContentSelectionError):
    def __init__(self):
        super().__init__("NO_DOCUMENTS_SELECTED", {})


class InvalidDocumentIdError(ContentSelectionError):
    def __init__(self):
        super().__init__("INVALID_DOCUMENT_ID", {})


class ConflictingDuplicateDocumentIdError(ContentSelectionError):
    def __init__(self, document_id: str, length_a: int, length_b: int):
        super().__init__(
            "CONFLICTING_DUPLICATE_DOCUMENT_ID",
            {"document_id": document_id, "lengths": [length_a, length_b]},
        )


class AllSourcesEmptyError(ContentSelectionError):
    def __init__(self, document_ids: Sequence[str]):
        super().__init__("ALL_SOURCES_EMPTY", {"document_ids": list(document_ids)})


class FullTextOverBudgetError(ContentSelectionError):
    def __init__(
        self,
        unique_document_count: int,
        required_input_tokens: int,
        input_token_budget: int,
        source_token_allowance_value: int,
    ):
        super().__init__(
            "FULL_TEXT_OVER_BUDGET",
            {
                "unique_document_count": unique_document_count,
                "required_input_tokens": required_input_tokens,
                "input_token_budget": input_token_budget,
                "source_token_allowance": source_token_allowance_value,
            },
        )


class InsufficientPassageBudgetError(ContentSelectionError):
    def __init__(
        self,
        unique_document_count: int,
        floor_chars_total: int,
        source_token_allowance_value: int,
        documents_at_floor: int,
    ):
        super().__init__(
            "INSUFFICIENT_PASSAGE_BUDGET",
            {
                "unique_document_count": unique_document_count,
                "floor_chars_total": floor_chars_total,
                "source_token_allowance": source_token_allowance_value,
                "documents_at_floor": documents_at_floor,
            },
        )


class BudgetNotConvergedError(ContentSelectionError):
    def __init__(
        self,
        iterations: int,
        last_measured_tokens: int,
        source_token_allowance_value: int,
    ):
        super().__init__(
            "BUDGET_NOT_CONVERGED",
            {
                "iterations": iterations,
                "last_measured_tokens": last_measured_tokens,
                "source_token_allowance": source_token_allowance_value,
            },
        )


@dataclass(frozen=True)
class _DocRecord:
    document_id: str
    label: str
    length: int
    probe_tokens: int
    probe_chars: int


def select_quiz_content(
    documents: Sequence[SourceDocument],
    *,
    config: SelectionConfig,
    overhead: OverheadTokens,
    token_counter: Optional[TokenCounter] = None,
) -> SelectionResult:
    """Eager entry point. Detects conflicting duplicate ids, then streams."""
    if not documents:
        raise NoDocumentsSelectedError()

    order = []
    texts = {}
    seen = set()
    for document in documents:
        document_id = document.document_id.strip()
        if not document_id:
            raise InvalidDocumentIdError()
        if document_id in seen:
            previous = texts[document_id]
            if previous != document.text:
                raise ConflictingDuplicateDocumentIdError(
                    document_id, len(previous), len(document.text)
                )
            continue
        seen.add(document_id)
        texts[document_id] = document.text
        order.append(DocumentRef(document_id, document.label))

    def read_text(document_id: str) -> str:
        return texts[document_id]

    return select_quiz_content_streaming(
        order,
        read_text=read_text,
        config=config,
        overhead=overhead,
        token_counter=token_counter,
    )


def select_quiz_content_streaming(
    document_refs: Sequence[DocumentRef],
    *,
    read_text: Callable[[str], str],
    config: SelectionConfig,
    overhead: OverheadTokens,
    token_counter: Optional[TokenCounter] = None,
) -> SelectionResult:
    """Streaming entry point. ``read_text(document_id)`` returns the full source.

    Phase 2 supplies an S3-backed reader. The reader may be invoked more than
    once per document, at most ``1 + MAX_BUDGET_ITERATIONS`` times.
    """
    if not document_refs:
        raise NoDocumentsSelectedError()

    refs = _dedupe_refs(document_refs)
    encoding_name = encoding_name_for_model(config.model_name)
    _validate_shape(config)
    allowance = source_token_allowance(config, overhead)
    counter = token_counter if token_counter is not None else get_token_counter(config.model_name)

    records, skipped = _scan_documents(refs, read_text, counter)
    if not records:
        raise AllSourcesEmptyError(skipped)

    count = len(records)
    mode = (
        MODE_FULL_TEXT
        if count <= config.full_text_max_documents
        else MODE_SAMPLED
    )
    baselines = tuple(_baseline(record.length, count, mode) for record in records)
    if mode == MODE_SAMPLED:
        _ensure_floors_fit(records, baselines, config, allowance)

    if mode == MODE_FULL_TEXT:
        effectives = tuple(record.length for record in records)
        context, selections = _materialize(
            records, effectives, baselines, mode, 1000, config, read_text
        )
        measured = counter.count(context)
        if measured > allowance:
            raise FullTextOverBudgetError(
                count,
                measured + overhead.input_side(),
                config.operational_input_token_budget,
                allowance,
            )
        return _success(
            context,
            selections,
            skipped,
            config,
            overhead,
            encoding_name,
            allowance,
            measured,
            budget_iterations=1,
            global_scale_permille=1000,
            mode=mode,
        )

    scale = 1000
    estimate = _estimate_tokens(records, _effectives(records, baselines, config, scale), config)
    if estimate > allowance:
        scale = _next_scale(scale, estimate, allowance)

    last_measured = 0
    for iteration in range(1, MAX_BUDGET_ITERATIONS + 1):
        effectives = _effectives(records, baselines, config, scale)
        context, selections = _materialize(
            records, effectives, baselines, mode, scale, config, read_text
        )
        last_measured = counter.count(context)
        if last_measured <= allowance:
            return _success(
                context,
                selections,
                skipped,
                config,
                overhead,
                encoding_name,
                allowance,
                last_measured,
                budget_iterations=iteration,
                global_scale_permille=scale,
                mode=mode,
            )
        if iteration == MAX_BUDGET_ITERATIONS:
            break
        scale = _next_scale(scale, last_measured, allowance)

    raise BudgetNotConvergedError(MAX_BUDGET_ITERATIONS, last_measured, allowance)


def _dedupe_refs(document_refs: Sequence[DocumentRef]) -> Tuple[DocumentRef, ...]:
    order = []
    seen = set()
    for ref in document_refs:
        document_id = ref.document_id.strip()
        if not document_id:
            raise InvalidDocumentIdError()
        if document_id in seen:
            continue
        seen.add(document_id)
        order.append(DocumentRef(document_id, ref.label))
    return tuple(order)


def _validate_shape(config: SelectionConfig) -> None:
    if (
        config.min_passage_chars <= 0
        or config.target_passage_chars < config.min_passage_chars
        or config.max_passages_per_document <= 0
        or config.full_text_max_documents <= 0
    ):
        raise InvalidBudgetConfigurationError({"reason": "invalid_passage_shape"})


def _scan_documents(refs, read_text, counter):
    records = []
    skipped = []
    for ref in refs:
        text = read_text(ref.document_id)
        try:
            if not isinstance(text, str):
                raise InvalidBudgetConfigurationError({"reason": "document_text_not_str"})
            if not text.strip():
                skipped.append(ref.document_id)
                continue
            probe_end = min(PROBE_CHARS, len(text))
            probe = text[:probe_end]
            probe_tokens = counter.count(probe)
            probe_chars = len(probe) if probe else 1
            records.append(
                _DocRecord(
                    document_id=ref.document_id,
                    label=ref.label,
                    length=len(text),
                    probe_tokens=probe_tokens,
                    probe_chars=probe_chars,
                )
            )
            del probe
        finally:
            del text
    return tuple(records), tuple(skipped)


def _baseline(length: int, count: int, mode: str) -> int:
    if mode == MODE_FULL_TEXT:
        return length
    return (length * BASELINE_NUMERATOR) // count


def _effective_allowance(
    length: int, baseline: int, min_passage_chars: int, scale_permille: int
) -> int:
    floor = min(min_passage_chars, length)
    scaled = (baseline * scale_permille) // 1000
    effective = max(floor, scaled)
    # The usability floor is the only reason a document may exceed its baseline.
    if floor <= baseline:
        effective = min(effective, baseline)
    return min(effective, length)


def _effectives(records, baselines, config, scale):
    return tuple(
        _effective_allowance(record.length, baseline, config.min_passage_chars, scale)
        for record, baseline in zip(records, baselines)
    )


def _chars_to_tokens(chars: int, probe_tokens: int, probe_chars: int) -> int:
    if chars <= 0 or probe_tokens <= 0 or probe_chars <= 0:
        return 0
    return (chars * probe_tokens + probe_chars - 1) // probe_chars


def _formatting_chars(records, passage_counts) -> int:
    total = 0
    document_count = len(records)
    for index, record in enumerate(records):
        label = _sanitize_label(record.label, index + 1)
        total += len("### Document %s: %s\n" % (index + 1, label))
        passages = passage_counts[index]
        if passages > 1:
            total += (passages - 1) * len(GAP_MARKER)
    if document_count > 1:
        total += (document_count - 1) * len("\n\n")
    return total


def _estimate_tokens(records, effectives, config) -> int:
    source = 0
    for record, chars in zip(records, effectives):
        source += _chars_to_tokens(chars, record.probe_tokens, record.probe_chars)
    probe_tokens = sum(record.probe_tokens for record in records)
    probe_chars = sum(record.probe_chars for record in records) or 1
    # One passage per document is a lower bound on formatting; extra gap
    # markers are covered by the exact measurement that follows.
    formatting = _formatting_chars(records, [1] * len(records))
    return source + _chars_to_tokens(formatting, probe_tokens, probe_chars)


def _ensure_floors_fit(records, baselines, config, allowance) -> None:
    floors = tuple(min(config.min_passage_chars, record.length) for record in records)
    floor_chars_total = sum(floors)
    documents_at_floor = sum(
        1
        for record, baseline, floor in zip(records, baselines, floors)
        if floor >= baseline or record.length <= config.min_passage_chars
    )
    # Every accepted document contributes at least its floor, so a floor that
    # cannot fit rejects the whole request instead of dropping a document.
    if _estimate_tokens(records, floors, config) > allowance:
        raise InsufficientPassageBudgetError(
            len(records),
            floor_chars_total,
            allowance,
            documents_at_floor if documents_at_floor else len(records),
        )


def _next_scale(scale: int, measured_or_estimate: int, allowance: int) -> int:
    if measured_or_estimate <= 0:
        return scale
    reduced = (scale * allowance * 97) // (measured_or_estimate * 100)
    if reduced >= scale:
        reduced = scale - 1
    if reduced < 0:
        return 0
    return reduced


def _materialize(records, effectives, baselines, mode, scale, config, read_text):
    pieces = []
    selections = []
    for index, record in enumerate(records):
        text = read_text(record.document_id)
        try:
            ranges, slices = _passages_for_document(
                text, effectives[index], config, record.document_id
            )
        finally:
            del text
        pieces.append(slices)
        selected = sum(len(piece) for piece in slices)
        selections.append(
            DocumentSelection(
                document_id=record.document_id,
                mode=mode,
                original_chars=record.length,
                selected_chars=selected,
                baseline_allowance_chars=baselines[index],
                effective_allowance_chars=effectives[index],
                passage_count=len(ranges),
                ranges=ranges,
                reduction_reason=_reduction_reason(
                    record.length,
                    baselines[index],
                    effectives[index],
                    config.min_passage_chars,
                    scale,
                    mode,
                ),
            )
        )
    context = _assemble(records, pieces)
    return context, tuple(selections)


def _passages_for_document(text, allowance, config, document_id):
    length = len(text)
    if allowance >= length:
        return (PassageRange(0, length),), (_copy_slice(text, 0, length),)

    raw = plan_passages(
        text_length=length,
        allowance=allowance,
        rng=_document_rng(config.seed, document_id),
        target_passage_chars=config.target_passage_chars,
        min_passage_chars=config.min_passage_chars,
        max_passages=config.max_passages_per_document,
    )
    snapped = [
        snap_range(text, start, end, min_passage_chars=config.min_passage_chars)
        for start, end in raw
    ]
    merged = coalesce(snapped)
    ranges = tuple(PassageRange(start, end) for start, end in merged)
    slices = tuple(_copy_slice(text, start, end) for start, end in merged)
    return ranges, slices


def _copy_slice(text: str, start: int, end: int) -> str:
    # Join forces a new string so a full-span slice does not alias ``text``.
    return "".join(text[start:end])


def _document_rng(seed: int, document_id: str) -> random.Random:
    payload = ("%s|%s|%s" % (ALGORITHM_VERSION, seed, document_id)).encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=16).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _reduction_reason(length, baseline, effective, min_passage_chars, scale, mode):
    if mode == MODE_FULL_TEXT:
        return None
    floor = min(min_passage_chars, length)
    if floor > baseline and effective >= baseline:
        return "USABILITY_FLOOR_APPLIED"
    if scale < 1000 and effective < baseline:
        return "GLOBAL_TOKEN_BUDGET"
    return None


def _assemble(records, pieces) -> str:
    parts = []
    for index, record in enumerate(records):
        label = _sanitize_label(record.label, index + 1)
        header = "### Document %s: %s\n" % (index + 1, label)
        body = GAP_MARKER.join(pieces[index])
        parts.append(header + body)
    return "\n\n".join(parts)


def _sanitize_label(label: str, index: int) -> str:
    if not label or not label.strip():
        return "Document %s" % index
    characters = []
    for char in label:
        if char.isspace():
            characters.append(" ")
        elif _is_control(char):
            continue
        else:
            characters.append(char)
    collapsed = " ".join("".join(characters).split())
    collapsed = collapsed[:_LABEL_MAX_CHARS]
    return collapsed or ("Document %s" % index)


def _is_control(char: str) -> bool:
    return unicodedata.category(char) == "Cc"


def _success(
    context,
    selections,
    skipped,
    config,
    overhead,
    encoding_name,
    allowance,
    measured,
    budget_iterations,
    global_scale_permille,
    mode,
):
    selected_total = sum(item.selected_chars for item in selections)
    source_total = sum(item.original_chars for item in selections)
    metadata = SelectionMetadata(
        algorithm_version=ALGORITHM_VERSION,
        seed=config.seed,
        mode=mode,
        model_name=config.model_name,
        encoding_name=encoding_name,
        unique_document_count=len(selections),
        documents=selections,
        skipped_empty_document_ids=tuple(skipped),
        source_chars_total=source_total,
        selected_source_chars_total=selected_total,
        formatting_chars=len(context) - selected_total,
        context_tokens_measured=measured,
        overhead_tokens_declared=overhead.input_side(),
        safety_margin_tokens=overhead.safety_margin,
        source_token_allowance=allowance,
        operational_input_token_budget=config.operational_input_token_budget,
        context_window_tokens=config.context_window_tokens,
        token_estimate_kind=TOKEN_ESTIMATE_KIND,
        budget_iterations=budget_iterations,
        global_scale_permille=global_scale_permille,
    )
    return SelectionResult(context, metadata)
