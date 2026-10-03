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
import get_questions  # noqa: E402
from quiz_fakes import QuizWorld, client_error  # noqa: E402


def _payload(count, marker):
    questions = []
    for index in range(count):
        questions.append(
            {
                "question": "%s %s" % (marker, index),
                "options": ["a", "b", "c", "d"],
                "correct_index": 0,
                "explanation": "because",
                "topics": ["General"],
                "difficulty": "Medium",
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


class _Context(object):
    def __init__(self, request_id):
        self.aws_request_id = request_id


class CommitTests(unittest.TestCase):
    def setUp(self):
        self.world = QuizWorld()
        self.world.bind(generate_questions)
        self.world.courses["course-1"] = {"owner_id": "user-1", "course_id": "course-1"}
        self.client = MagicMock()
        self.client.chat.completions.create.return_value = _Completion(_payload(5, "q"))
        patcher = patch("generate_questions.get_openai_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _arm(self, generation_id, count=1):
        ids = []
        for index in range(count):
            document_id = "d%s" % index
            ids.append(document_id)
            self.world.add_document(document_id)
        set_id = generate_questions._generation_set_id(generation_id)
        self.world.sets[set_id] = {
            "set_id": set_id,
            "generation_id": generation_id,
            "generation_status": "PENDING",
            "lease_expires_at": 10**12,
            "generation_course_id": "course-1",
            "document_ids": ids,
        }
        return ids, set_id

    def test_commit_is_one_transaction_within_action_limit(self):
        ids, set_id = self._arm("gen-20")
        self.client.chat.completions.create.return_value = _Completion(_payload(20, "q"))
        result = generate_questions.worker_handler(
            {
                "courseId": "course-1",
                "documentIds": ids,
                "generationId": "gen-20",
                "requestedBy": "user-1",
                "requestedQuestionCount": 20,
                "quizLanguage": "en",
            },
            _Context("req-20"),
        )
        self.assertTrue(result["ok"])
        transacts = [call for call in self.world.calls if call[0] == "transact"]
        self.assertEqual(len(transacts), 1)
        self.assertEqual(transacts[0][1], 21)
        self.assertLessEqual(21, generate_questions._MAX_TRANSACT_ACTIONS)
        self.assertEqual(generate_questions._MAX_TRANSACT_ACTIONS, 100)
        puts = [action for action in self.world.last_transact if "Put" in action]
        update = [action for action in self.world.last_transact if "Update" in action][0]
        self.assertEqual(len(puts), 20)
        self.assertIn("generation_status", update["Update"]["ConditionExpression"])
        row = self.world.sets[set_id]
        self.assertEqual(row["generation_status"], "READY")
        self.assertIn("course_id", row)
        self.assertIn("created_at", row)
        self.assertEqual(len(self.world.questions), 20)
        floats = [
            value
            for item in self.world.questions.values()
            for value in item.values()
            if isinstance(value, float)
        ]
        self.assertEqual(floats, [])

    def test_transaction_failure_records_persistence_failed(self):
        ids, set_id = self._arm("gen-fail")
        self.world.transact_error = client_error("InternalServerError", "TransactWriteItems")
        result = generate_questions.worker_handler(
            {
                "courseId": "course-1",
                "documentIds": ids,
                "generationId": "gen-fail",
                "requestedBy": "user-1",
                "requestedQuestionCount": 5,
                "quizLanguage": "en",
            },
            _Context("req-fail"),
        )
        self.assertEqual(result["code"], "PERSISTENCE_FAILED")
        self.assertEqual(self.world.questions, {})
        self.assertEqual(self.world.sets[set_id]["generation_status"], "FAILED")
        self.assertEqual(self.world.sets[set_id]["failure_code"], "PERSISTENCE_FAILED")
        self.assertEqual(self.world.docs["d0"]["processing_status"], "READY")
        self.assertNotIn("failure_code", self.world.docs["d0"])

    def test_precommit_row_is_invisible_to_question_reads(self):
        hidden = {
            "set_id": "hidden",
            "generation_id": "gen-hidden",
            "generation_status": "PENDING",
            "generation_course_id": "course-1",
            "document_ids": ["d0"],
            "lease_expires_at": 10,
        }
        table = MagicMock()
        table.get_item.return_value = {"Item": hidden}
        table.query.return_value = {"Items": []}
        original = get_questions._question_sets_table
        get_questions._question_sets_table = table
        try:
            self.assertIsNone(get_questions._get_set_or_404("course-1", "hidden"))
            response = get_questions._list_sets("course-1")
        finally:
            get_questions._question_sets_table = original
        body = json.loads(response["body"])
        self.assertEqual(body["sets"], [])

        visible = dict(hidden)
        visible["course_id"] = "course-1"
        visible["created_at"] = "2026-01-01T00:00:00+00:00"
        visible["generation_status"] = "READY"
        table.get_item.return_value = {"Item": visible}
        table.query.return_value = {"Items": [visible]}
        get_questions._question_sets_table = table
        try:
            self.assertIsNotNone(get_questions._get_set_or_404("course-1", "hidden"))
            response = get_questions._list_sets("course-1")
        finally:
            get_questions._question_sets_table = original
        body = json.loads(response["body"])
        self.assertEqual(body["sets"][0]["generation_id"], "gen-hidden")

    def test_conditional_cancel_when_peer_is_ready_is_success(self):
        ids, set_id = self._arm("gen-race")

        def cancel(**_kwargs):
            self.world.sets[set_id]["generation_status"] = "READY"
            self.world.sets[set_id]["course_id"] = "course-1"
            self.world.sets[set_id]["created_at"] = "2026-01-01T00:00:00+00:00"
            raise client_error(
                "TransactionCanceledException",
                "TransactWriteItems",
                [{"Code": "None"}, {"Code": "ConditionalCheckFailed"}],
            )

        self.world.transact_write_items = cancel
        with self.assertLogs(generate_questions.logger, level="INFO") as captured:
            result = generate_questions.worker_handler(
                {
                    "courseId": "course-1",
                    "documentIds": ids,
                    "generationId": "gen-race",
                    "requestedBy": "user-1",
                    "requestedQuestionCount": 5,
                    "quizLanguage": "en",
                },
                _Context("req-race"),
            )
        self.assertTrue(result["ok"])
        self.assertIn("commit_lost_race", "\n".join(captured.output))
        self.assertEqual(self.world.questions, {})
        self.assertEqual(self.world.sets[set_id]["generation_status"], "READY")
        self.assertEqual(self.world.docs["d0"]["processing_status"], "READY")

    def test_create_record_omits_gsi_keys(self):
        item = generate_questions._create_generation_record(
            "gen-new", "course-1", ["d0"], "cid"
        )
        self.assertNotIn("course_id", item)
        self.assertNotIn("created_at", item)
        self.assertEqual(item["generation_status"], "PENDING")
        self.assertEqual(item["generation_course_id"], "course-1")


if __name__ == "__main__":
    unittest.main()
