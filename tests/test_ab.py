import copy
import unittest

from bench.ab import assess, summarise


class AssessmentTest(unittest.TestCase):
    def setUp(self):
        self.rows = [{"task": task, "arm": arm, "i": i, "score": 100.0,
                      "wall_seconds": 60.0, "judge": 8.0 if task == "research" else None}
                     for task in ("bugfix2", "research") for arm in ("control", "treatment") for i in (1, 2)]

    def check(self):
        return assess(self.rows, ["bugfix2", "research"], 2)

    def test_complete_pairs_are_eligible_and_not_mutated(self):
        before = copy.deepcopy(self.rows)
        self.assertEqual(self.check(), {"status": "eligible", "failures": [], "missing": []})
        self.assertEqual(self.rows, before)

    def test_missing_score_cannot_disappear_from_average(self):
        self.rows[2]["score"] = None
        self.assertEqual(self.check()["status"], "incomplete")

    def test_missing_quality_blocks_adoption(self):
        self.rows[6]["judge"] = None
        self.assertEqual(self.check()["status"], "incomplete")

    def test_quality_regression_rejects_perfect_keyword_scores(self):
        self.rows[6]["judge"] = 6.0
        self.assertEqual(self.check()["status"], "reject")

    def test_score_regression_rejects_even_without_quality_grades(self):
        self.rows[2]["score"] = 90.0
        self.rows[6]["judge"] = None
        self.assertEqual(self.check()["status"], "reject")

    def test_task_regression_cannot_be_hidden_by_overall_gain(self):
        self.rows[0]["score"] = self.rows[1]["score"] = 80.0
        self.rows[6]["score"] = self.rows[7]["score"] = 90.0
        self.assertIn("research score regressed beyond the control spread", self.check()["failures"])

    def test_control_spread_allows_task_variation(self):
        self.rows[0]["score"] = 80.0
        self.rows[1]["score"] = 100.0
        self.rows[2]["score"] = self.rows[3]["score"] = 90.0
        self.assertEqual(self.check()["status"], "eligible")

    def test_time_limit(self):
        for row in self.rows:
            if row["arm"] == "treatment":
                row["wall_seconds"] = 72.0
        self.assertEqual(self.check()["status"], "eligible")
        self.rows[2]["wall_seconds"] = 73.0
        self.assertEqual(self.check()["status"], "reject")

    def test_invalid_numbers_are_incomplete(self):
        for field, value in (("score", float("nan")), ("score", 101), ("score", True),
                             ("wall_seconds", 0), ("wall_seconds", float("inf")), ("judge", 11)):
            with self.subTest(field=field, value=value):
                rows = copy.deepcopy(self.rows)
                rows[6][field] = value
                self.assertEqual(assess(rows, ["bugfix2", "research"], 2)["status"], "incomplete")

    def test_missing_and_duplicate_pairs(self):
        self.rows.pop()
        self.assertEqual(self.check()["status"], "incomplete")
        self.rows.append(copy.deepcopy(self.rows[0]))
        self.assertEqual(self.check()["status"], "incomplete")

    def test_requested_matrix_detects_whole_missing_task(self):
        self.assertEqual(assess(self.rows, ["bugfix2", "research", "data"], 2)["status"], "incomplete")
        self.assertEqual(assess([], [], 0)["status"], "incomplete")

    def test_partial_report_is_incomplete(self):
        report = summarise("test", "{}", self.rows[:4], ["bugfix2", "research"], 2)
        self.assertIn("**incomplete**", report)
        self.assertIn("Missing or invalid research quality grade", report)
