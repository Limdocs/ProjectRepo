# Quiz content selection

The public entry points are `select_quiz_content` and `select_quiz_content_streaming` in `backend/src/content_selection.py`. Sampling lives in `passage_sampling.py`. Token arithmetic and the tiktoken loader live in `token_budget.py`. The quiz worker calls the streaming selector; document processing status and quiz-generation status stay independent.

## Policy

`N` is the number of unique, non-empty documents in **this** request, after `.strip()` on ids. It is not the course document count.

| `N` | Mode | What is sent |
| --- | --- | --- |
| 1–3 | `FULL_TEXT` | The entire original text of every accepted document. One range `[0, len(text))`. |
| 4 or more | `SAMPLED` | A stratified sample whose baseline allowance is `(len(text) * 3) // N` per document. |

The `3/N` baseline continues past 10 documents (4 → 75%, 5 → 60%, 10 → 30%, 20 → 15%, 50 → 6%). It is an upper bound. A single global scale, in parts per thousand (`1000` means the baseline), shrinks every document by the same factor. No document is raised above its baseline because another document had room left.

The usability floor is the one exception: every accepted document contributes at least `min(min_passage_chars, len(text))` characters, even when that is larger than its baseline. If those floors cannot fit the source-token allowance, the request fails with `INSUFFICIENT_PASSAGE_BUDGET`. No selected document is dropped, and no sub-floor fragment is emitted.

`FULL_TEXT` never falls back to sampling. If the full request exceeds the budget, the error is `FULL_TEXT_OVER_BUDGET`. Overflow is never resolved by taking a prefix of the assembled context.

Blank or whitespace-only sources are excluded from `N`, listed in `skipped_empty_document_ids`, and otherwise left unchanged. If every document is blank, the error is `ALL_SOURCES_EMPTY`.

Repeated ids collapse to the first occurrence (first label kept). Same normalized id with different text, on the eager entry point only, is `CONFLICTING_DUPLICATE_DOCUMENT_ID`. Filenames are not identifiers: two ids with the same label stay two documents.

Characters decide the sample. Tokens decide whether the request is safe. The assembled context, including headers and gap markers, is counted with the real tokenizer after formatting. That total is measured text plus declared overhead plus a safety margin. It is not an exact provider payload size.

Account TPM/RPM limits are not in this repository and are not part of the budget.

## Algorithm

Version: `content-selection/1`.

Given `(documents, source text, config, ALGORITHM_VERSION, seed)`, the output is identical across calls and fresh imports. Each document draws from `random.Random` seeded with `blake2b(ALGORITHM_VERSION | seed | document_id)`. There is no global `random`, no clock, and no builtin `hash()`. Different seeds are not required to differ (full text, and any document whose allowance covers it, ignore the seed).

Sampled passages:

1. If the allowance covers the document, emit `[0, len(text))`.
2. Passage count is `clamp(1, round(allowance / target_passage_chars), max_passages)`, then capped by `allowance // min_passage_chars` so a passage is not thinner than the minimum.
3. Sizes are `allowance // P`, with the remainder given one character at a time to the earliest passages.
4. The document is split into `P` equal strata. Passage `k` starts inside stratum `k`, so the last passage always starts at or after `(P - 1) * length // P`.
5. Each range snaps to a nearby boundary, shrink-only, within `min(200, size // 4)`: newline, then a sentence terminator (`. ! ? ; : ׃ ׀`) followed by whitespace or end, then whitespace. Start moves forward and end moves backward. Leading Unicode combining marks are skipped so Hebrew niqqud is not stranded at a passage start. If the snapped span would fall below `max(min_passage_chars, size // 2)`, the raw range is restored (combining marks are still skipped).
6. Touching or overlapping ranges coalesce. Overlap merges only remove characters, so the allowance still holds.
7. Passages stay in document order.

The starting scale uses a per-document ratio from that document's own leading `PROBE_CHARS` (4,000) characters. The full string is not kept between the length scan and passage planning. There is no universal characters-per-token constant. After assembly, the context is measured exactly. If it is over the source allowance, the scale is multiplied by `allowance / measured * 0.97` and the loop repeats, at most 4 measurements. Failure after that is `BUDGET_NOT_CONVERGED`.

Assembly:

- Each document is `### Document {i}: {label}\n` plus its passages joined by `\n[...]\n`.
- Documents are separated by a blank line.
- Labels have control characters removed, whitespace collapsed, and length capped at 120. An empty label becomes `Document {i}`.
- Headers, separators, and gap markers are formatting. They are counted in `formatting_chars` and are never inside a `PassageRange`.

`read_text` is called at most `1 + MAX_BUDGET_ITERATIONS` times per document (one length/probe scan plus one read per measurement). Sampled mode holds one full document string at a time, plus the passage copies already taken. Full-text mode holds the accepted texts (at most three) and the joined context.

## Metadata

`SelectionResult.metadata` is a frozen dataclass. It stores offsets and counts, never source text.

| Field | Meaning |
| --- | --- |
| `algorithm_version` | `content-selection/1` |
| `seed` | The caller seed |
| `mode` | `FULL_TEXT` or `SAMPLED` |
| `model_name` / `encoding_name` | Requested model and the vetted encoding (`o200k_base` for the models in the table) |
| `unique_document_count` | `N` |
| `documents` | Per-document id, mode, original and selected character counts, baseline and effective allowances, passage count, half-open ranges, and `reduction_reason` |
| `reduction_reason` | `None`, `GLOBAL_TOKEN_BUDGET`, or `USABILITY_FLOOR_APPLIED` |
| `skipped_empty_document_ids` | Blank sources, in first-seen order |
| `source_chars_total` / `selected_source_chars_total` / `formatting_chars` | Original accepted characters, selected characters, and everything else in `context` |
| `context_tokens_measured` | Tokenizer count of the full assembled context |
| `overhead_tokens_declared` | `system_prompt + schema + extra_hints` (not re-tokenized into the context) |
| `safety_margin_tokens` | Declared margin |
| `source_token_allowance` | Maximum context tokens this request may use |
| `operational_input_token_budget` / `context_window_tokens` | The two input ceilings the caller supplied |
| `token_estimate_kind` | `MEASURED_TEXT_PLUS_DECLARED_OVERHEAD` |
| `budget_iterations` | Exact measurements performed (1–4) |
| `global_scale_permille` | `1000` at the baseline; lower after a uniform shrink |

Source allowance:

```text
input_tokens    = context_tokens_measured + overhead.input_side()
accounted_total = input_tokens + reserved_output + safety_margin
A accounted_total <= context_window_tokens
B input_tokens    <= operational_input_token_budget
C reserved_output <= max_output_tokens

source_token_allowance =
    min(operational_input_token_budget,
        context_window_tokens - reserved_output - safety_margin)
    - overhead.input_side()
```

`reserved_output_tokens(n) = min(max_output_tokens, 400 * n + 500)`. Twenty questions reserve 8,500 tokens when the output cap allows it.

## Errors

All inherit `ContentSelectionError` and expose `code` plus `details`. Details contain counts, ids, and token numbers only.

| Code | When | Phase 2 HTTP |
| --- | --- | --- |
| `NO_DOCUMENTS_SELECTED` | Empty selection | 400 |
| `INVALID_DOCUMENT_ID` | Id empty after `.strip()` | 400 |
| `CONFLICTING_DUPLICATE_DOCUMENT_ID` | Same id, different text, eager API | Failure (programming error) |
| `ALL_SOURCES_EMPTY` | Every unique document is blank | 400 |
| `FULL_TEXT_OVER_BUDGET` | `N <= 3` and the full request does not fit | Surface the budget failure; do not sample |
| `INSUFFICIENT_PASSAGE_BUDGET` | Usability floors do not fit | Ask the user to select fewer documents |
| `BUDGET_NOT_CONVERGED` | Four measurements all stayed over the allowance | Failure |
| `UNSUPPORTED_MODEL` | `model_name` is not in the vetted tables | Failure until the model is added explicitly |
| `TOKENIZER_UNAVAILABLE` | tiktoken or the vendored `o200k_base` blob cannot be loaded | Failure; do not download |
| `INVALID_BUDGET_CONFIGURATION` | Non-positive allowance, reserved output above the model max, or overhead larger than capacity | Failure |

## Configuration

Verified from this repository:

| Item | Value |
| --- | --- |
| Default model | `gpt-4.1-mini`, overridable with `OPENAI_MODEL_NAME` |
| Lambda runtime | `python3.9` for every function in `backend/template.yaml` |
| Worker | Timeout 300 s, memory 512 MB, `MaximumRetryAttempts: 0` |
| Tokenizer pin | `tiktoken>=0.11.0,<0.12.0` (0.11.0 is the first release with a `gpt-4.1` → `o200k_base` mapping and the last with a cp39 wheel) |
| Encoding blob | `backend/src/tiktoken_cache/fb374d419588a4632f3f557e76b4b70aebbca790` |
| Live quiz cap today | 12,000 characters, still in force until Phase 2 |
| Build | `backend/build.ps1`. `--use-container` is required so Windows does not package `win_amd64` wheels |

Assumptions, not read from the repository, and meant to be overridable by env in Phase 2:

| Knob | Recommended default | What it is |
| --- | --- | --- |
| `QUIZ_MODEL_CONTEXT_WINDOW_TOKENS` | `1_047_576` for `gpt-4.1-mini` | Model context window from public documentation |
| `QUIZ_MODEL_MAX_OUTPUT_TOKENS` | `32_768` for `gpt-4.1-mini` | Model output cap from public documentation |
| `QUIZ_INPUT_TOKEN_BUDGET` | `120_000` | Application policy. Far below the context window, to bound latency and cost. Not a latency guarantee |
| `QUIZ_SAFETY_MARGIN_TOKENS` | `1_500` | Chat framing, role tokens, and schema serialization the local count can miss |
| `QUIZ_MIN_PASSAGE_CHARS` | `400` | Usable-passage floor |
| `QUIZ_TARGET_PASSAGE_CHARS` | `1200` | Target passage length |
| `QUIZ_MAX_PASSAGES_PER_DOCUMENT` | `12` | Cap on strata |

Also stored as code defaults: `gpt-4.1` and `gpt-4.1-nano` use the same 1,047,576 / 32,768 limits; `gpt-4o` and `gpt-4o-mini` use 128,000 / 16,384. Those figures are the same kind of public-doc assumption. An unknown model raises `UNSUPPORTED_MODEL` instead of guessing an encoding.

Not determinable here: the OpenAI account's TPM/RPM limits. Phase 3 has to measure them. They are a fourth constraint, separate from the context window, the output cap, and the operational input budget.

## Phase 2 integration

The selector is wired into the quiz worker via `select_quiz_content_streaming`. Document ids are deduplicated in `_parse_api_request`. Concurrency is owned by the generation record / lease (`generation_id`), not by `documents.processing_status`.

```text
Document status  = document ingestion/processing health
                   UPLOADED -> PROCESSING -> READY
                   or UPLOADED/PROCESSING -> FAILED / ERROR

Generation status = one quiz-generation request
                    GENERATING -> READY
                    or GENERATING -> FAILED
```

A quiz-generation failure must not change a valid document's `processing_status`. The frontend polls the current `generation_id`, not source-document badges.

Known follow-ups that remain:

- Topics are extracted from the first 100,000 characters, while sampling can draw from the rest of the document. The schema `enum` is strict.
- Three very large documents that used to succeed under the 12,000-character cap will return `FULL_TEXT_OVER_BUDGET`.
- `python3.9` forces `tiktoken < 0.12`. A runtime upgrade (3.12) is the way to lift that pin.
- `process_document.py` contains duplicated imports and duplicated helper definitions. The bodies match; clean that up separately.
