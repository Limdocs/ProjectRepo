import ast
import os
import subprocess
import sys
import textwrap
import unicodedata
import unittest
import weakref

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from content_selection import (  # noqa: E402
    ALGORITHM_VERSION,
    GAP_MARKER,
    MAX_BUDGET_ITERATIONS,
    TOKEN_ESTIMATE_KIND,
    AllSourcesEmptyError,
    BudgetNotConvergedError,
    ConflictingDuplicateDocumentIdError,
    ContentSelectionError,
    DocumentRef,
    DocumentSelection,
    FullTextOverBudgetError,
    InsufficientPassageBudgetError,
    InvalidDocumentIdError,
    NoDocumentsSelectedError,
    OverheadTokens,
    SelectionConfig,
    SourceDocument,
    UnsupportedModelError,
    select_quiz_content,
    select_quiz_content_streaming,
)


SENTINEL = "UNIQUE_SOURCE_SENTINEL_9f3a"


class CharCounter:
    name = "chars"

    def count(self, text):
        return 0 if not text else len(text)


class HeaderInflatedCounter:
    """Counts source text normally and any assembled context as a huge constant."""

    name = "pathological"

    def count(self, text):
        if not text:
            return 0
        if "### Document" in text:
            return 1_000_000
        return len(text)


def _config(**overrides):
    values = dict(
        model_name="gpt-4.1-mini",
        context_window_tokens=2_000_000,
        max_output_tokens=32_768,
        operational_input_token_budget=1_000_000,
        seed=12345,
    )
    values.update(overrides)
    return SelectionConfig(**values)


def _overhead(reserved=100, margin=50, prompt=0, schema=0, hints=0):
    return OverheadTokens(prompt, schema, hints, reserved, margin)


def _select(documents, **overrides):
    return select_quiz_content(
        documents,
        config=_config(**overrides),
        overhead=_overhead(),
        token_counter=CharCounter(),
    )


def _prose(length, alphabet="abcdefghijklmnopqrstuvwxyz "):
    unit = "Alpha beta gamma. Delta epsilon zeta.\n"
    base = (unit * ((length // len(unit)) + 2))[:length]
    return base if len(base) == length else (base + "a" * length)[:length]


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)
    elif hasattr(value, "__dataclass_fields__"):
        for field in value.__dataclass_fields__:
            yield from _walk_strings(getattr(value, field))


def _assert_no_sentinel(testcase, payload):
    for text in _walk_strings(payload):
        testcase.assertNotIn(SENTINEL, text)


class FullTextPolicyTests(unittest.TestCase):
    def test_one_two_and_three_documents_keep_every_character(self):
        for count in (1, 2, 3):
            documents = [
                SourceDocument(
                    "doc-%s" % index,
                    SENTINEL + _prose(15_000 + index),
                    "Label %s" % index,
                )
                for index in range(count)
            ]
            result = _select(documents)
            self.assertEqual(result.metadata.mode, "FULL_TEXT")
            self.assertEqual(result.metadata.unique_document_count, count)
            self.assertEqual(result.metadata.global_scale_permille, 1000)
            self.assertEqual(result.metadata.token_estimate_kind, TOKEN_ESTIMATE_KIND)
            self.assertNotIn("12000", result.metadata.algorithm_version)
            for document, selection in zip(documents, result.metadata.documents):
                self.assertEqual(selection.mode, "FULL_TEXT")
                self.assertEqual(selection.original_chars, len(document.text))
                self.assertEqual(selection.selected_chars, len(document.text))
                self.assertGreater(selection.original_chars, 12_000)
                self.assertEqual(
                    selection.ranges,
                    (type(selection.ranges[0])(0, len(document.text)),),
                )
                self.assertIn(document.text, result.context)
            self.assertEqual(
                result.metadata.formatting_chars,
                len(result.context) - result.metadata.selected_source_chars_total,
            )
            _assert_no_sentinel(self, result.metadata)

    def test_full_text_overflow_does_not_fall_back_to_sampling(self):
        document = SourceDocument("only", "x" * 2_000, "Only")
        wide = _select([document])
        measured = wide.metadata.context_tokens_measured
        with self.assertRaises(FullTextOverBudgetError) as caught:
            _select(
                [document],
                operational_input_token_budget=measured - 1,
                context_window_tokens=2_000_000,
            )
        self.assertEqual(caught.exception.code, "FULL_TEXT_OVER_BUDGET")
        self.assertEqual(caught.exception.details["unique_document_count"], 1)
        self.assertIn("required_input_tokens", caught.exception.details)
        self.assertIn("source_token_allowance", caught.exception.details)
        self.assertNotIn("SAMPLED", str(caught.exception))
        self.assertNotIn("xxxx", str(caught.exception))

        with self.assertRaises(FullTextOverBudgetError) as window_caught:
            _select(
                [document],
                operational_input_token_budget=1_000_000,
                context_window_tokens=measured + 100 + 50 - 1,
            )
        self.assertEqual(window_caught.exception.code, "FULL_TEXT_OVER_BUDGET")

    def test_three_documents_over_budget_stay_full_text_failures(self):
        documents = [SourceDocument("d%s" % i, "y" * 3_000, "L%s" % i) for i in range(3)]
        wide = _select(documents)
        with self.assertRaises(FullTextOverBudgetError):
            _select(
                documents,
                operational_input_token_budget=wide.metadata.context_tokens_measured - 1,
            )


class SamplingPolicyTests(unittest.TestCase):
    def test_baseline_formula_continues_past_ten_documents(self):
        length = 10_000
        for count, fraction_num in ((4, 75), (10, 30), (20, 15), (50, 6)):
            documents = [
                SourceDocument("d%s" % index, _prose(length), "L%s" % index)
                for index in range(count)
            ]
            result = _select(documents, operational_input_token_budget=2_000_000)
            self.assertEqual(result.metadata.mode, "SAMPLED")
            self.assertEqual(result.metadata.unique_document_count, count)
            expected = (length * 3) // count
            self.assertEqual(expected, (length * fraction_num) // 100)
            for selection in result.metadata.documents:
                self.assertEqual(selection.baseline_allowance_chars, expected)
                self.assertLessEqual(selection.effective_allowance_chars, expected)
                self.assertLessEqual(selection.selected_chars, selection.effective_allowance_chars)
                self.assertGreaterEqual(
                    selection.selected_chars, min(400, selection.original_chars)
                )

    def test_baseline_is_an_upper_bound_when_the_floor_does_not_apply(self):
        documents = [SourceDocument("d%s" % i, _prose(8_000), "L%s" % i) for i in range(4)]
        result = _select(documents)
        for selection in result.metadata.documents:
            self.assertGreater(selection.baseline_allowance_chars, 400)
            self.assertLessEqual(
                selection.effective_allowance_chars, selection.baseline_allowance_chars
            )
            self.assertIsNone(selection.reduction_reason)

    def test_short_documents_receive_the_usability_floor(self):
        documents = [SourceDocument("short", "hi " * 3, "Short")]
        documents.extend(
            SourceDocument("long-%s" % i, _prose(4_000), "Long %s" % i) for i in range(3)
        )
        result = _select(documents)
        short = result.metadata.documents[0]
        self.assertEqual(short.original_chars, len(documents[0].text))
        self.assertLessEqual(short.original_chars, 400)
        self.assertEqual(short.selected_chars, short.original_chars)
        self.assertEqual(short.reduction_reason, "USABILITY_FLOOR_APPLIED")
        self.assertGreater(short.effective_allowance_chars, short.baseline_allowance_chars)

    def test_unequal_lengths_keep_each_documents_share(self):
        lengths = (10, 500, 1_000, 500_000)
        documents = [
            SourceDocument("d%s" % index, _prose(length), "L%s" % index)
            for index, length in enumerate(lengths)
        ]
        result = _select(
            documents,
            operational_input_token_budget=2_000_000,
            context_window_tokens=3_000_000,
        )
        self.assertEqual(result.metadata.mode, "SAMPLED")
        for document, selection in zip(documents, result.metadata.documents):
            baseline = (len(document.text) * 3) // 4
            self.assertEqual(selection.baseline_allowance_chars, baseline)
            self.assertGreaterEqual(
                selection.selected_chars, min(400, selection.original_chars)
            )
        short = result.metadata.documents[0]
        long = result.metadata.documents[-1]
        self.assertEqual(short.selected_chars, 10)
        self.assertGreater(long.selected_chars, short.selected_chars * 100)
        self.assertLessEqual(long.effective_allowance_chars, long.baseline_allowance_chars)

    def test_global_scale_reduces_every_document_by_the_same_factor(self):
        documents = [SourceDocument("d%s" % i, _prose(8_000), "L%s" % i) for i in range(4)]
        wide = _select(documents)
        self.assertEqual(wide.metadata.global_scale_permille, 1000)
        tight_budget = max(2_000, wide.metadata.context_tokens_measured // 2)
        tight = _select(documents, operational_input_token_budget=tight_budget)
        effectives = [item.effective_allowance_chars for item in tight.metadata.documents]
        self.assertEqual(len(set(effectives)), 1)
        self.assertLess(effectives[0], wide.metadata.documents[0].effective_allowance_chars)
        self.assertEqual(effectives[0], effectives[-1])
        for item in tight.metadata.documents:
            self.assertLessEqual(item.effective_allowance_chars, item.baseline_allowance_chars)
            self.assertGreaterEqual(item.selected_chars, 400)
            self.assertEqual(item.reduction_reason, "GLOBAL_TOKEN_BUDGET")
            self.assertIn(item.document_id, [doc.document_id for doc in documents])
        self.assertLessEqual(tight.metadata.budget_iterations, MAX_BUDGET_ITERATIONS)
        self.assertLessEqual(
            tight.metadata.context_tokens_measured + _overhead().input_side(),
            tight_budget,
        )


class IdentityTests(unittest.TestCase):
    def test_repeated_id_collapses_and_keeps_first_label_and_order(self):
        text = _prose(2_000) + SENTINEL
        documents = [
            SourceDocument("  b  ", text, "first-b"),
            SourceDocument("a", _prose(1_800), "label-a"),
            SourceDocument("b", text, "second-b"),
        ]
        result = _select(documents)
        self.assertEqual(
            [item.document_id for item in result.metadata.documents],
            ["b", "a"],
        )
        self.assertEqual(result.metadata.unique_document_count, 2)
        self.assertEqual(result.context.count(SENTINEL), 1)
        self.assertIn("first-b", result.context)
        self.assertNotIn("second-b", result.context)

    def test_conflicting_duplicate_is_an_error(self):
        documents = [
            SourceDocument("same", "alpha " + SENTINEL, "one"),
            SourceDocument(" same ", "beta " + SENTINEL, "two"),
        ]
        with self.assertRaises(ConflictingDuplicateDocumentIdError) as caught:
            _select(documents)
        self.assertEqual(caught.exception.code, "CONFLICTING_DUPLICATE_DOCUMENT_ID")
        self.assertEqual(caught.exception.details["document_id"], "same")
        self.assertEqual(len(caught.exception.details["lengths"]), 2)
        self.assertNotIn(SENTINEL, str(caught.exception))

    def test_same_filename_and_different_ids_stay_distinct(self):
        documents = [
            SourceDocument("one", _prose(100) + " ONE", "notes.txt"),
            SourceDocument("two", _prose(100) + " TWO", "notes.txt"),
        ]
        result = _select(documents)
        self.assertEqual(result.metadata.unique_document_count, 2)
        self.assertIn(" ONE", result.context)
        self.assertIn(" TWO", result.context)

    def test_empty_selection_and_blank_id(self):
        with self.assertRaises(NoDocumentsSelectedError) as empty:
            _select([])
        self.assertEqual(empty.exception.code, "NO_DOCUMENTS_SELECTED")
        with self.assertRaises(InvalidDocumentIdError) as blank:
            _select([SourceDocument("   ", "hello", "x")])
        self.assertEqual(blank.exception.code, "INVALID_DOCUMENT_ID")

    def test_whitespace_only_sources_are_skipped_not_rewritten(self):
        kept = "kept  value  " + SENTINEL
        documents = [
            SourceDocument("blank", "  \n\t", "ignored"),
            SourceDocument("kept", kept, "Kept"),
        ]
        result = _select(documents)
        self.assertEqual(result.metadata.skipped_empty_document_ids, ("blank",))
        self.assertEqual(result.metadata.unique_document_count, 1)
        self.assertIn(kept, result.context)
        self.assertNotIn("ignored", result.context)
        _assert_no_sentinel(self, result.metadata)

        with self.assertRaises(AllSourcesEmptyError) as caught:
            _select(
                [
                    SourceDocument("a", " "),
                    SourceDocument("b", "\n\t"),
                ]
            )
        self.assertEqual(caught.exception.code, "ALL_SOURCES_EMPTY")
        self.assertEqual(caught.exception.details["document_ids"], ["a", "b"])


class RangeAndOrderingTests(unittest.TestCase):
    def _sampled(self):
        documents = [
            SourceDocument("c", _prose(4_000), "C"),
            SourceDocument("a", _prose(4_000), "A"),
            SourceDocument("b", _prose(4_000), "B"),
            SourceDocument("d", _prose(4_000), "D"),
        ]
        return documents, _select(documents, seed=7)

    def test_ranges_are_valid_and_reproduce_the_body(self):
        documents, result = self._sampled()
        by_id = {document.document_id: document.text for document in documents}
        for selection in result.metadata.documents:
            text = by_id[selection.document_id]
            previous = -1
            slices = []
            for passage in selection.ranges:
                self.assertGreaterEqual(passage.start, 0)
                self.assertGreater(passage.end, passage.start)
                self.assertLessEqual(passage.end, len(text))
                self.assertGreater(passage.start, previous)
                previous = passage.start
                self.assertEqual(unicodedata.combining(text[passage.start]), 0)
                slices.append(text[passage.start : passage.end])
            body = GAP_MARKER.join(slices)
            self.assertIn(body, result.context)
            self.assertEqual(selection.passage_count, len(selection.ranges))

    def test_document_order_follows_first_appearance(self):
        documents, result = self._sampled()
        self.assertEqual(
            [item.document_id for item in result.metadata.documents],
            [document.document_id for document in documents],
        )
        positions = [result.context.index("### Document %s:" % (index + 1)) for index in range(4)]
        self.assertEqual(positions, sorted(positions))

    def test_sampled_ranges_cover_middle_and_end(self):
        _documents, result = self._sampled()
        for selection in result.metadata.documents:
            count = selection.passage_count
            if count < 2:
                continue
            length = selection.original_chars
            self.assertGreaterEqual(
                selection.ranges[-1].start, (count - 1) * length // count
            )
            self.assertLess(selection.ranges[0].start, length // count)

    def test_unicode_hebrew_english_emoji_and_combining_marks(self):
        unit = "בְּרֵאשִׁית בָּרָא. Hello world 😀 עולם!\n"
        self.assertTrue(any(unicodedata.combining(char) for char in unit))
        documents = [
            SourceDocument("he", unit * 400, "עברית"),
            SourceDocument("en", ("The quick brown fox jumps.\n" * 400), "English"),
            SourceDocument("mix", (unit + "Second sentence here.\n") * 200, "Mixed"),
            SourceDocument("emoji", ("Line with 😀 and more text.\n" * 300), "Emoji"),
        ]
        result = _select(documents, seed=3)
        self.assertEqual(result.context.encode("utf-8").decode("utf-8"), result.context)
        by_id = {document.document_id: document.text for document in documents}
        for selection in result.metadata.documents:
            text = by_id[selection.document_id]
            for passage in selection.ranges:
                self.assertEqual(unicodedata.combining(text[passage.start]), 0)
                self.assertIn(text[passage.start : passage.end], result.context)


class BudgetBoundaryTests(unittest.TestCase):
    def test_success_stays_inside_operational_and_window_budgets(self):
        documents = [SourceDocument("d%s" % i, _prose(6_000), "L%s" % i) for i in range(4)]
        wide = _select(documents)
        measured = wide.metadata.context_tokens_measured
        overhead = _overhead()
        just_under = _select(
            documents,
            operational_input_token_budget=measured,
            context_window_tokens=measured + overhead.reserved_output + overhead.safety_margin,
        )
        meta = just_under.metadata
        input_tokens = meta.context_tokens_measured + overhead.input_side()
        self.assertLessEqual(input_tokens, meta.operational_input_token_budget)
        self.assertLessEqual(
            input_tokens + overhead.reserved_output + overhead.safety_margin,
            meta.context_window_tokens,
        )
        self.assertIn("### Document 1:", just_under.context)
        self.assertIn(GAP_MARKER, just_under.context)

        tighter = _select(documents, operational_input_token_budget=max(2_500, measured - 1))
        self.assertLessEqual(
            tighter.metadata.context_tokens_measured,
            max(2_500, measured - 1),
        )
        self.assertEqual(tighter.metadata.unique_document_count, 4)
        self.assertLess(
            tighter.metadata.selected_source_chars_total,
            wide.metadata.selected_source_chars_total,
        )

    def test_insufficient_passage_budget_drops_nothing(self):
        documents = [SourceDocument("d%s" % i, _prose(50), "L%s" % i) for i in range(30)]
        with self.assertRaises(InsufficientPassageBudgetError) as caught:
            _select(documents, operational_input_token_budget=200, context_window_tokens=10_000)
        details = caught.exception.details
        self.assertEqual(details["unique_document_count"], 30)
        self.assertEqual(details["floor_chars_total"], 50 * 30)
        self.assertEqual(details["documents_at_floor"], 30)
        self.assertLess(details["source_token_allowance"], details["floor_chars_total"])
        self.assertNotIn("Alpha", str(caught.exception))

    def test_pathological_counter_does_not_converge(self):
        documents = [SourceDocument("d%s" % i, _prose(2_000), "L%s" % i) for i in range(4)]
        with self.assertRaises(BudgetNotConvergedError) as caught:
            select_quiz_content(
                documents,
                config=_config(operational_input_token_budget=8_000),
                overhead=_overhead(),
                token_counter=HeaderInflatedCounter(),
            )
        self.assertEqual(caught.exception.details["iterations"], MAX_BUDGET_ITERATIONS)
        self.assertEqual(caught.exception.details["last_measured_tokens"], 1_000_000)
        self.assertLessEqual(MAX_BUDGET_ITERATIONS, 4)

    def test_every_accepted_document_appears(self):
        documents = [SourceDocument("d%s" % i, _prose(1_500), "Name%s" % i) for i in range(6)]
        result = _select(documents, operational_input_token_budget=4_000)
        self.assertEqual(len(result.metadata.documents), 6)
        for selection in result.metadata.documents:
            self.assertGreaterEqual(selection.selected_chars, min(400, selection.original_chars))
            self.assertIn("Name%s" % selection.document_id[-1], result.context)

    def test_unknown_model_fails_before_sampling(self):
        with self.assertRaises(UnsupportedModelError) as caught:
            _select(
                [SourceDocument("d", "hello", "L")],
                model_name="unknown-model",
            )
        self.assertEqual(caught.exception.code, "UNSUPPORTED_MODEL")
        self.assertNotIn("hello", str(caught.exception))


class ProbeRatioTests(unittest.TestCase):
    def test_starting_scale_uses_each_documents_own_probe(self):
        class PrefixRate:
            name = "prefix-rate"

            def count(self, text):
                if text.startswith("EXPENSIVE"):
                    return len(text) * 8
                return 0 if not text else len(text)

        documents = [SourceDocument("e", "EXPENSIVE\n" + _prose(5_000), "E")]
        documents.extend(
            SourceDocument("d%s" % index, _prose(5_000), "L%s" % index) for index in range(3)
        )
        budget = 30_000
        plain = _select(documents, operational_input_token_budget=budget)
        rated = select_quiz_content(
            documents,
            config=_config(operational_input_token_budget=budget),
            overhead=_overhead(),
            token_counter=PrefixRate(),
        )
        self.assertEqual(plain.metadata.global_scale_permille, 1000)
        self.assertLess(rated.metadata.global_scale_permille, 1000)
        self.assertLessEqual(rated.metadata.context_tokens_measured, budget)


class ReproducibilityTests(unittest.TestCase):
    def test_same_seed_is_byte_identical(self):
        documents = [SourceDocument("d%s" % i, _prose(3_000), "L%s" % i) for i in range(5)]
        first = _select(documents, seed=99)
        second = _select(documents, seed=99)
        self.assertEqual(first.context, second.context)
        self.assertEqual(first.metadata, second.metadata)
        self.assertEqual(first.metadata.algorithm_version, ALGORITHM_VERSION)

    def test_fresh_import_repeats_the_selection(self):
        src = os.path.join(os.path.dirname(__file__), "..", "src")
        script = textwrap.dedent(
            """
            import importlib
            from content_selection import SourceDocument, select_quiz_content
            from content_selection import SelectionConfig, OverheadTokens

            class Counter:
                name = "chars"
                def count(self, text):
                    return 0 if not text else len(text)

            documents = [
                SourceDocument("d%s" % i, ("Para %s.\\n" % (i % 7)) * 300, "L%s" % i)
                for i in range(4)
            ]
            config = SelectionConfig(
                model_name="gpt-4.1-mini",
                context_window_tokens=2_000_000,
                max_output_tokens=32_768,
                operational_input_token_budget=1_000_000,
                seed=5,
            )
            overhead = OverheadTokens(0, 0, 0, 100, 50)

            def run(module):
                return module.select_quiz_content(
                    documents, config=config, overhead=overhead, token_counter=Counter()
                )

            import content_selection
            first = run(content_selection)
            content_selection = importlib.reload(content_selection)
            second = run(content_selection)
            assert first.context == second.context
            assert [ (d.document_id, tuple((r.start, r.end) for r in d.ranges))
                     for d in first.metadata.documents ] == [
                     (d.document_id, tuple((r.start, r.end) for r in d.ranges))
                     for d in second.metadata.documents ]
            assert first.metadata.context_tokens_measured == second.metadata.context_tokens_measured
            print("ok")
            """
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = os.path.abspath(src)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ok", completed.stdout)


class StreamingTests(unittest.TestCase):
    def test_read_bound_and_single_full_text_resident(self):
        texts = {"d%s" % i: _prose(5_000) for i in range(4)}

        class Tracked(str):
            pass

        live = []
        calls = []
        max_live = {"value": 0}

        def read_text(document_id):
            alive = sum(1 for ref in live if ref() is not None)
            max_live["value"] = max(max_live["value"], alive)
            fresh = Tracked("".join(texts[document_id]))
            live.append(weakref.ref(fresh))
            calls.append(document_id)
            alive = sum(1 for ref in live if ref() is not None)
            max_live["value"] = max(max_live["value"], alive)
            return fresh

        refs = [DocumentRef("d%s" % i, "L%s" % i) for i in range(4)]
        refs.append(DocumentRef("d0", "ignored duplicate"))
        result = select_quiz_content_streaming(
            refs,
            read_text=read_text,
            config=_config(),
            overhead=_overhead(),
            token_counter=CharCounter(),
        )
        self.assertEqual(result.metadata.mode, "SAMPLED")
        self.assertEqual(result.metadata.unique_document_count, 4)
        per_id = {doc_id: calls.count(doc_id) for doc_id in texts}
        self.assertTrue(all(1 <= count <= 1 + MAX_BUDGET_ITERATIONS for count in per_id.values()))
        self.assertLessEqual(max_live["value"], 1)
        self.assertNotIn("ignored duplicate", result.context)


class IsolationTests(unittest.TestCase):
    def test_modules_do_not_import_boto3_or_openai(self):
        src = os.path.join(os.path.dirname(__file__), "..", "src")
        for name in ("content_selection.py", "passage_sampling.py", "token_budget.py"):
            path = os.path.join(src, name)
            tree = ast.parse(open(path, encoding="utf-8").read())
            modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        modules.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules.add(node.module.split(".")[0])
            self.assertNotIn("boto3", modules)
            self.assertNotIn("openai", modules)

    def test_import_and_select_without_aws_or_openai_env(self):
        src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
        script = textwrap.dedent(
            """
            import builtins
            import os
            real_import = builtins.__import__

            def guarded(name, globals=None, locals=None, fromlist=(), level=0):
                root = name.split(".")[0]
                if root in {"boto3", "openai"}:
                    raise RuntimeError("blocked import " + root)
                return real_import(name, globals, locals, fromlist, level)

            builtins.__import__ = guarded
            from content_selection import (
                OverheadTokens, SelectionConfig, SourceDocument, select_quiz_content,
            )

            class Counter:
                name = "chars"
                def count(self, text):
                    return 0 if not text else len(text)

            result = select_quiz_content(
                [SourceDocument("a", "hello selection text", "A"),
                 SourceDocument("b", "more text here", "B")],
                config=SelectionConfig(
                    model_name="gpt-4.1-mini",
                    context_window_tokens=10_000,
                    max_output_tokens=1_000,
                    operational_input_token_budget=8_000,
                    seed=1,
                ),
                overhead=OverheadTokens(1, 1, 0, 10, 5),
                token_counter=Counter(),
            )
            assert "hello selection text" in result.context
            assert result.metadata.mode == "FULL_TEXT"
            assert os.environ.get("AWS_ACCESS_KEY_ID") is None
            print("ok")
            """
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "PYTHONPATH": src,
            "PYTHONIOENCODING": "utf-8",
        }
        completed = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIn("ok", completed.stdout)

    def test_metadata_type_has_no_source_field(self):
        self.assertNotIn("text", DocumentSelection.__dataclass_fields__)
        self.assertTrue(issubclass(FullTextOverBudgetError, ContentSelectionError))
        self.assertTrue(issubclass(InsufficientPassageBudgetError, ContentSelectionError))
        self.assertTrue(issubclass(BudgetNotConvergedError, ContentSelectionError))
