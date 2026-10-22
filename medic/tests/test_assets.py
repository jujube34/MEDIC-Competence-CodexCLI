import unittest
from types import SimpleNamespace
from unittest.mock import patch

from medic.p0.assets import model, matrix


def cells(values, row=15):
    return [SimpleNamespace(value=value, data_type="s", row=row) for value in values]


class AssetAudit(unittest.TestCase):
    def test_spacer_column_does_not_shift_execution_anchors(self):
        row = cells(["Synthetic dimension", "M1", "Name", "Definition", "A", "B", "C", "D", None,
                     "execA", "execB", "execC", "execD"])
        sheet = SimpleNamespace(iter_rows=lambda: [row])
        book = {"五维模型定义": sheet}
        class Workbook(dict):
            def close(self):
                pass
        with patch("medic.p0.assets.openpyxl.load_workbook", return_value=Workbook(book)):
            items, issues = model("synthetic.xlsx")
        self.assertEqual(issues, [])
        self.assertEqual(items["M1"]["execution_A"], "execA")
        self.assertEqual(items["M1"]["execution_D"], "execD")

    def test_duplicate_and_missing_anchor_are_reported(self):
        row = cells(["Synthetic", "M1", "Name", "Definition", "A", "B", "C", "D", None, "A", "B", "C", None])
        class Workbook(dict):
            def close(self):
                pass
        book = Workbook({"五维模型定义": SimpleNamespace(iter_rows=lambda: [row, row])})
        with patch("medic.p0.assets.openpyxl.load_workbook", return_value=book):
            _, issues = model("synthetic.xlsx")
        self.assertIn("duplicate:M1", issues)
        self.assertIn("missing_definition_or_anchor:M1", issues)

    def test_bad_grade_duplicate_and_missing_job_coverage(self):
        row = cells(["job", "family", "track", "title", "level", "M", "M1", "name", None, 0], row=2)
        sheet = SimpleNamespace(title="明细-test", iter_rows=lambda **kwargs: [row, row])
        class Workbook(list):
            def close(self):
                pass
        with patch("medic.p0.assets.openpyxl.load_workbook", return_value=Workbook([sheet])):
            _, issues = matrix("synthetic.xlsx")
        self.assertIn("invalid_recommendation:2", issues)
        self.assertIn("duplicate_row:2", issues)
        self.assertIn("incomplete_job_coverage", issues)


if __name__ == "__main__":
    unittest.main()
