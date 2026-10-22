import unittest

from medic.adapters.codex.worker import parse_receipt
from medic.p0.provider import ProbeError


class ReceiptTests(unittest.TestCase):
    def test_model_fenced_receipt(self):
        self.assertEqual(parse_receipt('```json\n{"receipt":"submitted-abc"}\n```'), 'submitted-abc')

    def test_model_receipt_in_separate_final_paragraph(self):
        self.assertEqual(parse_receipt('All required roles were dispatched and the plan was submitted.\n\n{"receipt":"submitted-abc"}'), 'submitted-abc')

    def test_receipt_is_one_object_not_arbitrary_embedded_json(self):
        for value in ('', 'Now submitting the GoalReview.', 'prefix {"receipt":"x"}', '[{"receipt":"x"}]',
                      '{"receipt":"x","grade":"D"}', '{"receipt":null}',
                      '```json\n{"receipt":"x"}\n```\nextra', '{"receipt":"x"}{"receipt":"y"}'):
            with self.subTest(value=value), self.assertRaises((ValueError, ProbeError)):
                parse_receipt(value)
