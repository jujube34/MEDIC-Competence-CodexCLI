"""Opt-in real PostgreSQL contract suite; requires an explicit *_test database."""
import os
import unittest
from unittest.mock import patch

from medic.runtime.store import Store
from medic.tests import test_runtime, test_aggregation, test_dispatcher, test_budget, test_mailbox, test_rework, test_finalization, test_build


def database():
    dsn = os.environ.get("MEDIC_TEST_POSTGRES_DSN")
    if not dsn:
        raise unittest.SkipTest("MEDIC_TEST_POSTGRES_DSN is not configured")
    from psycopg.conninfo import conninfo_to_dict
    if not conninfo_to_dict(dsn).get("dbname", "").endswith("_test"):
        raise ValueError("PostgreSQL integration tests require an explicit *_test database")
    return dsn


class PostgreSQLRuntime(test_runtime.RuntimeContracts):
    def setUp(self):
        dsn = database()
        with patch("medic.tests.test_runtime.Store", side_effect=lambda _: Store(dsn)):
            super().setUp()


class PostgreSQLAggregation(test_aggregation.AggregationContracts):
    def setUp(self):
        dsn = database()
        with patch("medic.tests.test_aggregation.Store", side_effect=lambda _: Store(dsn)):
            super().setUp()


class PostgreSQLDispatcher(test_dispatcher.DurableDispatcher):
    def setUp(self):
        dsn = database()
        with patch("medic.tests.test_dispatcher.Store", side_effect=lambda _: Store(dsn)):
            super().setUp()
        self.path = self.store.path


class PostgreSQLBudget(test_budget.PersistentBudget):
    def setUp(self):
        dsn = database()
        with patch("medic.tests.test_budget.Store", side_effect=lambda _: Store(dsn)):
            super().setUp()
        self.path = self.store.path


class PostgreSQLMailbox(test_mailbox.MailboxContracts):
    def setUp(self):
        dsn = database()
        with patch('medic.tests.test_mailbox.Store', side_effect=lambda _: Store(dsn)):
            super().setUp()


class PostgreSQLRework(test_rework.ReworkContracts):
    def setUp(self):
        dsn = database()
        with patch('medic.tests.test_mailbox.Store', side_effect=lambda _: Store(dsn)):
            super().setUp()


class PostgreSQLTargetedRework(test_rework.TargetedReworkContracts):
    def setUp(self):
        dsn = database()
        with patch('medic.tests.test_aggregation.Store', side_effect=lambda _: Store(dsn)):
            super().setUp()


class PostgreSQLFinalization(test_finalization.FinalizationContracts):
    def setUp(self):
        dsn = database()
        with patch('medic.tests.test_runtime.Store', side_effect=lambda _: Store(dsn)):
            super().setUp()


class PostgreSQLBuild(test_build.BuildBindings):
    def setUp(self):
        dsn = database()
        with patch('medic.tests.test_build.Store', side_effect=lambda _: Store(dsn)):
            super().setUp()
