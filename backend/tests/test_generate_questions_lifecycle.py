import json
import os
import sys
import unittest
from unittest.mock import patch

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
from content_selection import InsufficientPassageBudgetError  # noqa: E402
from quiz_fakes import QuizWorld  # noqa: E402


def _api_event(body):
    return {
        "requestContext": {"authorizer": {"claims": {"sub": "user-123"}}},
        "pathParameters": {"courseId": "course-1"},
        "body": json.dumps(body),
    }


class _Context(object):
    aws_request_id = "worker-req"


class LifecycleTests(unittest.TestCase):
    """A quiz-generation failure is recorded on the generation, never on a document."""

    def setUp(self):
        self.world = QuizWorld()
        self.world.bind(generate_questions)
        self.world.courses["course-1"] = {"owner_id": "user-1", "course_id": "course-1"}
        self.generation_id = "gen-life"
        self.world.add_document("d1")
        self.set_id = generate_questions._generation_set_id(self.generation_id)
        self.world.sets[self.set_id] = {
            "set_id": self.set_id,
            "generation_id": self.generation_id,
            "generation_status": "PENDING",
            "lease_expires_at": 10**12,
        }
        self.event = {
            "courseId": "course-1",
            "documentIds": ["d1"],
            "generationId": self.generation_id,
            "apiRequestId": "cid",
            "requestedBy": "user-1",
            "requestedQuestionCount": 5,
            "quizLanguage": "en",
        }

    def test_selector_openai_and_validation_failures(self):
        cases = [
            (
                InsufficientPassageBudgetError(4, 10, 10, 4),
                "CONTENT_INSUFFICIENT_PASSAGE_BUDGET",
            ),
            (RuntimeError("sensitive backend detail"), "INTERNAL_ERROR"),
            (ValueError("not json"), "LLM_INVALID_RESPONSE"),
        ]
        for exc, code in cases:
            self.world.sets[self.set_id]["generation_status"] = "PENDING"
            self.world.questions.clear()
            with patch(
                "generate_questions._generate_questions_worker",
                side_effect=exc,
            ):
                result = generate_questions.worker_handler(self.event, _Context())
            self.assertEqual(result["code"], code)
            self.assertEqual(self.world.questions, {})
            row = self.world.sets[self.set_id]
            self.assertEqual(row["generation_status"], "FAILED")
            self.assertEqual(row["failure_code"], code)
            self.assertNotIn("course_id", row)
            self.assertEqual(self.world.docs["d1"]["processing_status"], "READY")
            self.assertNotIn("failure_code", self.world.docs["d1"])

    def test_failure_code_is_not_exception_text(self):
        with patch(
            "generate_questions._generate_questions_worker",
            side_effect=RuntimeError("sensitive backend detail"),
        ):
            generate_questions.worker_handler(self.event, _Context())
        row = self.world.sets[self.set_id]
        self.assertEqual(row["failure_code"], "INTERNAL_ERROR")
        self.assertNotIn("sensitive", json.dumps(row))

    def test_failed_generation_can_be_retried_with_the_same_documents(self):
        with patch(
            "generate_questions._generate_questions_worker",
            side_effect=RuntimeError("boom"),
        ):
            generate_questions.worker_handler(self.event, _Context())
        self.assertEqual(self.world.sets[self.set_id]["generation_status"], "FAILED")

        with patch("generate_questions.require_course_owner", return_value=None):
            retry = generate_questions.api_handler(
                {
                    "httpMethod": "POST",
                    "pathParameters": {"courseId": "course-1"},
                    "requestContext": {"authorizer": {"claims": {"sub": "user-1"}}},
                    "body": json.dumps(
                        {
                            "documentIds": ["d1"],
                            "requested_question_count": 5,
                            "quiz_language": "en",
                        }
                    ),
                },
                type("_Ctx", (), {"aws_request_id": "api-retry"})(),
            )
        self.assertEqual(retry["statusCode"], 202)
        self.assertEqual(self.world.docs["d1"]["processing_status"], "READY")

    def test_parse_dedupes_document_ids(self):
        parsed, err = generate_questions._parse_api_request(
            _api_event({"documentIds": ["d1", " d1 ", "d2", "d1"]})
        )
        self.assertIsNone(err)
        self.assertEqual(parsed["document_ids"], ["d1", "d2"])


if __name__ == "__main__":
    unittest.main()
