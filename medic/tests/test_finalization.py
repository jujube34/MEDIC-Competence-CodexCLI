import unittest

from medic.runtime.store import Denied
from medic.tests import test_runtime
from medic.tools.service import ToolService
from medic.domain.validation import InvalidResult


class FinalizationContracts(unittest.TestCase):
    setUp = test_runtime.RuntimeContracts.setUp
    tearDown = test_runtime.RuntimeContracts.tearDown

    def test_bounded_continuation_does_not_change_lease_or_accept_old_turn(self):
        binding = self.coordinator
        self.store.bind_execution(binding, 'thread', 'turn-1')
        with self.assertRaisesRegex(Denied, 'not_reserved'):
            self.store.bind_finalization(binding, 'thread', 'turn-1', 'turn-2')
        for old, new in [('turn-1', 'turn-2'), ('turn-2', 'turn-3')]:
            self.store.request_finalization(binding, 'thread', old)
            with self.assertRaisesRegex(Denied, 'already_pending'):
                self.store.request_finalization(binding, 'thread', old)
            self.store.bind_finalization(binding, 'thread', old, new)
        with self.assertRaisesRegex(Denied, 'repair_budget_exhausted'):
            self.store.request_finalization(binding, 'thread', 'turn-3')
        with self.assertRaisesRegex(Denied, 'identity_mismatch'):
            self.store.complete(binding, 'fake-receipt', 'thread', 'turn-1')
        task = self.store.snapshot(self.run)['tasks'][0]
        self.assertEqual(task['lease_epoch'], binding['lease_epoch'])
        self.assertEqual(task['turn_id'], 'turn-3')

    def test_result_rejections_share_the_finalization_repair_budget(self):
        binding = self.coordinator
        self.store.bind_execution(binding, 'thread', 'turn-1')
        with self.store.connect(self.run) as db:
            for _ in range(2):
                self.store.event(db, self.run, binding['task_id'], 'result_rejected', {'lease_epoch': binding['lease_epoch']})
        with self.assertRaisesRegex(Denied, 'repair_budget_exhausted'):
            self.store.request_finalization(binding, 'thread', 'turn-1')

    def test_tool_submissions_also_account_for_prior_finalization_repairs(self):
        binding = self.coordinator
        self.store.bind_execution(binding, 'thread', 'turn-1')
        self.store.request_finalization(binding, 'thread', 'turn-1')
        self.store.bind_finalization(binding, 'thread', 'turn-1', 'turn-2')
        service = ToolService(self.store, binding)
        for _ in range(2):
            with self.assertRaises(InvalidResult):
                service.call('runtime_finish_plan', {})
        with self.assertRaisesRegex(Denied, 'repair_budget_exhausted'):
            service.call('runtime_finish_plan', {})
