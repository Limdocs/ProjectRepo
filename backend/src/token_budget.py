"""Model-aware token budget arithmetic for quiz content selection.

This module is the only part of content selection that imports tiktoken.
It performs no AWS or OpenAI I/O. The encoding blob under ``tiktoken_cache/``
is loaded locally so quiz generation does not download a tokenizer on first use.

``MODEL_LIMITS`` values are assumptions taken from public model documentation,
not from this repository. They are overridable by the caller via
``SelectionConfig`` in Phase 2.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Dict, Optional, Protocol


DEFAULT_OPERATIONAL_INPUT_TOKEN_BUDGET = 120_000
DEFAULT_SAFETY_MARGIN_TOKENS = 1_500
PROBE_CHARS = 4_000

# sha1(url) is the cache filename tiktoken's read_file_cached uses.
_O200K_BASE_URL = (
    "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
)
_ENCODING_URLS = {
    "o200k_base": _O200K_BASE_URL,
}

# Explicit table: an unvetted OPENAI_MODEL_NAME must fail closed rather than
# let tiktoken guess an encoding and download it.
MODEL_ENCODINGS = {
    "gpt-4.1-mini": "o200k_base",
    "gpt-4.1": "o200k_base",
    "gpt-4.1-nano": "o200k_base",
    "gpt-4o": "o200k_base",
    "gpt-4o-mini": "o200k_base",
}


@dataclass(frozen=True)
class ModelLimits:
    """Assumed context window and output cap. Not an account rate limit."""

    context_window_tokens: int
    max_output_tokens: int


# Assumptions (public OpenAI documentation, overridable in Phase 2 via env):
# gpt-4.1 family: 1,047,576 context / 32,768 output.
# gpt-4o family: 128,000 context / 16,384 output.
MODEL_LIMITS = {
    "gpt-4.1-mini": ModelLimits(1_047_576, 32_768),
    "gpt-4.1": ModelLimits(1_047_576, 32_768),
    "gpt-4.1-nano": ModelLimits(1_047_576, 32_768),
    "gpt-4o": ModelLimits(128_000, 16_384),
    "gpt-4o-mini": ModelLimits(128_000, 16_384),
}

_COUNTER_CACHE: Dict[tuple, "TokenCounter"] = {}


class ContentSelectionError(Exception):
    """Structured selection failure. ``details`` never contains source text."""

    def __init__(self, code: str, details: Optional[dict] = None):
        self.code = code
        self.details = details or {}
        super().__init__("%s: %s" % (code, self.details))


class UnsupportedModelError(ContentSelectionError):
    def __init__(self, model_name: str, supported: list):
        super().__init__(
            "UNSUPPORTED_MODEL",
            {"model_name": model_name, "supported": list(supported)},
        )


class TokenizerUnavailableError(ContentSelectionError):
    def __init__(self, encoding_name: str, cache_dir: str, cause_type: str):
        super().__init__(
            "TOKENIZER_UNAVAILABLE",
            {
                "encoding_name": encoding_name,
                "cache_dir": cache_dir,
                "cause_type": cause_type,
            },
        )


class InvalidBudgetConfigurationError(ContentSelectionError):
    def __init__(self, details: dict):
        super().__init__("INVALID_BUDGET_CONFIGURATION", details)


class TokenCounter(Protocol):
    name: str

    def count(self, text: str) -> int:
        ...


@dataclass(frozen=True)
class OverheadTokens:
    system_prompt: int
    schema: int
    extra_hints: int
    reserved_output: int
    safety_margin: int

    def input_side(self) -> int:
        return self.system_prompt + self.schema + self.extra_hints

    def total(self) -> int:
        return self.input_side() + self.reserved_output + self.safety_margin


def vendored_cache_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiktoken_cache")


def encoding_cache_filename(encoding_name: str) -> str:
    url = _ENCODING_URLS.get(encoding_name)
    if not url:
        raise KeyError(encoding_name)
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def encoding_name_for_model(model_name: str) -> str:
    try:
        return MODEL_ENCODINGS[model_name]
    except KeyError:
        raise UnsupportedModelError(model_name, sorted(MODEL_ENCODINGS))


def model_limits(model_name: str) -> ModelLimits:
    try:
        return MODEL_LIMITS[model_name]
    except KeyError:
        raise UnsupportedModelError(model_name, sorted(MODEL_LIMITS))


def reserved_output_tokens(
    requested_question_count: int, *, max_output_tokens: int
) -> int:
    """Generous per-question reservation, capped by the model's output limit.

    A question object is roughly 60-120 JSON tokens in English and more in
    Hebrew. 400 tokens per question plus 500 of structure is deliberate slack.
    Twenty questions reserve 8,500 tokens when the model output cap allows it.
    """
    if requested_question_count < 0 or max_output_tokens <= 0:
        raise InvalidBudgetConfigurationError(
            {
                "reason": "invalid_reserved_output_inputs",
                "requested_question_count": requested_question_count,
                "max_output_tokens": max_output_tokens,
            }
        )
    return min(max_output_tokens, 400 * requested_question_count + 500)


def count_overhead_tokens(
    counter: TokenCounter,
    *,
    system_prompt: str,
    schema_json: str,
    extra_hints: str,
    reserved_output: int,
    safety_margin: int,
) -> OverheadTokens:
    return OverheadTokens(
        system_prompt=counter.count(system_prompt),
        schema=counter.count(schema_json),
        extra_hints=counter.count(extra_hints),
        reserved_output=reserved_output,
        safety_margin=safety_margin,
    )


def source_token_allowance(config: object, overhead: OverheadTokens) -> int:
    """Maximum tokens the assembled context may consume.

    Characters drive sampling. Tokens decide whether the request is safe.
    Account rate limits are a separate constraint and are not part of this.

        input_tokens    = context_tokens + overhead.input_side()
        accounted_total = input_tokens + reserved_output + safety_margin
        A: accounted_total <= context_window_tokens
        B: input_tokens    <= operational_input_token_budget
        C: reserved_output <= max_output_tokens

        allowance = min(operational_input_token_budget,
                        context_window - reserved_output - safety_margin)
                    - overhead.input_side()
    """
    operational = getattr(config, "operational_input_token_budget")
    context_window = getattr(config, "context_window_tokens")
    max_output = getattr(config, "max_output_tokens")
    reserved = overhead.reserved_output
    margin = overhead.safety_margin
    input_side = overhead.input_side()

    if (
        operational <= 0
        or context_window <= 0
        or max_output <= 0
        or reserved < 0
        or margin < 0
        or input_side < 0
        or overhead.system_prompt < 0
        or overhead.schema < 0
        or overhead.extra_hints < 0
    ):
        raise InvalidBudgetConfigurationError(
            {
                "reason": "non_positive_budget",
                "operational_input_token_budget": operational,
                "context_window_tokens": context_window,
                "max_output_tokens": max_output,
            }
        )
    if reserved > max_output:
        raise InvalidBudgetConfigurationError(
            {
                "reason": "reserved_output_exceeds_max_output",
                "reserved_output": reserved,
                "max_output_tokens": max_output,
            }
        )

    window_input_room = context_window - reserved - margin
    if window_input_room <= input_side or operational <= input_side:
        raise InvalidBudgetConfigurationError(
            {
                "reason": "overhead_exceeds_capacity",
                "input_side_tokens": input_side,
                "window_input_room": window_input_room,
                "operational_input_token_budget": operational,
            }
        )

    allowance = min(operational, window_input_room) - input_side
    if allowance <= 0:
        raise InvalidBudgetConfigurationError(
            {
                "reason": "non_positive_allowance",
                "source_token_allowance": allowance,
            }
        )
    return allowance


def _resolve_cache_dir() -> str:
    configured = os.environ.get("TIKTOKEN_CACHE_DIR")
    if configured:
        return configured
    cache_dir = vendored_cache_dir()
    # Only set the variable when the process has not chosen a cache already.
    os.environ["TIKTOKEN_CACHE_DIR"] = cache_dir
    return cache_dir


def get_token_counter(model_name: str) -> TokenCounter:
    """Load a local tiktoken encoding for ``model_name``.

    Sets ``TIKTOKEN_CACHE_DIR`` to the packaged cache only when it is unset.
    Missing the vendored blob raises ``TokenizerUnavailableError`` instead of
    downloading the encoding on first use.
    """
    encoding_name = encoding_name_for_model(model_name)
    cache_dir = _resolve_cache_dir()
    cache_key = (encoding_name, cache_dir)
    cached = _COUNTER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        filename = encoding_cache_filename(encoding_name)
    except KeyError:
        raise TokenizerUnavailableError(encoding_name, cache_dir, "KeyError")

    blob_path = os.path.join(cache_dir, filename)
    if not os.path.isfile(blob_path):
        raise TokenizerUnavailableError(encoding_name, cache_dir, "FileNotFoundError")

    try:
        import tiktoken
    except Exception as exc:
        raise TokenizerUnavailableError(
            encoding_name, cache_dir, type(exc).__name__
        )

    try:
        encoding = tiktoken.get_encoding(encoding_name)
    except Exception as exc:
        raise TokenizerUnavailableError(
            encoding_name, cache_dir, type(exc).__name__
        )

    counter = _TiktokenCounter(encoding_name, encoding)
    _COUNTER_CACHE[cache_key] = counter
    return counter


class _TiktokenCounter:
    def __init__(self, encoding_name: str, encoding: object):
        self.name = encoding_name
        self._encoding = encoding

    def count(self, text: str) -> int:
        if not text:
            return 0
        # Source documents may contain strings that look like special tokens.
        return len(self._encoding.encode(text, disallowed_special=()))
