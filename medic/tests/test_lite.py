"""Lite-profile behavior: no OCR/subprocess isolation, no quota locks, no preview."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from medic.apps.api.service import Service
from medic.apps.api.uploads import LiteUploadAdmission
from medic.ingestion.bounded import parse_file
from medic.tests.test_ingestion import word


class LiteParsing(unittest.TestCase):
    def test_parse_file_skips_worker_process_and_ocr(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'evidence.docx'
            path.write_bytes(word('Exact text kept in lite'))
            with patch.dict(os.environ, {'MEDIC_LITE': '1'}), \
                    patch('medic.ingestion.bounded.subprocess.Popen') as popen:
                extraction = parse_file(path)
            popen.assert_not_called()
            self.assertEqual(extraction.state, 'ready')
            self.assertIn('Exact text kept in lite', '\n'.join(b.quote for b in extraction.blocks))


class LiteUploads(unittest.TestCase):
    def test_permit_creates_and_cleans_one_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            admission = LiteUploadAdmission(type('V', (), {'root': Path(folder)})())
            permit = admission.acquire('tenant', 'user')
            path = permit.temporary_path('evidence.docx')
            path.write_bytes(b'PK')
            permit.received(2)
            self.assertEqual(permit.process(lambda p: p.read_bytes(), path), b'PK')
            self.assertFalse(path.exists())


class LitePreview(unittest.TestCase):
    def test_material_summary_disables_preview(self):
        snapshot = {'snapshot_id': 'snapshot-1', 'original_name': 'a.docx', 'period': 'p',
                    'source_sha256': 'a'*64, 'format': 'docx', 'created_at': 'now',
                    'extraction': {'state': 'ready', 'parser_version': 'v', 'units': [], 'blocks': [], 'issues': []}}
        with patch.dict(os.environ, {'MEDIC_LITE': '1'}):
            summary = Service.material_summary(snapshot)
        self.assertFalse(summary['preview']['supported'])
        self.assertFalse(summary['preview']['derived'])


if __name__ == '__main__':
    unittest.main()
