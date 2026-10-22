from unittest.mock import patch
from medic.tests.test_postgres import database
from medic.tests import test_receipt_recovery
from medic.runtime.store import Store


class PostgreSQLReceiptRecovery(test_receipt_recovery.ReceiptRecoveryContracts):
    def setUp(self):
        dsn=database()
        with patch('medic.tests.test_runtime.Store',side_effect=lambda _:Store(dsn)):
            super().setUp()
