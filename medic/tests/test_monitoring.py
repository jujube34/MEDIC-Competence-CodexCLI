"""Authorized incremental execution logs, using synthetic runtime events."""
import json
import unittest

from medic.tests import test_api


class MonitoringContracts(unittest.TestCase):
    setUp = test_api.ApplicationContracts.setUp
    database = test_api.ApplicationContracts.database
    auth = staticmethod(test_api.ApplicationContracts.auth)
    create = test_api.ApplicationContracts.create
    upload = test_api.ApplicationContracts.upload

    def test_scope_authentication_and_incremental_paging(self):
        assessment = self.create()
        url = '/assessments/' + assessment['assessment_id'] + '/execution'
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get(url, headers=self.auth(self.other)).status_code, 404)
        first = self.client.get(url, params={'limit': 1}, headers=self.auth(self.employee))
        self.assertEqual(first.status_code, 200, first.text)
        result = first.json()
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['expected_tasks'], 6)
        next_page = self.client.get(url, params={'after': result['next_cursor']}, headers=self.auth(self.manager)).json()
        self.assertTrue(all(item['seq'] > result['next_cursor'] for item in next_page['events']))
        for params in ({'after': -1}, {'limit': 201}):
            self.assertEqual(self.client.get(url, params=params, headers=self.auth(self.employee)).status_code, 422)

    def test_raw_payload_credentials_and_quotes_never_released(self):
        assessment = self.create()
        with self.service.runtime.connect() as db:
            self.service.runtime.event(db, assessment['run_id'], None, 'tool_invoked', {
                'tool': 'evidence_read_span', 'lease_epoch': 1,
                'arguments': {'token': 'SECRET_TEST_KEY'}, 'quote': 'PRIVATE_EVIDENCE',
                'reasoning': 'HIDDEN_REASONING', 'error': '/private/path'})
        response = self.client.get('/assessments/' + assessment['assessment_id'] + '/execution', headers=self.auth(self.employee))
        for secret in ('SECRET_TEST_KEY', 'PRIVATE_EVIDENCE', 'HIDDEN_REASONING', '/private/path', 'token_hash', 'context_json'):
            self.assertNotIn(secret, response.text)
        self.assertEqual(response.json()['events'][-1]['tool'], 'evidence_read_span')

    def test_revoked_scope_cannot_read_logs(self):
        assessment = self.create()
        with self.service.db.transaction(self.tenant) as db:
            db.execute('DELETE FROM app_grants WHERE tenant=? AND user_id=?', (self.tenant, 'manager'))
        response = self.client.get('/assessments/' + assessment['assessment_id'] + '/execution', headers=self.auth(self.manager))
        self.assertEqual(response.status_code, 404)


class PostgreSQLMonitoring(MonitoringContracts):
    def database(self):
        from medic.tests.test_postgres import database
        return database()


if __name__ == '__main__':
    unittest.main()
