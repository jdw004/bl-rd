"""Focused checks for worker costs, integer timing, and scheduling invariants."""

import json
import unittest

from score import _duration_milliseconds, evaluate_run
from selector import allocate, duration_milliseconds, split


def tests_with_durations(seconds):
    return [{"number": number, "seconds": value} for number, value in enumerate(seconds)]


class AllocationTests(unittest.TestCase):
    def test_empty_selection(self):
        self.assertEqual(allocate([]), [])

    def test_single_long_test(self):
        self.assertEqual(allocate(tests_with_durations([600])), [[0]])

    def test_worker_overhead_tradeoffs(self):
        for durations, expected in [([1, 1], [[0, 1]]), ([10, 10], [[0], [1]]),
                                    ([2, 2], [[0, 1]])]:
            with self.subTest(durations=durations):
                self.assertEqual(allocate(tests_with_durations(durations)), expected)

    def test_deterministic_ties(self):
        tests = tests_with_durations([10, 10, 10, 10])
        self.assertEqual(split(tests, 2), [[0, 2], [1, 3]])
        self.assertEqual(allocate(tests), allocate(list(reversed(tests))))

    def test_duration_conversion_matches_scorer(self):
        for seconds in [0, 0.0004, 0.0005, 0.0015, 1.2345, 10.123, 600]:
            with self.subTest(seconds=seconds):
                self.assertEqual(duration_milliseconds(seconds), _duration_milliseconds(seconds))
        self.assertEqual(duration_milliseconds(0.0005), 1)

    def test_duplicate_free_allocation_and_cost(self):
        for durations in [[0] * 10, [10] * 16, [0, 0.0005, 2, 7, 1, 30, 2, 2, 5, 9]]:
            with self.subTest(durations=durations):
                tests = tests_with_durations(durations)
                workers = allocate(tests)
                selected = [number for worker in workers for number in worker]
                self.assertEqual(sorted(selected), list(range(len(tests))))
                self.assertLessEqual(len(workers), 8)
                self.assertTrue(all(workers))

                def cost(schedule):
                    output = json.dumps({"commit": "check", "workers": schedule}).encode()
                    result = evaluate_run(output, tests, {"check": {"failing": selected}})
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["missed_commits"], [])
                    return result["simulated_milliseconds"]

                self.assertEqual(cost(workers), min(cost(split(tests, count))
                                 for count in range(1, min(8, len(tests)) + 1)))
        self.assertEqual(len(allocate(tests_with_durations([10] * 16))), 8)


if __name__ == "__main__":
    unittest.main()
