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

import generate_questions  # noqa: E402
from quiz_fakes import QuizWorld  # noqa: E402


def _payload():
    return json.dumps(
        {
            "questions": [
                {
                    "question": "Q%s" % index,
                    "options": ["a", "b", "c", "d"],
                    "correct_index": 0,
                    "explanation": "because",
                    "topics": ["General"],
                    "difficulty": "Easy",
                    "answer": "a",
                }
                for index in range(5)
            ]
        }
    )


class _Context(object):
    aws_request_id = "worker-post"


class PostCommitTests(unittest.TestCase):
    def setUp(self):
        self.world = QuizWorld()
        self.world.bind(generate_questions)
        self.world.courses["course-1"] = {"owner_id": "user-1", "course_id": "course-1"}
        self.generation_id = "gen-post"
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
            "requestedBy": "user-1",
            "requestedQuestionCount": 5,
            "quizLanguage": "en",
        }
        client = MagicMock()
        message = MagicMock()
        message.content = _payload()
        choice = MagicMock()
        choice.message = message
        client.chat.completions.create.return_value = MagicMock(choices=[choice])
        patcher = patch("generate_questions.get_openai_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_post_commit_failure_does_not_fail_generation(self):
        real_info = generate_questions.logger.info

        def info(msg, *args, **kwargs):
            real_info(msg, *args, **kwargs)
            if isinstance(msg, str) and "generation_outcome" in msg:
                raise RuntimeError("post commit work failed")

        with patch.object(
            generate_questions.logger, "info", side_effect=info
        ), self.assertLogs(generate_questions.logger, level="ERROR") as captured:
            result = generate_questions.worker_handler(self.event, _Context())
        self.assertTrue(result["ok"])
        self.assertEqual(self.world.sets[self.set_id]["generation_status"], "READY")
        self.assertEqual(self.world.docs["d1"]["processing_status"], "READY")
        self.assertIn("post_commit_error_ignored", "\n".join(captured.output))

    def test_exception_after_commit_cannot_regress_even_if_flag_is_false(self):
        def blow_up(*_args, **kwargs):
            self.world.sets[self.set_id]["generation_status"] = "READY"
            kwargs["commit_state"]["committed"] = False
            raise RuntimeError("after commit")

        with patch("generate_questions._generate_questions_worker", side_effect=blow_up):
            result = generate_questions.worker_handler(self.event, _Context())
        self.assertFalse(result["ok"])
        self.assertEqual(self.world.sets[self.set_id]["generation_status"], "READY")
        self.assertEqual(self.world.docs["d1"]["processing_status"], "READY")

        result = generate_questions.worker_handler(self.event, _Context())
        self.assertTrue(result.get("duplicate"))
        self.assertEqual(self.world.sets[self.set_id]["generation_status"], "READY")

    def test_commit_succeeds_without_touching_the_source_document(self):
        result = generate_questions.worker_handler(self.event, _Context())
        self.assertTrue(result["ok"])
        self.assertEqual(self.world.sets[self.set_id]["generation_status"], "READY")
        self.assertEqual(len(self.world.questions), 5)
        self.assertEqual(self.world.docs["d1"]["processing_status"], "READY")
        self.assertNotIn("has_generated_quiz", self.world.docs["d1"])
        self.assertNotIn("generation_id", self.world.docs["d1"])


if __name__ == "__main__":
    unittest.main()
