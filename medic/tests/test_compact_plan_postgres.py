from unittest.mock import patch
from medic.tests.test_postgres import database
from medic.tests import test_compact_plan
from medic.runtime.store import Store


class PostgreSQLCompactPlan(test_compact_plan.CompactPlan):
    def setUp(self):
        dsn=database()
        with patch('medic.tests.test_compact_plan.Store',side_effect=lambda _:Store(dsn)):
            super().setUp()
