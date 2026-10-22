import copy
import json
import unittest

from medic.evals.verify_run import build_provenance
from medic.runtime import build


class BuildAudit(unittest.TestCase):
    def test_audit_rejects_missing_or_mixed_worker_identity(self):
        identity = build.verify(build.VERSION)
        snapshot = {'execution_build': identity, 'source': {'versions': {'runtime_version': build.VERSION}},
                    'report': {'execution_build': identity},
                    'tasks': [{'context_json': json.dumps({'execution_build': identity})}],
                    'events': [{'kind': 'run_created', 'data': {'execution_build': identity}}]}
        protocols = [{'execution_build': copy.deepcopy(identity)}]
        self.assertTrue(build_provenance(snapshot, protocols))
        protocols[0]['execution_build']['environment']['python'] = 'mismatched-version'
        self.assertFalse(build_provenance(snapshot, protocols))
        self.assertFalse(build_provenance(snapshot, [{}]))
        self.assertFalse(build_provenance(snapshot, []))
        self.assertFalse(build_provenance({**snapshot, 'execution_build': None}, protocols))


if __name__ == '__main__':
    unittest.main()
