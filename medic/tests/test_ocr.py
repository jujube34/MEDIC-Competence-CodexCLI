"""Synthetic OCR contracts; no employee documents or model calls."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf
from medic.ingestion import ocr
from medic.ingestion.bounded import parse_file
from medic.ingestion.contracts import ParseError
from medic.ingestion.parser import extract
from medic.ingestion.preview import render_pdf
from medic.ingestion.vault import Vault
from medic.p0.common import digest


def scanned_pdf(*, rotation=0, crop=False, pages=1, mixed=False, blank=False, huge=False):
    with pymupdf.open() as source, pymupdf.open() as result:
        p = source.new_page(width=595, height=842)
        if not blank:
            p.insert_text((100,180), 'Synthetic scanned evidence 2026', fontsize=18)
            p.insert_text((100,220), 'Completed 3 projects. Total 120.', fontsize=18)
            p.insert_text((100,260), '员工能力评估：项目完成情况', fontname='china-s', fontsize=20)
        image = p.get_pixmap(dpi=200).tobytes('png')
        for i in range(pages):
            page = result.new_page(width=20000 if huge else 595, height=20000 if huge else 842)
            page.insert_image(page.rect, stream=image)
            if mixed:
                page.insert_text((100,110), 'Native text remains separate.')
            if crop:
                page.set_cropbox(pymupdf.Rect(50,70,550,800))
            page.set_rotation(rotation)
        return result.tobytes()


class OCRBoundaries(unittest.TestCase):
    def test_absent_and_changed_training_data_preserve_gaps_without_text(self):
        data = scanned_pdf()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'MEDIC_OCR_DATA_ROOT': directory}):
            result = extract(data, 'pdf', ocr=True)
            self.assertEqual(result.state, 'empty')
            self.assertIn('ocr_data_unavailable', {i.code for i in result.issues})
            (Path(directory)/'eng.traineddata').write_bytes(b'wrong')
            result = extract(data, 'pdf', ocr=True)
            self.assertIn('ocr_data_integrity_failure', {i.code for i in result.issues})
            self.assertFalse(result.blocks)

    def test_mixed_pages_are_not_ocrd_or_duplicated_and_page_budget_is_visible(self):
        with patch('medic.ingestion.ocr.recognize_page', return_value={'recognized_blocks':0}) as recognize:
            mixed = extract(scanned_pdf(mixed=True), 'pdf', ocr=True)
            recognize.assert_not_called()
            self.assertEqual(len(mixed.blocks), 1)
            self.assertIn('Native text remains separate.', mixed.blocks[0].quote)
            self.assertEqual(mixed.state, 'needs_review')
            result = extract(scanned_pdf(pages=6), 'pdf', ocr=True)
            self.assertEqual(recognize.call_count, 5)
            self.assertIn(('ocr_page_limit_exceeded','page:6'), {(i.code,i.locator) for i in result.issues})
            self.assertFalse(result.units[5]['ocr_applied'])

    def test_oversized_pages_and_engine_failure_do_not_remove_original_gaps(self):
        with patch('medic.ingestion.ocr.verified_data') as loading:
            result = extract(scanned_pdf(huge=True), 'pdf', ocr=True)
            loading.assert_not_called()
            self.assertIn('ocr_page_size_exceeded', {i.code for i in result.issues})
        with patch('medic.ingestion.ocr.recognize_page', side_effect=RuntimeError('private text must not leak')):
            result = extract(scanned_pdf(), 'pdf', ocr=True)
            self.assertIn('ocr_recognition_failed', {i.code for i in result.issues})
            self.assertNotIn('private text', result.model_dump_json())
            self.assertIn('page_without_extractable_text', {i.code for i in result.issues})

    def test_ocr_preview_has_no_guessed_native_rectangle(self):
        data = scanned_pdf()
        info, image = render_pdf(data, page_number=1, width=800, source_sha256=digest(data), highlight={'mode':'ocr_page'})
        self.assertEqual(info['highlight'], 'ocr_page_only')
        self.assertIsNone(info['highlight_pixels'])
        plain = render_pdf(data, page_number=1, width=800, source_sha256=digest(data))[1]
        self.assertEqual(image, plain)


@unittest.skipUnless(all((ocr.data_root()/name).is_file() for name in ocr.DATA_FILES), 'Pinned local OCR language data required')
class RealOCR(unittest.TestCase):
    def test_bilingual_rotated_cropped_pages_keep_source_provenance_and_review_gate(self):
        for crop in (False,True):
            for rotation in (0,90,180,270):
                with self.subTest(rotation=rotation,crop=crop):
                    data = scanned_pdf(rotation=rotation, crop=crop)
                    result = extract(data, 'pdf', ocr=True)
                    self.assertEqual(result.source_sha256, digest(data))
                    self.assertEqual(result.state, 'needs_review')
                    text = ''.join(''.join(b.quote.split()) for b in result.blocks)
                    self.assertIn('Syntheticscannedevidence2026', text)
                    self.assertIn('员工能力评估', text)
                    self.assertIn('项目完成情况', text)
                    self.assertTrue(result.units[0]['ocr_applied'])
                    self.assertTrue(result.blocks)
                    for block in result.blocks:
                        self.assertEqual(block.kind,'pdf_ocr')
                        self.assertEqual(block.locator['page'],1)
                        self.assertIn('ocr_block',block.locator)
                        self.assertNotIn('block',block.locator)
                        self.assertEqual(block.quality_flags,['ocr_text_requires_review'])
                        self.assertIsNone(block.metadata['confidence'])
                        self.assertFalse(block.metadata['human_verified'])
                        self.assertEqual(block.metadata['data_commit'],ocr.DATA_COMMIT)
                    with pymupdf.open(stream=data,filetype='pdf') as document:
                        self.assertEqual(document[0].rotation,rotation)
                        self.assertFalse(document[0].get_text().strip())

    def test_real_child_and_vault_deny_automatic_scoring_and_preserve_historical_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'synthetic.pdf';path.write_bytes(scanned_pdf())
            child=parse_file(path)
            self.assertTrue(child.blocks)
            self.assertEqual(child.state,'needs_review')
            vault=Vault(root/'vault')
            with patch.dict(os.environ,{'MEDIC_OCR_DATA_ROOT':str(root/'absent')}):
                old=vault.ingest('tenant','employee',path,period='synthetic',owner_claim='synthetic')
            new=vault.ingest('tenant','employee',path,period='synthetic',owner_claim='synthetic')
            self.assertEqual(old['extraction']['state'],'empty')
            self.assertTrue(new['extraction']['blocks'])
            self.assertNotEqual(old['snapshot_id'],new['snapshot_id'])
            self.assertEqual(old['source_sha256'],new['source_sha256'])
            self.assertEqual(vault.get('tenant','employee',old['snapshot_id']),old)
            with self.assertRaisesRegex(ValueError,'extraction_not_ready'):
                vault.material('tenant','employee',new['snapshot_id'])
            self.assertEqual(digest(path.read_bytes()),new['source_sha256'])

    def test_blank_page_and_output_budgets_fail_without_invented_text(self):
        result=extract(scanned_pdf(blank=True),'pdf',ocr=True)
        self.assertEqual(result.state,'empty')
        self.assertFalse(result.blocks)
        self.assertIn('ocr_no_text_recognized',{i.code for i in result.issues})
        for limits in ({'blocks':1},{'text_chars':5}):
            with self.subTest(limits=limits), self.assertRaisesRegex(ParseError,'(block|text)_limit_exceeded'):
                extract(scanned_pdf(),'pdf',limits,ocr=True)
