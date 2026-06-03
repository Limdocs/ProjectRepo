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

from generate_questions import (
    _build_system_prompt,
    _parse_api_request,
    _question_set_generation_metadata,
    _resolve_weak_topic_focus,
)
from openai_helpers import build_canonical_topic_lookup


def _api_event(body, sub="user-123"):
    return {
        "requestContext": {"authorizer": {"claims": {"sub": sub}}},
        "pathParameters": {"courseId": "course-1"},
        "body": json.dumps(body),
    }


class ParseFocusWeakTopicsTests(unittest.TestCase):
    def test_missing_flag_defaults_false(self):
        parsed, err = _parse_api_request(_api_event({"documentIds": ["d1"]}))
        self.assertIsNone(err)
        self.assertFalse(parsed["focus_weak_topics"])

    def test_true_flag(self):
        parsed, err = _parse_api_request(
            _api_event(
                {
                    "documentIds": ["d1"],
                    "requested_question_count": 5,
                    "quiz_language": "he",
                    "focus_weak_topics": True,
                }
            )
        )
        self.assertIsNone(err)
        self.assertTrue(parsed["focus_weak_topics"])

    def test_invalid_type_returns_400(self):
        _, err = _parse_api_request(
            _api_event({"documentIds": ["d1"], "focus_weak_topics": "yes"})
        )
        self.assertIsNotNone(err)
        self.assertEqual(err["statusCode"], 400)


class WeakFocusResolverTests(unittest.TestCase):
    @patch("generate_questions._dynamodb")
    def test_applied_only_with_canonical_overlap(self, mock_dynamodb):
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.get_item.return_value = {
            "Item": {
                "matrix": {
                    "Algorithms": {"Hard": {"correct": 0, "total": 2}},
                    "Other": {"Hard": {"correct": 0, "total": 2}},
                }
            }
        }
        lookup = build_canonical_topic_lookup(["Algorithms"])
        result = _resolve_weak_topic_focus(
            "user-1", "course-1", lookup, "cid-test"
        )
        self.assertTrue(result["progress_found"])
        self.assertTrue(result["applied_focus_weak_topics"])
        self.assertEqual(result["prioritized_weak_topics"], ["Algorithms"])
        self.assertEqual(result["weak_count_before_intersection"], 2)
        self.assertEqual(result["weak_count_after_intersection"], 1)

    @patch("generate_questions._dynamodb")
    def test_no_overlap_not_applied(self, mock_dynamodb):
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_table.get_item.return_value = {
            "Item": {
                "matrix": {
                    "Unrelated": {"Hard": {"correct": 0, "total": 2}},
                }
            }
        }
        lookup = build_canonical_topic_lookup(["Algorithms"])
        result = _resolve_weak_topic_focus(
            "user-1", "course-1", lookup, "cid-test"
        )
        self.assertTrue(result["progress_found"])
        self.assertFalse(result["applied_focus_weak_topics"])
        self.assertEqual(result["prioritized_weak_topics"], [])


class SystemPromptWeakFocusTests(unittest.TestCase):
    def test_standard_prompt_without_weak_block(self):
        prompt = _build_system_prompt(["Algorithms"], 5, "he")
        self.assertNotIn("WEAK-TOPIC PRIORITY", prompt)
        self.assertNotIn("60", prompt)

    def test_weak_block_when_topics_provided(self):
        prompt = _build_system_prompt(
            ["Algorithms", "Data Structures"],
            10,
            "en",
            prioritized_weak_topics=["Algorithms"],
        )
        self.assertIn("WEAK-TOPIC PRIORITY", prompt)
        self.assertIn("60", prompt)
        self.assertIn("70", prompt)
        self.assertIn("Algorithms", prompt)


class QuestionSetMetadataTests(unittest.TestCase):
    def test_normal_when_not_applied(self):
        meta = _question_set_generation_metadata(False, [])
        self.assertEqual(meta, {"generation_mode": "NORMAL"})
        self.assertNotIn("focused_topics", meta)

    def test_weakness_focused_with_topics(self):
        meta = _question_set_generation_metadata(
            True, ["Algorithms", "Graphs"]
        )
        self.assertEqual(meta["generation_mode"], "WEAKNESS_FOCUSED")
        self.assertEqual(meta["focused_topics"], ["Algorithms", "Graphs"])


if __name__ == "__main__":
    unittest.main()
