import hashlib
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from token_budget import (  # noqa: E402
    DEFAULT_OPERATIONAL_INPUT_TOKEN_BUDGET,
    DEFAULT_SAFETY_MARGIN_TOKENS,
    MODEL_ENCODINGS,
    MODEL_LIMITS,
    ContentSelectionError,
    InvalidBudgetConfigurationError,
    OverheadTokens,
    TokenizerUnavailableError,
    UnsupportedModelError,
    count_overhead_tokens,
    encoding_cache_filename,
    encoding_name_for_model,
    get_token_counter,
    model_limits,
    reserved_output_tokens,
    source_token_allowance,
    vendored_cache_dir,
)


class _Chars:
    name = "chars"

    def count(self, text):
        return len(text)


class _Config:
    def __init__(self, operational, window, max_output):
        self.operational_input_token_budget = operational
        self.context_window_tokens = window
        self.max_output_tokens = max_output


class ModelTableTests(unittest.TestCase):
    def test_gpt_4_1_mini_encoding_and_limits(self):
        self.assertEqual(encoding_name_for_model("gpt-4.1-mini"), "o200k_base")
        limits = model_limits("gpt-4.1-mini")
        self.assertEqual(limits.context_window_tokens, 1_047_576)
        self.assertEqual(limits.max_output_tokens, 32_768)
        self.assertEqual(MODEL_ENCODINGS["gpt-4.1-mini"], "o200k_base")
        self.assertIn("gpt-4.1-mini", MODEL_LIMITS)

    def test_unknown_model_is_unsupported(self):
        with self.assertRaises(UnsupportedModelError) as caught:
            encoding_name_for_model("not-a-real-model")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_MODEL")
        self.assertEqual(caught.exception.details["model_name"], "not-a-real-model")
        self.assertIn("gpt-4.1-mini", caught.exception.details["supported"])
        self.assertNotIn("source text", str(caught.exception))

    def test_unknown_model_limits(self):
        with self.assertRaises(UnsupportedModelError) as caught:
            model_limits("gpt-4.1-mini-extra")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_MODEL")

    def test_recommended_budget_constants(self):
        self.assertEqual(DEFAULT_OPERATIONAL_INPUT_TOKEN_BUDGET, 120_000)
        self.assertEqual(DEFAULT_SAFETY_MARGIN_TOKENS, 1_500)


class ArithmeticTests(unittest.TestCase):
    def test_reserved_output_for_twenty_questions(self):
        self.assertEqual(
            reserved_output_tokens(20, max_output_tokens=32_768),
            8_500,
        )

    def test_reserved_output_is_capped(self):
        self.assertEqual(reserved_output_tokens(100, max_output_tokens=1_000), 1_000)

    def test_source_allowance_uses_the_tighter_constraint(self):
        config = _Config(operational=300, window=1_000, max_output=400)
        overhead = OverheadTokens(50, 20, 10, 100, 30)
        # min(300, 1000-100-30) - (50+20+10) = 220
        self.assertEqual(source_token_allowance(config, overhead), 220)
        self.assertEqual(overhead.input_side(), 80)
        self.assertEqual(overhead.total(), 210)

    def test_window_can_be_tighter_than_operational_budget(self):
        config = _Config(operational=10_000, window=500, max_output=400)
        overhead = OverheadTokens(10, 0, 0, 100, 50)
        # min(10000, 500-100-50) - 10 = 340
        self.assertEqual(source_token_allowance(config, overhead), 340)

    def test_reserved_output_above_max_output_is_invalid(self):
        config = _Config(operational=1_000, window=2_000, max_output=400)
        overhead = OverheadTokens(0, 0, 0, 401, 0)
        with self.assertRaises(InvalidBudgetConfigurationError) as caught:
            source_token_allowance(config, overhead)
        self.assertEqual(caught.exception.code, "INVALID_BUDGET_CONFIGURATION")
        self.assertEqual(
            caught.exception.details["reason"], "reserved_output_exceeds_max_output"
        )

    def test_overhead_alone_exceeding_capacity_is_invalid(self):
        config = _Config(operational=1_000, window=1_000, max_output=400)
        overhead = OverheadTokens(900, 0, 0, 100, 50)
        with self.assertRaises(InvalidBudgetConfigurationError) as caught:
            source_token_allowance(config, overhead)
        self.assertEqual(caught.exception.details["reason"], "overhead_exceeds_capacity")

    def test_non_positive_operational_budget_is_invalid(self):
        config = _Config(operational=0, window=1_000, max_output=100)
        overhead = OverheadTokens(0, 0, 0, 0, 0)
        with self.assertRaises(InvalidBudgetConfigurationError) as caught:
            source_token_allowance(config, overhead)
        self.assertEqual(caught.exception.code, "INVALID_BUDGET_CONFIGURATION")

    def test_count_overhead_uses_the_injected_counter(self):
        overhead = count_overhead_tokens(
            _Chars(),
            system_prompt="abcd",
            schema_json="{}",
            extra_hints="",
            reserved_output=80,
            safety_margin=15,
        )
        self.assertEqual(overhead.system_prompt, 4)
        self.assertEqual(overhead.schema, 2)
        self.assertEqual(overhead.extra_hints, 0)
        self.assertEqual(overhead.input_side(), 6)

    def test_errors_inherit_content_selection_error(self):
        self.assertTrue(issubclass(UnsupportedModelError, ContentSelectionError))
        self.assertTrue(issubclass(TokenizerUnavailableError, ContentSelectionError))
        self.assertTrue(issubclass(InvalidBudgetConfigurationError, ContentSelectionError))


class VendoredTokenizerTests(unittest.TestCase):
    def test_cache_filename_matches_sha1_of_the_encoding_url(self):
        filename = encoding_cache_filename("o200k_base")
        self.assertEqual(filename, "fb374d419588a4632f3f557e76b4b70aebbca790")
        blob = os.path.join(vendored_cache_dir(), filename)
        self.assertTrue(os.path.isfile(blob))
        self.assertGreater(os.path.getsize(blob), 0)

    def test_o200k_base_loads_without_network(self):
        try:
            import tiktoken  # noqa: F401
        except ImportError:
            self.skipTest("tiktoken is not installed")

        os.environ.pop("TIKTOKEN_CACHE_DIR", None)

        def _blocked(*_args, **_kwargs):
            raise OSError("network disabled")

        with patch("socket.socket", _blocked), patch("socket.create_connection", _blocked):
            counter = get_token_counter("gpt-4.1-mini")
        self.assertEqual(counter.name, "o200k_base")
        self.assertGreater(counter.count("hello"), 0)
        self.assertGreaterEqual(counter.count("hello hello"), counter.count("hello"))
        hebrew = counter.count("שלום עולם")
        self.assertGreater(hebrew, 0)
        self.assertEqual(os.environ.get("TIKTOKEN_CACHE_DIR"), vendored_cache_dir())

    def test_missing_cache_does_not_download(self):
        os.environ["TIKTOKEN_CACHE_DIR"] = os.path.join(
            os.path.dirname(__file__), "missing-tiktoken-cache"
        )
        try:
            # Drop any counter cached against the real directory.
            import token_budget

            token_budget._COUNTER_CACHE.clear()
            with self.assertRaises(TokenizerUnavailableError) as caught:
                get_token_counter("gpt-4.1-mini")
            self.assertEqual(caught.exception.code, "TOKENIZER_UNAVAILABLE")
            self.assertEqual(caught.exception.details["cause_type"], "FileNotFoundError")
            self.assertEqual(caught.exception.details["encoding_name"], "o200k_base")
            self.assertNotIn("http", str(caught.exception).lower())
        finally:
            os.environ.pop("TIKTOKEN_CACHE_DIR", None)
            import token_budget

            token_budget._COUNTER_CACHE.clear()


class CacheKeyTests(unittest.TestCase):
    def test_documented_url_hashes_to_the_vendored_filename(self):
        url = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
        self.assertEqual(
            hashlib.sha1(url.encode("utf-8")).hexdigest(),
            "fb374d419588a4632f3f557e76b4b70aebbca790",
        )
