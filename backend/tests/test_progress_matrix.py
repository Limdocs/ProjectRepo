import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from progress_matrix import (
    MatrixDriftError,
    build_matrix_deltas_from_questions,
    deserialize_deltas,
    merge_deltas,
    serialize_deltas,
    _subtract_deltas_from_matrix,
)
from topic_scoring import compute_topic_scores


def _q(question_id, *, topics=None, difficulty="Easy", correct_index=0):
    return {
        "question_id": question_id,
        "topics": topics if topics is not None else ["Algorithms"],
        "difficulty": difficulty,
        "correct_index": correct_index,
    }


class ProgressMatrixDeltaBuildTests(unittest.TestCase):
    def test_easy_correct(self):
        questions = [_q("q1", difficulty="Easy", correct_index=1)]
        deltas = build_matrix_deltas_from_questions(questions, {"q1": 1})
        self.assertEqual(deltas["Algorithms"]["Easy"]["correct"], 1)
        self.assertEqual(deltas["Algorithms"]["Easy"]["total"], 1)

    def test_hard_wrong(self):
        questions = [_q("q1", difficulty="Hard", correct_index=0)]
        deltas = build_matrix_deltas_from_questions(questions, {"q1": 1})
        self.assertEqual(deltas["Algorithms"]["Hard"]["correct"], 0)
        self.assertEqual(deltas["Algorithms"]["Hard"]["total"], 1)

    def test_mixed_easy_correct_hard_wrong(self):
        questions = [
            _q("q1", difficulty="Easy", correct_index=0),
            _q("q2", difficulty="Hard", correct_index=0),
        ]
        deltas = build_matrix_deltas_from_questions(questions, {"q1": 0, "q2": 1})
        self.assertEqual(deltas["Algorithms"]["Easy"]["correct"], 1)
        self.assertEqual(deltas["Algorithms"]["Hard"]["correct"], 0)

    def test_multi_topic_duplicates_topic_once_per_question(self):
        questions = [
            {
                "question_id": "q1",
                "topics": ["Topic A", "Topic B"],
                "difficulty": "Medium",
                "correct_index": 0,
            }
        ]
        deltas = build_matrix_deltas_from_questions(questions, {"q1": 0})
        self.assertEqual(deltas["Topic A"]["Medium"]["total"], 1)
        self.assertEqual(deltas["Topic B"]["Medium"]["total"], 1)

    def test_uncategorized_when_topics_missing(self):
        questions = [{"question_id": "q1", "difficulty": "Easy", "correct_index": 0}]
        deltas = build_matrix_deltas_from_questions(questions, {"q1": 0})
        self.assertIn("Uncategorized", deltas)

    def test_medium_fallback_for_unknown_difficulty(self):
        questions = [_q("q1", difficulty="weird", correct_index=0)]
        deltas = build_matrix_deltas_from_questions(questions, {"q1": 0})
        self.assertIn("Medium", deltas["Algorithms"])


class ProgressMatrixApplyTests(unittest.TestCase):
    def test_strict_subtract_raises_on_underflow(self):
        matrix = {"Algorithms": {"Easy": {"correct": 1, "total": 1}}}
        deltas = {"Algorithms": {"Easy": {"correct": 2, "total": 2}}}
        with self.assertRaises(MatrixDriftError):
            _subtract_deltas_from_matrix(matrix, deltas)

    def test_general_topic_key_preserved_in_serialized_deltas(self):
        questions = [_q("q1", topics=["General"], difficulty="Easy", correct_index=0)]
        deltas = build_matrix_deltas_from_questions(questions, {"q1": 0})
        stored = serialize_deltas(deltas)
        self.assertIn("General", stored)
        restored = deserialize_deltas(stored)
        self.assertEqual(restored["General"]["Easy"]["total"], 1)

    def test_serialize_deserialize_round_trip(self):
        questions = [
            _q("q1", difficulty="Easy", correct_index=0),
            _q("q2", difficulty="Hard", correct_index=0),
        ]
        deltas = build_matrix_deltas_from_questions(questions, {"q1": 0, "q2": 1})
        raw = serialize_deltas(deltas)
        raw["Algorithms"]["Easy"]["correct"] = Decimal(1)
        restored = deserialize_deltas(raw)
        self.assertEqual(restored["Algorithms"]["Easy"]["correct"], 1)
        self.assertEqual(restored["Algorithms"]["Hard"]["total"], 1)


class ProgressMatrixDeleteGuardsTests(unittest.TestCase):
    @staticmethod
    def _should_decrement_progress(attempt_item):
        return bool(
            attempt_item.get("progress_applied_at")
            and not attempt_item.get("matrix_reverted_at")
        )

    def test_skip_decrement_without_progress_applied_at(self):
        attempt = {"matrix_deltas": {"T": {"Easy": {"correct": 1, "total": 1}}}}
        self.assertFalse(self._should_decrement_progress(attempt))

    def test_decrement_when_progress_applied_and_not_reverted(self):
        attempt = {
            "progress_applied_at": "2026-01-01T00:00:00+00:00",
            "matrix_deltas": {"T": {"Easy": {"correct": 1, "total": 1}}},
        }
        self.assertTrue(self._should_decrement_progress(attempt))

    def test_delete_uses_stored_deltas_without_questions(self):
        stored = {"Algorithms": {"Easy": {"correct": 1, "total": 2}}}
        restored = deserialize_deltas(stored)
        self.assertEqual(restored, stored)


class ProgressMatrixRebuildTests(unittest.TestCase):
    def test_merge_deltas_sums_multiple_attempts(self):
        first = {"Topic": {"Easy": {"correct": 1, "total": 2}}}
        second = {"Topic": {"Easy": {"correct": 0, "total": 1}, "Hard": {"correct": 1, "total": 1}}}
        merged = merge_deltas(first, second)
        self.assertEqual(merged["Topic"]["Easy"]["correct"], 1)
        self.assertEqual(merged["Topic"]["Easy"]["total"], 3)
        self.assertEqual(merged["Topic"]["Hard"]["correct"], 1)

    def test_compute_topic_scores_after_simulated_delete(self):
        matrix = {
            "Algorithms": {
                "Easy": {"correct": 2, "total": 2},
                "Hard": {"correct": 0, "total": 1},
            }
        }
        deltas = {"Algorithms": {"Easy": {"correct": 1, "total": 1}}}
        updated = _subtract_deltas_from_matrix(matrix, deltas)
        topics = compute_topic_scores(updated)
        self.assertEqual(topics[0]["total_answered"], 2)
        self.assertEqual(topics[0]["score"], 25)


class ProgressMatrixAttemptAnswersAlignmentTests(unittest.TestCase):
    def test_graded_row_count_matches_questions_with_id(self):
        questions = [
            _q("q1"),
            _q("q2"),
            {"difficulty": "Easy", "correct_index": 0},
        ]
        answers = {"q1": 0, "q2": 1}
        deltas = build_matrix_deltas_from_questions(questions, answers)
        serialized = serialize_deltas(deltas)
        question_ids = [q["question_id"] for q in questions if q.get("question_id")]
        total_cells = sum(
            bucket["total"]
            for by_diff in serialized.values()
            for bucket in by_diff.values()
        )
        self.assertEqual(len(question_ids), 2)
        self.assertEqual(total_cells, 2)


if __name__ == "__main__":
    unittest.main()
