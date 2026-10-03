import json
import os
import sys
import threading
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


class _Completion(object):
    def __init__(self, content):
        message = MagicMock()
        message.content = content
        choice = MagicMock()
        choice.message = message
        self.choices = [choice]


class _Context(object):
    def __init__(self, request_id):
        self.aws_request_id = request_id


class ConcurrencyTests(unittest.TestCase):
    def _run_pair(self, reverse):
        world = QuizWorld()
        world.bind(generate_questions)
        world.pair_commit = True
        world.reverse_pair = reverse
        world.courses["course-1"] = {"owner_id": "user-1", "course_id": "course-1"}
        generation_id = "gen-shared"
        world.add_document("d1")
        set_id = generate_questions._generation_set_id(generation_id)
        # The lease has already been taken by "req-a"; "req-b" is a duplicate
        # worker that reaches the commit anyway.
        world.sets[set_id] = {
            "set_id": set_id,
            "generation_id": generation_id,
            "generation_status": "GENERATING",
            "worker_request_id": "req-a",
            "lease_expires_at": 10**12,
        }
        markers = ["ALPHA", "BETA"]
        marker_lock = threading.Lock()

        client = MagicMock()

        def five(marker):
            return json.dumps(
                {
                    "questions": [
                        {
                            "question": "%s-%s" % (marker, index),
                            "options": ["a", "b", "c", "d"],
                            "correct_index": 0,
                            "explanation": marker,
                            "topics": ["General"],
                            "difficulty": "Easy",
                            "answer": "a",
                        }
                        for index in range(5)
                    ]
                }
            )

        markers[:] = ["ALPHA", "BETA"]

        def create_five(**_kwargs):
            with marker_lock:
                marker = markers.pop(0)
            return _Completion(five(marker))

        client.chat.completions.create.side_effect = create_five
        results = []

        def run(request_id):
            with patch("generate_questions.get_openai_client", return_value=client):
                try:
                    results.append(
                        generate_questions._generate_questions_worker(
                            "course-1",
                            ["d1"],
                            request_id,
                            5,
                            "en",
                            requested_by="user-1",
                            generation_id=generation_id,
                            worker_request_id=request_id,
                            commit_state={"committed": False},
                        )
                    )
                except generate_questions.GenerationError as exc:
                    results.append({"ok": False, "code": exc.code})

        threads = [
            threading.Thread(target=run, args=("req-a",)),
            threading.Thread(target=run, args=("req-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(
            sorted(bool(result.get("duplicate")) for result in results), [False, True]
        )
        texts = [item["question"] for item in world.questions.values()]
        self.assertEqual(len(texts), 5)
        prefixes = {text.split("-")[0] for text in texts}
        self.assertEqual(len(prefixes), 1)
        self.assertEqual(world.sets[set_id]["question_count"], 5)
        self.assertEqual(world.sets[set_id]["generation_status"], "READY")
        loser = "BETA" if "ALPHA" in prefixes else "ALPHA"
        self.assertFalse(any(loser in text for text in texts))

    def test_interleaved_workers_commit_one_coherent_set(self):
        self._run_pair(reverse=False)

    def test_reversed_apply_order_is_still_coherent(self):
        self._run_pair(reverse=True)

    def test_duplicate_after_commit_skips_openai(self):
        world = QuizWorld()
        world.bind(generate_questions)
        world.courses["course-1"] = {"owner_id": "user-1", "course_id": "course-1"}
        generation_id = "gen-dup"
        world.add_document("d1")
        set_id = generate_questions._generation_set_id(generation_id)
        world.sets[set_id] = {
            "set_id": set_id,
            "generation_id": generation_id,
            "generation_status": "READY",
            "course_id": "course-1",
            "created_at": "2026-01-01T00:00:00+00:00",
            "lease_expires_at": 10**12,
        }
        client = MagicMock()
        with patch("generate_questions.get_openai_client", return_value=client):
            result = generate_questions.worker_handler(
                {
                    "courseId": "course-1",
                    "documentIds": ["d1"],
                    "generationId": generation_id,
                    "requestedBy": "user-1",
                    "requestedQuestionCount": 5,
                    "quizLanguage": "en",
                },
                _Context("req-dup"),
            )
        self.assertTrue(result.get("duplicate"))
        client.chat.completions.create.assert_not_called()
        self.assertFalse(any(call[0] == "transact" for call in world.calls))
        self.assertEqual(world.docs["d1"]["processing_status"], "READY")


class DuplicateRequestTests(unittest.TestCase):
    """Duplicate requests are refused by the generation lease, not by document status."""

    def setUp(self):
        os.environ.setdefault("WORKER_FUNCTION_NAME", "worker")
        self.world = QuizWorld()
        self.world.bind(generate_questions)
        self.world.courses["course-1"] = {"owner_id": "user-1", "course_id": "course-1"}
        self.world.add_document("d1")
        owner = patch("generate_questions.require_course_owner", return_value=None)
        owner.start()
        self.addCleanup(owner.stop)

    def _post(self, request_id):
        return generate_questions.api_handler(
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
            _Context(request_id),
        )

    def test_second_request_is_refused_while_the_first_holds_the_lease(self):
        first = self._post("api-1")
        self.assertEqual(first["statusCode"], 202)
        second = self._post("api-2")
        self.assertEqual(second["statusCode"], 409)
        self.assertEqual(self.world.docs["d1"]["processing_status"], "READY")
        active = [
            item
            for item in self.world.sets.values()
            if item.get("generation_status") in ("PENDING", "GENERATING")
        ]
        self.assertEqual(len(active), 1)
        self.assertEqual(
            active[0]["generation_id"],
            json.loads(first["body"])["generation_id"],
        )

    def test_losing_a_slot_race_aborts_the_losing_generation(self):
        first_generation_id = "gen-winner"
        slot_id = generate_questions._generation_slot_id("course-1", ["d1"])
        set_id = generate_questions._generation_set_id(first_generation_id)
        self.world.sets[set_id] = {
            "set_id": set_id,
            "generation_id": first_generation_id,
            "generation_status": "PENDING",
            "lease_expires_at": 10**12,
        }

        original_update = self.world.update_item

        def steal_slot_before_claim(**kwargs):
            if kwargs["Key"].get("set_id") == slot_id and ":docs" in kwargs[
                "ExpressionAttributeValues"
            ]:
                self.world.update_item = original_update
                self.world.sets.setdefault(slot_id, {"set_id": slot_id})[
                    "active_generation_id"
                ] = first_generation_id
            return original_update(**kwargs)

        self.world.update_item = steal_slot_before_claim
        response = self._post("api-late")
        self.assertEqual(response["statusCode"], 409)
        aborted = [
            item
            for item in self.world.sets.values()
            if item.get("generation_status") == "ABORTED"
        ]
        self.assertEqual(len(aborted), 1)
        self.assertEqual(aborted[0]["failure_code"], "GENERATION_SUPERSEDED")
        self.assertEqual(self.world.docs["d1"]["processing_status"], "READY")


if __name__ == "__main__":
    unittest.main()
