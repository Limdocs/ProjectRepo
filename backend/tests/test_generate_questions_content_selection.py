import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("DOCUMENTS_TABLE", "documents")
os.environ.setdefault("QUESTIONS_TABLE", "questions")
os.environ.setdefault("QUESTION_SETS_TABLE", "question_sets")
os.environ.setdefault("COURSES_TABLE", "courses")
os.environ.setdefault("PROCESSED_BUCKET", "processed")
os.environ.setdefault("USER_PROGRESS_TABLE", "user_progress")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_MODEL_NAME", "gpt-4.1-mini")
os.environ.setdefault("WORKER_FUNCTION_NAME", "worker")

import generate_questions  # noqa: E402
from quiz_fakes import QuizWorld  # noqa: E402


def _questions_payload(count, marker):
    questions = []
    for index in range(count):
        questions.append(
            {
                "question": "%s question %s" % (marker, index),
                "options": ["a", "b", "c", "d"],
                "correct_index": 0,
                "explanation": "because %s" % marker,
                "topics": ["General"],
                "difficulty": "Easy",
                "answer": "a",
            }
        )
    return json.dumps({"questions": questions})


class _Completion(object):
    def __init__(self, content):
        message = MagicMock()
        message.content = content
        choice = MagicMock()
        choice.message = message
        self.choices = [choice]


def _context(request_id="worker-req-1"):
    context = MagicMock()
    context.aws_request_id = request_id
    return context


def _worker_event(document_ids, generation_id, count=5, language="en", focus=False):
    return {
        "courseId": "course-1",
        "documentIds": document_ids,
        "generationId": generation_id,
        "apiRequestId": "cid-1",
        "requestedBy": "user-1",
        "requestedQuestionCount": count,
        "quizLanguage": language,
        "focusWeakTopics": focus,
    }


class ContentSelectionWiringTests(unittest.TestCase):
    def setUp(self):
        self.world = QuizWorld()
        self.world.bind(generate_questions)
        self.world.courses["course-1"] = {"course_id": "course-1", "owner_id": "user-1"}
        self._openai = MagicMock()
        self._openai.chat.completions.create.return_value = _Completion(
            _questions_payload(5, "alpha")
        )
        patcher = patch("generate_questions.get_openai_client", return_value=self._openai)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _prepare(self, count, text=None, generation_id="gen-1", topics=None):
        ids = []
        for index in range(count):
            document_id = "d%s" % index
            ids.append(document_id)
            self.world.add_document(document_id)
            if text is not None:
                self.world.s3["%s.txt" % document_id] = text
            if topics is not None:
                self.world.docs[document_id]["topics"] = topics
        self.world.sets[generate_questions._generation_set_id(generation_id)] = {
            "set_id": generate_questions._generation_set_id(generation_id),
            "generation_id": generation_id,
            "generation_status": "PENDING",
            "lease_expires_at": 10**12,
            "generation_course_id": "course-1",
            "document_ids": ids,
        }
        return ids

    def test_module_has_no_character_truncation_path(self):
        import inspect

        source = inspect.getsource(generate_questions)
        self.assertNotIn("def _build_balanced_context", source)
        self.assertNotIn("def _allocate_budgets", source)
        self.assertNotIn("truncate_source_text", source)
        self.assertNotIn("QUIZ_MAX_SOURCE_CHARS", source)

    def test_selector_refs_and_context_are_the_user_message(self):
        ids = self._prepare(2, text=("Usable source sentence about circuits. " * 30))
        real = generate_questions.select_quiz_content_streaming
        seen = {}

        def spy(refs, read_text, config, overhead, token_counter=None):
            result = real(
                refs,
                read_text=read_text,
                config=config,
                overhead=overhead,
                token_counter=token_counter,
            )
            seen["refs"] = refs
            seen["context"] = result.context
            seen["overhead"] = overhead
            seen["metadata"] = result.metadata
            return result

        with patch("generate_questions.select_quiz_content_streaming", side_effect=spy):
            result = generate_questions.worker_handler(
                _worker_event(ids, "gen-1"), _context()
            )
        self.assertTrue(result["ok"])
        self.assertEqual([ref.document_id for ref in seen["refs"]], ids)
        user_message = self._openai.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertTrue(user_message.startswith(seen["context"]))
        self.assertLessEqual(
            seen["metadata"].context_tokens_measured + seen["overhead"].input_side(),
            120000,
        )
        self.assertGreater(seen["overhead"].extra_hints, 0)

    def test_three_documents_use_full_text_and_four_are_sampled(self):
        body = "Paragraph about graphs and queues. " * 40
        three = self._prepare(3, text=body, generation_id="gen-full")
        with patch("generate_questions.select_quiz_content_streaming", wraps=generate_questions.select_quiz_content_streaming) as wrapped:
            generate_questions.worker_handler(_worker_event(three, "gen-full"), _context())
            self.assertEqual(wrapped.call_args.kwargs["config"].full_text_max_documents, 3)
        user_message = self._openai.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        for document_id in three:
            self.assertIn(body.strip(), self.world.s3["%s.txt" % document_id])
            self.assertIn("Paragraph about graphs and queues.", user_message)

        self._openai.chat.completions.create.reset_mock()
        four_world_ids = []
        generation_id = "gen-sampled"
        for index in range(4):
            document_id = "s%s" % index
            four_world_ids.append(document_id)
            self.world.add_document(document_id)
            self.world.s3["%s.txt" % document_id] = ("Section %s. " % index) * 200
        self.world.sets[generate_questions._generation_set_id(generation_id)] = {
            "set_id": generate_questions._generation_set_id(generation_id),
            "generation_id": generation_id,
            "generation_status": "PENDING",
            "lease_expires_at": 10**12,
            "generation_course_id": "course-1",
        }
        captured = {}
        real = generate_questions.select_quiz_content_streaming

        def spy(refs, read_text, config, overhead, token_counter=None):
            result = real(
                refs,
                read_text=read_text,
                config=config,
                overhead=overhead,
                token_counter=token_counter,
            )
            captured["mode"] = result.metadata.mode
            return result

        with patch("generate_questions.select_quiz_content_streaming", side_effect=spy):
            generate_questions.worker_handler(
                _worker_event(four_world_ids, generation_id), _context("worker-req-2")
            )
        self.assertEqual(captured["mode"], "SAMPLED")

    def test_full_text_over_budget_fails_before_openai(self):
        ids = self._prepare(2, text="token " * 4000, generation_id="gen-budget")
        previous = os.environ.get("QUIZ_INPUT_TOKEN_BUDGET")
        os.environ["QUIZ_INPUT_TOKEN_BUDGET"] = "4000"
        try:
            result = generate_questions.worker_handler(
                _worker_event(ids, "gen-budget"), _context()
            )
        finally:
            if previous is None:
                os.environ.pop("QUIZ_INPUT_TOKEN_BUDGET", None)
            else:
                os.environ["QUIZ_INPUT_TOKEN_BUDGET"] = previous
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CONTENT_FULL_TEXT_OVER_BUDGET")
        self._openai.chat.completions.create.assert_not_called()
        for document_id in ids:
            self.assertEqual(self.world.docs[document_id]["processing_status"], "READY")
            self.assertNotIn("failure_code", self.world.docs[document_id])
        row = self.world.sets[generate_questions._generation_set_id("gen-budget")]
        self.assertEqual(row["generation_status"], "FAILED")
        self.assertEqual(row["failure_code"], "CONTENT_FULL_TEXT_OVER_BUDGET")
        self.assertNotIn("course_id", row)

    def test_same_generation_id_is_byte_identical(self):
        ids = []
        generation_id = "gen-seed"
        for index in range(4):
            document_id = "seed-%s" % index
            ids.append(document_id)
            self.world.add_document(document_id)
            self.world.s3["%s.txt" % document_id] = ("Stable passage %s. " % index) * 80
        self.world.sets[generate_questions._generation_set_id(generation_id)] = {
            "set_id": generate_questions._generation_set_id(generation_id),
            "generation_id": generation_id,
            "generation_status": "GENERATING",
            "lease_expires_at": 10**12,
        }
        messages = []

        def _create(**kwargs):
            messages.append(kwargs["messages"][1]["content"])
            return _Completion(_questions_payload(5, "alpha"))

        self._openai.chat.completions.create.side_effect = _create
        event = _worker_event(ids, generation_id)
        generate_questions._generate_questions_worker(
            "course-1",
            ids,
            "cid",
            5,
            "en",
            requested_by="user-1",
            generation_id=generation_id,
            worker_request_id="req-a",
            commit_state={"committed": False},
        )
        self.world.sets[generate_questions._generation_set_id(generation_id)]["generation_status"] = "GENERATING"
        self.world.sets[generate_questions._generation_set_id(generation_id)].pop("course_id", None)
        self.world.sets[generate_questions._generation_set_id(generation_id)].pop("created_at", None)
        generate_questions._generate_questions_worker(
            "course-1",
            ids,
            "cid",
            5,
            "en",
            requested_by="user-1",
            generation_id=generation_id,
            worker_request_id="req-b",
            commit_state={"committed": False},
        )
        self.assertEqual(messages[0], messages[1])

    def test_short_source_hint_depends_on_selected_chars(self):
        ids = self._prepare(1, text="Short.", generation_id="gen-short")
        generate_questions.worker_handler(_worker_event(ids, "gen-short"), _context())
        short_message = self._openai.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertIn(generate_questions._SHORT_SOURCE_HINT, short_message)

        self._openai.chat.completions.create.reset_mock()
        long_id = "long-doc"
        self.world.add_document(long_id)
        self.world.s3["long-doc.txt"] = "Long source. " * 400
        self.world.sets[generate_questions._generation_set_id("gen-long")] = {
            "set_id": generate_questions._generation_set_id("gen-long"),
            "generation_id": "gen-long",
            "generation_status": "PENDING",
            "lease_expires_at": 10**12,
        }
        generate_questions.worker_handler(
            _worker_event([long_id], "gen-long"), _context("worker-req-long")
        )
        long_message = self._openai.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertNotIn(generate_questions._SHORT_SOURCE_HINT, long_message)

    def test_language_blocks_sizes_and_weak_focus(self):
        ids = self._prepare(1, text="Algorithms sort lists. " * 20, generation_id="gen-lang")
        self.world.docs["d0"]["topics"] = [{"he": "אלגוריתמים", "en": "Algorithms"}]
        self.world.Table = lambda _name: MagicMock(
            get_item=MagicMock(
                return_value={
                    "Item": {
                        "matrix": {"Algorithms": {"Hard": {"correct": 0, "total": 2}}}
                    }
                }
            )
        )
        generate_questions.worker_handler(
            _worker_event(ids, "gen-lang", language="he", focus=True), _context()
        )
        system_prompt = self._openai.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("in Hebrew", system_prompt)
        self.assertIn("WEAK-TOPIC PRIORITY", system_prompt)

        self._openai.chat.completions.create.reset_mock()
        self.world.sets[generate_questions._generation_set_id("gen-lang")]["generation_status"] = "PENDING"
        self.world.sets[generate_questions._generation_set_id("gen-lang")].pop("course_id", None)
        generate_questions.worker_handler(
            _worker_event(ids, "gen-lang", language="en", focus=False),
            _context("worker-en"),
        )
        system_prompt = self._openai.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("in English", system_prompt)
        self.assertNotIn("WEAK-TOPIC PRIORITY", system_prompt)

        for count in (5, 10, 15, 20):
            generation_id = "gen-size-%s" % count
            document_id = "size-%s" % count
            self.world.add_document(document_id)
            self.world.sets[generate_questions._generation_set_id(generation_id)] = {
                "set_id": generate_questions._generation_set_id(generation_id),
                "generation_id": generation_id,
                "generation_status": "PENDING",
                "lease_expires_at": 10**12,
            }
            self._openai.chat.completions.create.return_value = _Completion(
                _questions_payload(count, "size")
            )
            result = generate_questions.worker_handler(
                _worker_event([document_id], generation_id, count=count),
                _context("worker-%s" % count),
            )
            self.assertTrue(result["ok"], count)
            row = self.world.sets[generate_questions._generation_set_id(generation_id)]
            self.assertEqual(row["question_count"], count)
            self.assertEqual(row["generation_status"], "READY")
            self.assertEqual(
                len([item for item in self.world.questions.values() if item["set_id"] == row["set_id"]]),
                count,
            )

    def test_content_selection_log_omits_document_text(self):
        marker = "SECRET_SOURCE_SENTENCE_9f3a"
        ids = self._prepare(1, text=marker + " and surrounding prose.", generation_id="gen-log")
        with self.assertLogs(generate_questions.logger, level="INFO") as captured:
            generate_questions.worker_handler(_worker_event(ids, "gen-log"), _context())
        joined = "\n".join(captured.output)
        self.assertIn("content_selection_complete", joined)
        self.assertIn("openai_latency_ms", joined)
        self.assertIn("generation_outcome", joined)
        self.assertNotIn(marker, joined)
        self.assertNotIn("test-key", joined)


if __name__ == "__main__":
    unittest.main()
