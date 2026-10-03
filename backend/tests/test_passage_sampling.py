import os
import random
import sys
import unicodedata
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from passage_sampling import coalesce, plan_passages, snap_range  # noqa: E402


def _passage_count(allowance, target=1200, minimum=400, maximum=12):
    rounded = int(round(allowance / float(target)))
    count = max(1, min(rounded, maximum))
    return min(count, max(1, allowance // minimum))


def _prose(length):
    unit = "Alpha beta gamma. Delta epsilon.\n"
    return (unit * ((length // len(unit)) + 1))[:length]


class PlanPassagesTests(unittest.TestCase):
    def test_allowance_covering_the_text_is_a_single_full_range(self):
        ranges = plan_passages(
            text_length=50,
            allowance=50,
            rng=random.Random(1),
            target_passage_chars=1200,
            min_passage_chars=400,
            max_passages=12,
        )
        self.assertEqual(ranges, ((0, 50),))

    def test_sizes_sum_to_allowance_and_stay_inside_the_text(self):
        length = 10_000
        allowance = 2_000
        ranges = plan_passages(
            text_length=length,
            allowance=allowance,
            rng=random.Random(4),
            target_passage_chars=1200,
            min_passage_chars=400,
            max_passages=12,
        )
        self.assertEqual(sum(end - start for start, end in ranges), allowance)
        previous = -1
        for start, end in ranges:
            self.assertGreaterEqual(start, 0)
            self.assertGreater(end, start)
            self.assertLessEqual(end, length)
            self.assertGreater(start, previous)
            previous = start

    def test_middle_and_end_are_structurally_eligible(self):
        length = 10_000
        allowance = 2_000
        expected = _passage_count(allowance)
        self.assertGreaterEqual(expected, 2)
        for seed in (0, 1, 2, 3, 7, 99):
            ranges = plan_passages(
                text_length=length,
                allowance=allowance,
                rng=random.Random(seed),
                target_passage_chars=1200,
                min_passage_chars=400,
                max_passages=12,
            )
            self.assertEqual(len(ranges), expected)
            self.assertGreaterEqual(ranges[-1][0], (expected - 1) * length // expected)
            self.assertLess(ranges[0][0], length // expected)

    def test_same_rng_seed_is_deterministic(self):
        kwargs = dict(
            text_length=8_000,
            allowance=3_000,
            target_passage_chars=1200,
            min_passage_chars=400,
            max_passages=12,
        )
        first = plan_passages(rng=random.Random(11), **kwargs)
        second = plan_passages(rng=random.Random(11), **kwargs)
        self.assertEqual(first, second)

    def test_passage_count_does_not_starve_a_passage(self):
        # A small target would ask for 5 passages, but the minimum size allows 2.
        allowance = 1_000
        count = _passage_count(allowance, target=200, minimum=400, maximum=12)
        self.assertEqual(count, 2)
        ranges = plan_passages(
            text_length=5_000,
            allowance=allowance,
            rng=random.Random(0),
            target_passage_chars=200,
            min_passage_chars=400,
            max_passages=12,
        )
        self.assertEqual(len(ranges), 2)
        self.assertTrue(all(end - start >= 400 for start, end in ranges))


class SnapAndCoalesceTests(unittest.TestCase):
    def test_snap_prefers_newline_and_only_shrinks(self):
        text = "abc\n" + ("word " * 80)
        raw_start, raw_end = 1, len(text)
        start, end = snap_range(text, raw_start, raw_end, min_passage_chars=20)
        self.assertGreater(start, raw_start)
        self.assertLessEqual(end, raw_end)
        self.assertLess(end - start, raw_end - raw_start)
        self.assertEqual(text[start - 1], "\n")
        self.assertEqual(text[start], "w")

    def test_snap_reverts_when_the_only_cut_is_too_aggressive(self):
        text = "abcdefghijklmnopqrstuvwxyz"
        raw_start, raw_end = 0, 20
        snapped = snap_range(text, raw_start, raw_end, min_passage_chars=400)
        self.assertEqual(snapped, (raw_start, raw_end))

    def test_combining_marks_are_not_left_at_the_start(self):
        base = "ב"
        mark = "\u05bc"  # dagesh
        self.assertNotEqual(unicodedata.combining(mark), 0)
        text = ("abcde " * 20) + base + mark + (" hello world.\n" * 30)
        mark_at = text.index(mark)
        start, end = snap_range(text, mark_at, len(text), min_passage_chars=20)
        self.assertGreater(end, start)
        self.assertEqual(unicodedata.combining(text[start]), 0)
        self.assertGreaterEqual(start, mark_at)

    def test_snap_never_grows_over_random_ranges(self):
        text = _prose(4_000)
        rng = random.Random(0)
        for _ in range(40):
            start = rng.randrange(0, 3_000)
            end = start + rng.randrange(400, 900)
            new_start, new_end = snap_range(text, start, end, min_passage_chars=400)
            self.assertGreaterEqual(new_start, start)
            self.assertLessEqual(new_end, end)
            self.assertGreater(new_end, new_start)

    def test_coalesce_merges_overlap_and_drops_duplicate_coverage(self):
        merged = coalesce([(0, 10), (8, 12), (12, 15), (20, 22)])
        self.assertEqual(merged, ((0, 15), (20, 22)))
        covered = sum(end - start for start, end in merged)
        self.assertLess(covered, 10 + 4 + 3 + 2)
