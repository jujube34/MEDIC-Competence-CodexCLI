"""Same selection contracts on an explicit *_test PostgreSQL database."""
import unittest
from unittest.mock import patch

from medic.runtime.store import Store
from medic.tests import test_evidence_selection_boundaries as contracts
from medic.tests.test_postgres import database


class PostgreSQLSelectionBoundaries(contracts.SelectionBoundaries):
    def setUp(self):
        dsn = database()
        with patch('medic.tests.test_evidence_selection.Store', side_effect=lambda _: Store(dsn)):
            super().setUp()


class PostgreSQLSelectionMultiMaterial(contracts.SelectionMultiMaterial):
    def setUp(self):
        dsn = database()
        with patch('medic.tests.test_aggregation.Store', side_effect=lambda _: Store(dsn)):
            super().setUp()


class PostgreSQLSelectionTargetedRework(contracts.SelectionTargetedRework):
    def setUp(self):
        dsn = database()
        with patch('medic.tests.test_aggregation.Store', side_effect=lambda _: Store(dsn)):
            super().setUp()


class PostgreSQLSelectionPriorDisputes(contracts.SelectionPriorDisputes):
    def setUp(self):
        dsn = database()
        with patch('medic.tests.test_mailbox.Store', side_effect=lambda _: Store(dsn)):
            super().setUp()


if __name__ == '__main__':
    unittest.main()
