import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from contextlib import closing

from medic.domain.contracts import AssessmentInput
from medic.domain.graph import source_groups
from medic.domain.validation import verify_input
from medic.ingestion.bounded import parse_file
from medic.ingestion.contracts import ParseError
from medic.ingestion.parser import extract
from medic.ingestion.vault import Vault
from medic.p0.common import digest
from medic.tests.test_runtime import fixture

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PACKAGE_R = 'http://schemas.openxmlformats.org/package/2006/relationships'


def package(parts):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        for name, content in parts.items(): archive.writestr(name, content)
    return stream.getvalue()


def word(text='Exact  text\tkept'):
    from xml.sax.saxutils import escape
    return package({'word/document.xml': f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p></w:body></w:document>'})


def excel(sheet, *, hidden=False, extra=None):
    return package({'xl/workbook.xml': f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="核验表" sheetId="7" state="{"hidden" if hidden else "visible"}" r:id="r7"/></sheets></workbook>',
                    'xl/_rels/workbook.xml.rels': f'<Relationships xmlns="{PACKAGE_R}"><Relationship Id="r7" Type="{R}/worksheet" Target="worksheets/sheet7.xml"/></Relationships>',
                    'xl/worksheets/sheet7.xml': f'<worksheet xmlns="{S}">{sheet}</worksheet>', **(extra or {})})


def presentation():
    shape = f'<p:sp><p:nvSpPr><p:cNvPr id="9" name="Text"/></p:nvSpPr><p:txBody><a:p><a:r><a:t>Slide two first</a:t></a:r></a:p></p:txBody></p:sp>'
    table = '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="12" name="Table"/></p:nvGraphicFramePr><a:graphic><a:graphicData><a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>Table cell</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>'
    note = '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Notes"/></p:nvSpPr><p:txBody><a:p><a:r><a:t>Private speaker note</a:t></a:r></a:p></p:txBody></p:sp>'
    return package({'ppt/presentation.xml': f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="256" r:id="r2"/></p:sldIdLst></p:presentation>',
        'ppt/_rels/presentation.xml.rels': f'<Relationships xmlns="{PACKAGE_R}"><Relationship Id="r2" Type="{R}/slide" Target="slides/slide2.xml"/></Relationships>',
        'ppt/slides/slide2.xml': f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>{shape}{table}</p:spTree></p:cSld></p:sld>',
        'ppt/slides/_rels/slide2.xml.rels': f'<Relationships xmlns="{PACKAGE_R}"><Relationship Id="notes" Type="{R}/notesSlide" Target="../notesSlides/notesSlide4.xml"/></Relationships>',
        'ppt/notesSlides/notesSlide4.xml': f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>{note}</p:spTree></p:cSld></p:notes>'})


class ExtractionContracts(unittest.TestCase):
    def test_formula_only_uncached_row_is_retained_and_blocks_automatic_use(self):
        data = excel('<sheetData><row r="1"><c r="A1"><f>1+1</f><v/></c></row></sheetData>')
        result = extract(data, 'xlsx')
        self.assertEqual(result.state, 'needs_review')
        self.assertEqual(len(result.blocks), 1)
        cell = result.blocks[0]
        self.assertEqual(cell.quote, '=1+1')
        self.assertEqual(cell.locator['cell'], 'A1')
        self.assertEqual(cell.metadata['cache_state'], 'missing')
        self.assertIn('formula_uncached', cell.quality_flags)

    def test_cached_formula_hidden_sheet_and_sparse_wide_cell_keep_coordinates(self):
        data = excel('<cols><col min="1" max="1" hidden="1"/></cols><sheetData><row r="1" hidden="1"><c r="A1"><f>1+1</f><v>2</v></c></row><row r="1048576"><c r="XFD1048576" t="inlineStr"><is><t xml:space="preserve">Wide  value</t></is></c></row></sheetData>', hidden=True)
        result = extract(data, 'xlsx')
        self.assertEqual(len(result.blocks), 2)
        self.assertEqual(result.blocks[0].quote, '2')
        self.assertEqual(result.blocks[0].metadata['formula'], '=1+1')
        self.assertFalse(result.blocks[0].metadata['recalculated'])
        self.assertTrue({'hidden_sheet','hidden_row','hidden_column','formula_cache_not_recomputed'} <= set(result.blocks[0].quality_flags))
        self.assertEqual(result.blocks[1].locator['cell'], 'XFD1048576')
        self.assertEqual(result.blocks[1].quote, 'Wide  value')
        self.assertEqual(result.blocks[1].search_text, 'Wide value')

    def test_mixed_formula_caches_do_not_hide_missing_cells(self):
        result = extract(excel('<sheetData><row><c r="A1"><f>1+1</f><v>2</v></c><c r="B1"><f>3+3</f></c><c r="C1" t="e"><v>#REF!</v></c></row></sheetData>'), 'xlsx')
        self.assertEqual([b.locator['cell'] for b in result.blocks], ['A1','B1','C1'])
        self.assertEqual(result.state, 'needs_review')
        self.assertEqual({i.code for i in result.issues}, {'formula_uncached','cell_error'})

    def test_word_tables_headers_and_notes_are_structural_not_invented_pages(self):
        body = '<w:p><w:r><w:t xml:space="preserve">  Alpha </w:t><w:tab/><w:t>Beta</w:t><w:br/><w:t>Line two</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell text</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        data = package({'word/document.xml': f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>',
                        'word/header1.xml': f'<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>Header</w:t></w:r></w:p></w:hdr>',
                        'word/footnotes.xml': f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="1"><w:p><w:r><w:t>Footnote</w:t></w:r></w:p></w:footnote></w:footnotes>'})
        result = extract(data,'docx')
        self.assertEqual(result.state,'ready')
        self.assertEqual(result.blocks[0].quote,'  Alpha \tBeta\nLine two')
        self.assertIn('/tbl[1]/tr[1]/tc[1]/p[1]',result.blocks[1].locator['path'])
        self.assertTrue(all('page' not in b.locator for b in result.blocks))
        self.assertEqual({b.quote for b in result.blocks[2:]},{'Header','Footnote'})

    def test_revision_and_media_gaps_remain_explicit(self):
        data = package({'word/document.xml': f'<w:document xmlns:w="{W}"><w:body><w:p><w:del><w:r><w:delText>Deleted claim</w:delText></w:r></w:del></w:p></w:body></w:document>', 'word/media/pic.png': b'not-decoded'})
        result=extract(data,'docx')
        self.assertEqual(result.blocks[0].quote,'Deleted claim')
        self.assertEqual(result.state,'needs_review')
        self.assertIn('embedded_content_not_extracted',{i.code for i in result.issues})

    def test_slide_shape_table_and_notes_locations_follow_presentation_order(self):
        result=extract(presentation(),'pptx')
        self.assertEqual(result.state,'ready')
        self.assertEqual([b.quote for b in result.blocks],['Slide two first','Table cell','Private speaker note'])
        self.assertEqual(result.blocks[0].locator['slide'],1)
        self.assertEqual(result.blocks[0].locator['shape'],'9')
        self.assertEqual(result.blocks[1].locator['row'],1)
        self.assertEqual(result.blocks[2].locator['layer'],'notes')

    def test_pdf_keeps_page_coordinates_without_duplicate_page_text(self):
        import pymupdf
        with pymupdf.open() as document:
            page=document.new_page(); page.insert_text((50,70),'Verbatim PDF evidence')
            result=extract(document.tobytes(),'pdf')
        self.assertEqual(result.state,'ready')
        self.assertEqual(len(result.blocks),1)
        self.assertEqual(result.blocks[0].quote,'Verbatim PDF evidence')
        self.assertEqual(result.blocks[0].locator['page'],1)
        self.assertEqual(len(result.blocks[0].locator['bbox']),4)
        self.assertFalse(result.units[0]['ocr_applied'])

    def test_scanned_and_encrypted_pdf_do_not_become_empty_capability_evidence(self):
        import pymupdf
        with pymupdf.open() as document:
            page=document.new_page(); pix=pymupdf.Pixmap(pymupdf.csRGB, (0,0,2,2), False); pix.clear_with(100)
            page.insert_image(pymupdf.Rect(20,20,100,100),pixmap=pix)
            result=extract(document.tobytes(),'pdf')
            self.assertEqual(result.state,'empty')
            self.assertEqual(result.blocks,[])
            self.assertIn('image_content_requires_ocr_or_review',{i.code for i in result.issues})
            encrypted=document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,owner_pw='owner',user_pw='user')
        with self.assertRaisesRegex(ParseError,'encrypted_pdf'): extract(encrypted,'pdf')

    def test_clipped_pdf_text_disagreement_requires_review(self):
        import pymupdf
        with pymupdf.open() as document:
            page=document.new_page(width=100,height=100)
            page.insert_text((92,40),'Clipped text outside the page')
            result=extract(document.tobytes(),'pdf')
        self.assertIn('pdf_text_extraction_disagreement',{i.code for i in result.issues})
        self.assertNotEqual(result.state,'ready')

    def test_zip_total_budget_entities_and_duplicate_coordinates_fail_closed(self):
        data=excel('<sheetData/>',extra={'xl/media/large.bin':b'x'*10000})
        with self.assertRaisesRegex(ParseError,'archive_budget_exceeded'): extract(data,'xlsx',{'expanded_bytes':5000})
        evil=package({'word/document.xml': f'<!DOCTYPE x [<!ENTITY y SYSTEM "file:///never-read-private">]><w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&y;</w:t></w:r></w:p></w:body></w:document>'})
        with self.assertRaisesRegex(ParseError,'document_parse_failed'): extract(evil,'docx')
        with self.assertRaisesRegex(ParseError,'invalid_or_duplicate_cell'):
            extract(excel('<sheetData><row><c r="A1"><v>1</v></c><c r="A1"><v>2</v></c></row></sheetData>'),'xlsx')
        with self.assertRaisesRegex(ParseError,'unsafe_archive_member'):
            extract(package({'../outside.xml':'x'}),'docx')

    def test_full_snapshot_limits_fail_instead_of_silently_truncating(self):
        data=excel('<sheetData><row><c r="A1"><v>111</v></c><c r="B1"><v>222</v></c></row></sheetData>')
        with self.assertRaisesRegex(ParseError,'block_limit_exceeded'): extract(data,'xlsx',{'blocks':1})
        with self.assertRaisesRegex(ParseError,'text_limit_exceeded'): extract(data,'xlsx',{'text_chars':5})


class IsolatedParser(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)/'input.docx'; self.path.write_bytes(word())

    def tearDown(self): self.temp.cleanup()

    def test_real_child_returns_verbatim_text_and_frozen_build(self):
        result=parse_file(self.path)
        self.assertEqual(result.blocks[0].quote,'Exact  text\tkept')

    def test_wall_timeout_terminates_the_actual_child(self):
        real=subprocess.Popen; children=[]
        def start(*args,**kwargs):
            process=real([sys.executable,'-c','import sys,time; sys.stdin.buffer.read(); time.sleep(60)'],**kwargs)
            children.append(process); return process
        with patch('medic.ingestion.bounded.subprocess.Popen',side_effect=start):
            with self.assertRaisesRegex(ParseError,'parser_timeout'): parse_file(self.path,limits={'seconds':1})
        self.assertIsNotNone(children[0].poll())

    @unittest.skipUnless(os.name=='nt','Windows Job Object memory contract')
    def test_windows_process_memory_limit_is_enforced(self):
        real=subprocess.Popen; children=[]
        def start(*args,**kwargs):
            process=real([sys.executable,'-c','import sys; sys.stdin.buffer.read(); x=bytearray(256*1024*1024)'],**kwargs)
            children.append(process); return process
        with patch('medic.ingestion.bounded.subprocess.Popen',side_effect=start):
            with self.assertRaisesRegex(ParseError,'parser_process_failed'): parse_file(self.path,limits={'memory_bytes':128*1024*1024})
        self.assertNotEqual(children[0].returncode,0)


class MaterialVault(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.vault=Vault(self.root/'vault'); self.input=self.root/'source.docx'; self.input.write_bytes(word())

    def tearDown(self): self.temp.cleanup()

    def upload(self, **kwargs):
        return self.vault.ingest('tenant','employee',self.input,period='synthetic-period',owner_claim='self_reported',**kwargs)

    def test_reupload_has_new_snapshot_stable_blob_and_cannot_inflate_corroboration(self):
        first=self.upload(source_event_id='event-a'); second=self.upload(source_event_id='event-b')
        self.assertNotEqual(first['snapshot_id'],second['snapshot_id'])
        self.assertEqual(first['source_sha256'],second['source_sha256'])
        self.assertEqual(len(list((self.vault.root/first['scope']/'blobs').iterdir())),1)
        one=self.vault.material('tenant','employee',first['snapshot_id']); two=self.vault.material('tenant','employee',second['snapshot_id'])
        source=fixture(); source['materials']=[one.model_dump(),two.model_dump()]
        source=AssessmentInput.model_validate(source); verify_input(source)
        self.assertEqual(len(source_groups(source)),1)
        self.assertNotEqual(one.spans[0].span_hash,two.spans[0].span_hash)
        self.input.write_bytes(word('Changed original'))
        self.assertEqual(self.vault.material('tenant','employee',first['snapshot_id']).spans[0].quote,'Exact  text\tkept')

    def test_cross_employee_scope_and_snapshot_tampering_are_rejected(self):
        saved=self.upload(); ident=saved['snapshot_id']
        for tenant,employee in [('tenant','other'),('other','employee')]:
            with self.assertRaisesRegex(ValueError,'not_found_in_scope'): self.vault.get(tenant,employee,ident)
        with closing(sqlite3.connect(self.vault.database)) as db, db:
            db.execute('UPDATE snapshots SET payload=? WHERE id=?',(json.dumps({**saved,'owner_claim':'forged'}),ident))
        with self.assertRaisesRegex(ValueError,'snapshot_integrity'): self.vault.get('tenant','employee',ident)

    def test_failed_or_incomplete_parsing_cannot_supply_assessment_spans(self):
        self.input.write_bytes(b'not an office file')
        saved=self.upload()
        self.assertEqual(saved['extraction']['state'],'failed')
        self.assertEqual(saved['extraction']['blocks'],[])
        with self.assertRaisesRegex(ValueError,'extraction_not_ready'): self.vault.material('tenant','employee',saved['snapshot_id'])
        path=self.root/'uncached.xlsx'; path.write_bytes(excel('<sheetData><row><c r="A1"><f>1+1</f></c></row></sheetData>'))
        saved=self.vault.ingest('tenant','employee',path,period='p',owner_claim='self_reported')
        self.assertEqual(saved['extraction']['state'],'needs_review')
        with self.assertRaisesRegex(ValueError,'extraction_not_ready'): self.vault.material('tenant','employee',saved['snapshot_id'])

    def test_blob_tampering_is_detected_before_material_read(self):
        saved=self.upload()
        (self.vault.root/saved['scope']/'blobs'/saved['source_sha256']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'stored_source_hash'): self.vault.get('tenant','employee',saved['snapshot_id'])


if __name__=='__main__': unittest.main()
