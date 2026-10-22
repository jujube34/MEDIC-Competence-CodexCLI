"""Negative controls for the soak auditor, using reduced real observations."""
import copy
import json
from pathlib import Path
import unittest

from medic.evals.runtime_load import isolated_postgres_uri, validate_config, verify_observations


class LoadAudit(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((Path(__file__).resolve().parents[1]/
            'evals/fixtures/transaction_load_observation.json').read_text(encoding='utf-8'))

    def reject(self, name):
        self.assertFalse(verify_observations(self.data)[name])

    def test_reduced_real_process_observations_pass(self):
        self.assertTrue(all(verify_observations(self.data).values()))

    def test_missing_run_does_not_pass_vacuously(self):
        self.data['cases'].clear()
        self.reject('configured_arrivals_completed')
        self.reject('all_runs_cancelled_after_three_attempts')

    def test_duplicate_lease_and_epoch_gap_are_detected(self):
        worker = self.data['workers'][0]
        event = next(e for e in worker['events'] if e['kind']=='leased')
        worker['events'].append(copy.deepcopy(event))
        self.reject('exactly_three_unique_leases_per_run')
        worker['events'].pop()
        event['epoch'] = 99
        self.reject('exactly_three_unique_leases_per_run')

    def test_hard_kill_cannot_be_inferred_from_checkpoint(self):
        self.data['fault']['handle_signaled'] = False
        self.reject('actual_process_death_confirmed')
        self.data['fault']['live_unsettled_lease'] = False
        self.reject('fault_happened_during_load_with_live_lease')

    def test_lost_or_falsely_settled_crash_reservation_is_detected(self):
        case = self.data['cases'][self.data['fault']['run']]
        case['budget']['unsettled_requests'] = 0
        self.reject('budget_preserved_including_killed_reservation')
        row = next(r for r in case['budget']['ledger'] if r['request_id']==self.data['fault']['request'])
        row['charged'] = 0
        self.reject('fault_reservation_matches_claim_and_ledger')

    def test_lease_expiry_cannot_be_replaced_by_early_requeue(self):
        case = next(iter(self.data['cases'].values()))
        case['history'][0]['requeued_at'] = case['history'][0]['lease_until']-1
        self.reject('no_early_recovery')

    def test_reset_deadline_and_budget_are_detected(self):
        case = next(iter(self.data['cases'].values()))
        case['final_identity']['deadline'] += 1
        case['final_identity']['limits']['total_tokens'] += 1
        self.reject('immutable_input_build_limits_and_deadline')

    def test_report_or_fake_worker_identity_is_rejected(self):
        case = next(iter(self.data['cases'].values()))
        case['report'] = {'status':'pass'}
        self.reject('no_results_reports_or_execution_identity')
        case['report'] = None
        case['execution_mode'] = 'live'
        self.reject('no_results_reports_or_execution_identity')

    def test_missing_late_callback_and_failed_worker_are_not_success(self):
        worker = self.data['workers'][-1]
        worker['events'] = [e for e in worker['events'] if e['kind']!='fenced']
        self.reject('all_callbacks_including_killed_worker_fenced')
        worker['exit_code'] = 1
        self.reject('no_unexpected_worker_or_operation_failures')

    def test_duplicate_outbox_and_stale_failure_damage_are_detected(self):
        case = next(iter(self.data['cases'].values()))
        case['outbox'].append(copy.deepcopy(case['outbox'][0]))
        case['events'].append({'kind':'task_failed','data':{'error_code':'load_stale_callback'}})
        self.reject('idempotent_single_run_and_outbox')
        self.reject('no_stale_callback_damage')

    def test_changed_harness_is_not_verified_as_original(self):
        self.data['harness_after_sha256'] = '0'*64
        self.reject('harness_unchanged_during_probe')

    def test_invalid_schedule_rejected_before_execution(self):
        for duration, interval, lease, workers in [(float('nan'),2,3,4),(300,float('inf'),3,4),
                (300,0,3,4),(300,2,0,4),(1,2,3,4),(300,2,3,5),(300,2,3,True)]:
            with self.subTest(values=(duration,interval,lease,workers)), self.assertRaises(ValueError):
                validate_config(duration,interval,lease,workers)

    def test_libpq_receives_schema_option_with_actual_space(self):
        from psycopg.conninfo import conninfo_to_dict
        uri = isolated_postgres_uri('postgresql://synthetic@127.0.0.1:55439/test?connect_timeout=3',
                                    'load_probe_test')
        decoded = conninfo_to_dict(uri)
        self.assertEqual(decoded['options'], '-c search_path=load_probe_test')
        self.assertEqual(decoded['connect_timeout'], '3')
        with self.assertRaisesRegex(ValueError, 'postgres_options_not_supported'):
            isolated_postgres_uri(uri,'another_schema')


if __name__=='__main__':
    unittest.main()
