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
from quiz_fakes import QuizWorld  # noqa: E402


def _event(document_ids):
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
    def __init__(self, request_id):
        self.aws_request_id = request_id


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.world = QuizWorld()
        self.world.bind(generate_questions)
        self.world.courses["course-1"] = {"owner_id": "user-1", "course_id": "course-1"}
        patcher = patch("generate_questions.require_course_owner", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_invoke_failure_aborts_the_generation_and_leaves_documents_ready(self):
        self.world.add_document("d1")
        self.world.add_document("d2")
        self.world.invoke_error = RuntimeError("invoke down")
        response = generate_questions.api_handler(_event(["d1", "d2"]), _Context("api-1"))
        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 500)
        self.assertEqual(body["code"], "DISPATCH_FAILED")
        self.assertEqual(self.world.docs["d1"]["processing_status"], "READY")
        self.assertEqual(self.world.docs["d2"]["processing_status"], "READY")
        aborted = [
            item
            for item in self.world.sets.values()
            if item.get("generation_status") == "ABORTED"
        ]
        self.assertEqual(len(aborted), 1)
        self.assertEqual(aborted[0]["failure_code"], "DISPATCH_FAILED")

        self.world.invoke_error = None
        retry = generate_questions.api_handler(_event(["d1", "d2"]), _Context("api-2"))
        self.assertEqual(retry["statusCode"], 202)

    def test_unprocessed_document_is_rejected_before_any_generation_row(self):
        self.world.add_document("d1")
        self.world.docs["d1"].pop("s3_processed_key")
        self.world.docs["d1"]["processing_status"] = "FAILED"
        self.world.docs["d1"]["failure_reason"] = "Unsupported file extension '.docx'"
        response = generate_questions.api_handler(_event(["d1"]), _Context("api-3"))
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(json.loads(response["body"])["code"], "DOCUMENT_NOT_PROCESSED")
        self.assertEqual(self.world.sets, {})

    def test_legacy_quiz_failed_document_is_still_a_valid_source(self):
        self.world.add_document("d1", status="FAILED")
        self.world.docs["d1"]["failure_code"] = "LLM_REQUEST_FAILED"
        self.world.docs["d1"]["failure_reason"] = "LLM_REQUEST_FAILED"
        response = generate_questions.api_handler(_event(["d1"]), _Context("api-4"))
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(self.world.docs["d1"]["processing_status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
