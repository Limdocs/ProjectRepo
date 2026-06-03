import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from topic_scoring import compute_topic_scores


class TopicScoringTests(unittest.TestCase):
    def test_one_easy_correct_scores_100(self):
        matrix = {"Algorithms": {"Easy": {"correct": 1, "total": 1}}}
        topics = compute_topic_scores(matrix)
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["score"], 100)
        self.assertEqual(topics[0]["status"], "strong")

    def test_one_hard_wrong_scores_0(self):
        matrix = {"Algorithms": {"Hard": {"correct": 0, "total": 1}}}
        topics = compute_topic_scores(matrix)
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["score"], 0)
        self.assertEqual(topics[0]["status"], "weak")

    def test_easy_correct_hard_wrong_scores_25(self):
        matrix = {
            "Mixed": {
                "Easy": {"correct": 1, "total": 1},
                "Hard": {"correct": 0, "total": 1},
            }
        }
        topics = compute_topic_scores(matrix)
        self.assertEqual(topics[0]["score"], 25)

    def test_medium_hard_correct_easy_wrong_scores_83(self):
        matrix = {
            "Mixed": {
                "Easy": {"correct": 0, "total": 1},
                "Medium": {"correct": 1, "total": 1},
                "Hard": {"correct": 1, "total": 1},
            }
        }
        topics = compute_topic_scores(matrix)
        self.assertEqual(topics[0]["score"], 83)
        self.assertEqual(topics[0]["status"], "strong")

    def test_empty_matrix_returns_empty_list(self):
        self.assertEqual(compute_topic_scores({}), [])
        self.assertEqual(compute_topic_scores(None), [])

    def test_zero_totals_are_omitted(self):
        matrix = {"Unused": {"Easy": {"correct": 0, "total": 0}}}
        self.assertEqual(compute_topic_scores(matrix), [])

    def test_stray_difficulty_uses_medium_weight(self):
        matrix = {"Topic": {"Tricky": {"correct": 1, "total": 1}}}
        topics = compute_topic_scores(matrix)
        self.assertEqual(topics[0]["score"], 100)
        self.assertEqual(topics[0]["weighted_total"], 2)

    def test_general_merged_with_uncategorized(self):
        matrix = {
            "General": {"Easy": {"correct": 1, "total": 2}},
            "Uncategorized": {"Medium": {"correct": 1, "total": 1}},
        }
        topics = compute_topic_scores(matrix)
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["topic"], "Uncategorized")
        self.assertEqual(topics[0]["total_answered"], 3)
        self.assertEqual(topics[0]["difficulty_breakdown"]["easy"]["total"], 2)
        self.assertEqual(topics[0]["difficulty_breakdown"]["medium"]["total"], 1)

    def test_status_thresholds(self):
        weak = compute_topic_scores({"T": {"Easy": {"correct": 59, "total": 100}}})[0]
        medium = compute_topic_scores({"T": {"Easy": {"correct": 60, "total": 100}}})[0]
        strong = compute_topic_scores({"T": {"Easy": {"correct": 80, "total": 100}}})[0]
        self.assertEqual(weak["status"], "weak")
        self.assertEqual(medium["status"], "medium")
        self.assertEqual(strong["status"], "strong")

    def test_sort_weakest_first(self):
        matrix = {
            "Strong Topic": {"Easy": {"correct": 10, "total": 10}},
            "Weak Topic": {"Hard": {"correct": 0, "total": 2}},
        }
        topics = compute_topic_scores(matrix)
        self.assertEqual([topic["topic"] for topic in topics], ["Weak Topic", "Strong Topic"])

    def test_difficulty_breakdown_scores(self):
        matrix = {"Topic": {"Easy": {"correct": 1, "total": 2}}}
        breakdown = compute_topic_scores(matrix)[0]["difficulty_breakdown"]
        self.assertEqual(breakdown["easy"]["score"], 50)
        self.assertIsNone(breakdown["medium"]["score"])
        self.assertIsNone(breakdown["hard"]["score"])


if __name__ == "__main__":
    unittest.main()
