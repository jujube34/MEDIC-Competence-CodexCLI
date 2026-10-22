"""Connection admission must never create a file named after a DSN or secret."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from medic.apps.api.__main__ import main
from medic.runtime.database_location import classify
from medic.runtime.store import Store


class DatabaseLocationContracts(unittest.TestCase):
    def test_keyword_connections_and_unsupported_urls_rejected_before_io(self):
        values=['host=localhost dbname=test password=synthetic-secret',
                'user = test password = synthetic-secret', 'service=local',
                'target_session_attrs=read-write host=localhost',
                'mysql://test:synthetic-secret@localhost/db','sqlite:///state.db',
                'postgresql:host=localhost','POSTGRESQL://localhost/db','C:relative.db']
        with patch('medic.runtime.store.Path') as paths:
            for value in values:
                with self.subTest(format=value.split(':')[0].split('=')[0]):
                    with self.assertRaisesRegex(ValueError,'unsupported_database_connection_format') as error:
                        Store(value)
                    self.assertNotIn('synthetic-secret',str(error.exception))
            paths.assert_not_called()

    def test_blank_and_non_path_inputs_are_rejected(self):
        with patch('medic.runtime.store.Path') as paths:
            for value in ('',' ',' local.sqlite3','local.sqlite3 ','bad\x00path',None,42,b'bytes.db',':memory:'):
                with self.subTest(type=type(value).__name__):
                    with self.assertRaisesRegex(ValueError,'invalid_database_location'): Store(value)
            paths.assert_not_called()

    def test_supported_files_with_spaces_unicode_and_equals(self):
        for value in ('C:/local data/员工.sqlite3',r'C:\local data\state.db',r'\\server\share\state.db',
                      '/tmp/host=files/state.sqlite3','./name=value.sqlite3',Path('state.sqlite3')):
            self.assertEqual(classify(value),('sqlite',os.fspath(value)))
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'employee data'/'员工.sqlite3'
            store=Store(path)
            self.assertFalse(store.postgres); self.assertTrue(path.is_file())

    def test_postgres_connection_error_cannot_fall_back_to_a_file(self):
        for value in ('postgresql://test:synthetic-secret@127.0.0.1:1/db','postgres://localhost/db'):
            with patch('medic.runtime.postgres.initialize',side_effect=ConnectionError('not connected')) as connect, patch('medic.runtime.store.Path') as paths:
                with self.assertRaises(ConnectionError): Store(value)
                connect.assert_called_once_with(value); paths.assert_not_called()

    def test_invalid_environment_value_cannot_leave_files_or_console_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(__file__).resolve().parents[2]
            script='from medic.apps.api.__main__ import main; raise SystemExit(main())'
            env={**os.environ,'PYTHONPATH':str(root),'MEDIC_BAD_DB':'user=test password=synthetic-secret dbname=test'}
            result=subprocess.run([sys.executable,'-c',script,'--database-env','MEDIC_BAD_DB',
                                   '--vault',str(Path(folder)/'vault')],cwd=folder,env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,2)
            self.assertIn('unsupported_database_connection_format',result.stdout)
            self.assertNotIn('synthetic-secret',result.stdout+result.stderr)
            self.assertEqual(list(Path(folder).iterdir()),[])


class ApiDatabaseEntry(unittest.TestCase):
    def test_environment_passed_to_factory_without_command_argument(self):
        value='postgresql://test:synthetic-secret@localhost/db'
        with patch.dict(os.environ,{'MEDIC_DB_TEST':value}),patch('medic.apps.api.__main__.create_app') as create,patch('medic.apps.api.__main__.uvicorn.run') as run:
            self.assertEqual(main(['--database-env','MEDIC_DB_TEST','--vault','vault','--port','8766']),0)
            create.assert_called_once_with(value,Path('vault'),upload_policy=None)
            self.assertEqual(run.call_args.kwargs['host'],'127.0.0.1')

    def test_explicit_file_path_remains_supported(self):
        with patch('medic.apps.api.__main__.create_app') as create,patch('medic.apps.api.__main__.uvicorn.run'):
            self.assertEqual(main(['--database','state.sqlite3','--vault','vault']),0)
            create.assert_called_once_with('state.sqlite3',Path('vault'),upload_policy=None)

    def test_missing_empty_and_ambiguous_configuration_fail(self):
        with patch.dict(os.environ,{'MEDIC_EMPTY_DB':''}),patch('medic.apps.api.__main__.create_app') as create,redirect_stderr(io.StringIO()):
            for args in (['--vault','vault'],['--database-env','MEDIC_EMPTY_DB','--vault','vault'],
                         ['--database','state.db','--database-env','MEDIC_EMPTY_DB','--vault','vault']):
                with self.assertRaises(SystemExit) as error: main(args)
                self.assertEqual(error.exception.code,2)
            create.assert_not_called()

    def test_startup_errors_do_not_echo_credentials_or_provider_details(self):
        output=io.StringIO()
        with patch('medic.apps.api.__main__.create_app',side_effect=RuntimeError('synthetic-secret at private endpoint')),patch('medic.apps.api.__main__.uvicorn.run') as run,redirect_stdout(output):
            self.assertEqual(main(['--database','state.db','--vault','vault']),2)
            run.assert_not_called()
        self.assertIn('RuntimeError',output.getvalue())
        self.assertNotIn('synthetic-secret',output.getvalue())
        self.assertNotIn('private endpoint',output.getvalue())
