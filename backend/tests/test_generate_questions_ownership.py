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
from quiz_fakes import QuizWorld, client_error  # noqa: E402


def _api_event(document_ids):
    return {
        "httpMethod": "POST",
        "pathParameters": {"courseId": "course-1"},
        "requestContext": {"authorizer": {"claims": {"sub": "user-1"}}},
        "body": json.dumps(
            {
                "documentIds": document_ids,
                "requested_question_count": 5,
                "quiz_language": "en",
            }
        ),
    }


class _Context(object):
    def __init__(self, request_id="api-req"):
        self.aws_request_id = request_id


class _WorkerContext(object):
    def __init__(self, request_id="worker-req"):
        self.aws_request_id = request_id


def _worker_event(document_ids, generation_id):
    return {
        "courseId": "course-1",
        "documentIds": document_ids,
        "generationId": generation_id,
        "requestedBy": "user-1",
        "requestedQuestionCount": 5,
        "quizLanguage": "en",
    }


class OwnershipTests(unittest.TestCase):
    """Generation concurrency is owned by the generation record and its lease."""

    def setUp(self):
        self.world = QuizWorld()
        self.world.bind(generate_questions)
        self.world.courses["course-1"] = {"owner_id": "user-1", "course_id": "course-1"}
        self.owner = patch("generate_questions.require_course_owner", return_value=None)
        self.owner.start()
        self.addCleanup(self.owner.stop)

    def _seed_generation(self, generation_id, status, lease, document_ids=("d1",)):
        for document_id in document_ids:
            if document_id not in self.world.docs:
                self.world.add_document(document_id)
        set_id = generate_questions._generation_set_id(generation_id)
        self.world.sets[set_id] = {
            "set_id": set_id,
            "generation_id": generation_id,
            "generation_status": status,
            "lease_expires_at": lease,
            "generation_course_id": "course-1",
            "document_ids": list(document_ids),
        }
        slot_id = generate_questions._generation_slot_id("course-1", list(document_ids))
        self.world.sets[slot_id] = {
            "set_id": slot_id,
            "active_generation_id": generation_id,
            "generation_course_id": "course-1",
            "document_ids": list(document_ids),
        }
        return set_id

    def test_revoked_worker_exits_before_openai(self):
        self._seed_generation("gen-old", "REVOKED", 10**12)
        client = MagicMock()
        with patch("generate_questions.get_openai_client", return_value=client), patch(
            "generate_questions.select_quiz_content_streaming"
        ) as selector:
            result = generate_questions.worker_handler(
                _worker_event(["d1"], "gen-old"), _WorkerContext()
            )
        self.assertEqual(result["code"], "OWNERSHIP_LOST")
        client.chat.completions.create.assert_not_called()
        selector.assert_not_called()
        self.assertFalse(any(call[0] == "transact" for call in self.world.calls))
        self.assertEqual(self.world.docs["d1"]["processing_status"], "READY")

    def test_revoked_commit_writes_nothing(self):
        set_id = self._seed_generation("gen-commit", "PENDING", 10**12)
        client = MagicMock()
        message = MagicMock()
        message.content = json.dumps(
            {
                "questions": [
                    {
                        "question": "Q",
                        "options": ["a", "b", "c", "d"],
                        "correct_index": 0,
                        "explanation": "e",
                        "topics": ["General"],
                        "difficulty": "Easy",
                        "answer": "a",
                    }
                ]
                * 5
            }
        )
        choice = MagicMock()
        choice.message = message
        completion = MagicMock()
        completion.choices = [choice]
        client.chat.completions.create.return_value = completion

        def revoke_at_commit(**_kwargs):
            self.world.sets[set_id]["generation_status"] = "REVOKED"
            raise client_error(
                "TransactionCanceledException",
                "TransactWriteItems",
                [{"Code": "ConditionalCheckFailed"}],
            )

        self.world.transact_write_items = revoke_at_commit
        with patch("generate_questions.get_openai_client", return_value=client):
            result = generate_questions.worker_handler(
                _worker_event(["d1"], "gen-commit"), _WorkerContext()
            )
        self.assertEqual(result["code"], "OWNERSHIP_LOST")
        self.assertEqual(self.world.questions, {})
        self.assertEqual(self.world.docs["d1"]["processing_status"], "READY")
        self.assertNotIn("failure_code", self.world.docs["d1"])

    def test_expired_lease_is_revoked_before_a_new_generation_starts(self):
        self._seed_generation("gen-stale", "GENERATING", 10)
        with patch("generate_questions._now_epoch", return_value=1000):
            response = generate_questions.api_handler(_api_event(["d1"]), _Context())
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(
            self.world.sets[generate_questions._generation_set_id("gen-stale")][
                "generation_status"
            ],
            "REVOKED",
        )
        slot = self.world.sets[generate_questions._generation_slot_id("course-1", ["d1"])]
        self.assertNotEqual(slot["active_generation_id"], "gen-stale")
        self.assertEqual(self.world.docs["d1"]["processing_status"], "READY")

    def test_live_lease_returns_409_and_revokes_nothing(self):
        self._seed_generation("gen-live", "GENERATING", 5000)
        with patch("generate_questions._now_epoch", return_value=1000):
            response = generate_questions.api_handler(_api_event(["d1"]), _Context())
        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 409)
        self.assertIn("in progress", body["message"])
        self.assertFalse(
            any(":revoked" in call[3] for call in self.world.calls if call[0] == "update")
        )
        self.assertEqual(
            self.world.sets[generate_questions._generation_slot_id("course-1", ["d1"])][
                "active_generation_id"
            ],
            "gen-live",
        )

    def test_a_different_selection_is_not_blocked_by_a_live_generation(self):
        self._seed_generation("gen-live", "GENERATING", 5000)
        self.world.add_document("d2")
        with patch("generate_questions._now_epoch", return_value=1000):
            response = generate_questions.api_handler(_api_event(["d2"]), _Context())
        self.assertEqual(response["statusCode"], 202)

    def test_revoke_client_error_does_not_start_a_generation(self):
        self._seed_generation("gen-lock", "GENERATING", 10)
        self.world.revoke_error = client_error("AccessDeniedException")
        with patch("generate_questions._now_epoch", return_value=1000):
            response = generate_questions.api_handler(_api_event(["d1"]), _Context())
        self.assertEqual(response["statusCode"], 500)
        self.assertEqual(json.loads(response["body"])["code"], "GENERATION_LOCK_UNAVAILABLE")
        self.assertEqual(
            self.world.sets[generate_questions._generation_slot_id("course-1", ["d1"])][
                "active_generation_id"
            ],
            "gen-lock",
        )
        self.assertEqual(self.world.docs["d1"]["processing_status"], "READY")

    def test_worker_refreshes_lease_and_blocks_a_new_request(self):
        set_id = self._seed_generation("gen-lease", "PENDING", 1300)
        with patch("generate_questions._now_epoch", return_value=1290):
            begin = generate_questions._begin_generation(
                "gen-lease", "worker-req", "cid"
            )
        self.assertTrue(begin["ok"])
        self.assertEqual(self.world.sets[set_id]["lease_expires_at"], 1290 + 330)
        with patch("generate_questions._now_epoch", return_value=1291):
            response = generate_questions.api_handler(_api_event(["d1"]), _Context())
        self.assertEqual(response["statusCode"], 409)

    def test_pending_past_dispatch_lease_can_be_reclaimed(self):
        self._seed_generation("gen-queued", "PENDING", 100)
        with patch("generate_questions._now_epoch", return_value=1000):
            response = generate_questions.api_handler(_api_event(["d1"]), _Context())
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(
            self.world.sets[generate_questions._generation_set_id("gen-queued")][
                "generation_status"
            ],
            "REVOKED",
        )

    def test_legacy_generating_document_is_a_valid_source(self):
        self.world.add_document("d1", status="GENERATING", generation_id="gen-ancient")
        response = generate_questions.api_handler(_api_event(["d1"]), _Context())
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(self.world.docs["d1"]["processing_status"], "GENERATING")


if __name__ == "__main__":
    unittest.main()
