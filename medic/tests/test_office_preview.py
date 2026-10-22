"""Office admission, conversion isolation and conservative text mapping."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import pymupdf
from medic.ingestion.contracts import ParseError
from medic.ingestion.office import convert_file,engine_available,verify_engine,engine_root
from medic.ingestion.office_preflight import check_office
from medic.ingestion.parser import extract
from medic.ingestion.preview import render_pdf
from medic.p0.common import digest
from medic.tests.test_ingestion import word,excel,W,PACKAGE_R,R


def add_parts(data,parts):
    output=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source,zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            if name not in parts:target.writestr(name,source.read(name))
        for name,value in parts.items():target.writestr(name,value)
    return output.getvalue()


class OfficeAdmission(unittest.TestCase):
    def test_external_resources_macros_embeds_and_dynamic_fields_rejected(self):
        cases=[({'word/vbaProject.bin':b'not executed'},'active_content'),
               ({'word/embeddings/object.bin':b'not executed'},'active_content'),
               ({'word/media/image.svg':'<svg/>'},'active_content'),
               ({'word/document.xml':f'<w:document xmlns:w="{W}"><w:instrText>DDEAUTO forbidden</w:instrText></w:document>'},'dynamic_field'),
               ({'word/_rels/document.xml.rels':f'<Relationships xmlns="{PACKAGE_R}"><Relationship Id="r1" Type="{R}/image" TargetMode="External" Target="http://127.0.0.1:9/sentinel"/></Relationships>'},'external_content'),
               ({'word/_rels/document.xml.rels':f'<Relationships xmlns="{PACKAGE_R}"><Relationship Id="r1" Type="{R}/image" Target="%5C%5Cremote%5Cshare"/></Relationships>'},'external_content')]
        for parts,code in cases:
            with self.subTest(code=code):
                with self.assertRaisesRegex(ParseError,code):check_office(add_parts(word(),parts),'docx')
        # Ordinary links are display text, never followed by the converter.
        link=f'<Relationships xmlns="{PACKAGE_R}"><Relationship Id="r1" Type="{R}/hyperlink" TargetMode="External" Target="https://example.invalid/"/></Relationships>'
        check_office(add_parts(word(),{'word/_rels/document.xml.rels':link}),'docx')

    def test_formula_allowlist_rejects_network_and_external_workbooks(self):
        for formula in ('WEBSERVICE(&quot;https://example.invalid&quot;)','_xlfn.IMAGE(&quot;https://example.invalid&quot;)','[1]Sheet1!A1','RTD(&quot;remote&quot;)','EXEC(&quot;process&quot;)'):
            with self.subTest(formula=formula):
                data=excel(f'<sheetData><row><c r="A1"><f>{formula}</f><v>0</v></c></row></sheetData>')
                with self.assertRaisesRegex(ParseError,'formula_not_supported'):check_office(data,'xlsx')
        data=excel('<sheetData><row><c r="A1"><f>SUM(B1:B5)</f><v>30</v></c></row></sheetData>')
        before=extract(data,'xlsx');check_office(data,'xlsx');self.assertEqual(extract(data,'xlsx'),before)
        with self.assertRaisesRegex(ParseError,'formula_not_supported'):
            check_office(add_parts(data,{'xl/defined.xml':'<definedName>WEBSERVICE(&quot;https://example.invalid/&quot;)</definedName>'}),'xlsx')

    def test_engine_manifest_change_rejected_before_loading_a_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'engine';root.mkdir();(root.parent/'engine.manifest.json').write_text('{}')
            if os.name=='nt':
                with self.assertRaisesRegex(ParseError,'engine_changed'):verify_engine(root)
                (root/'module.py').write_bytes(b'original')
                raw=json.dumps({'files':{'module.py':digest(b'original')}}).encode();(root.parent/'engine.manifest.json').write_bytes(raw)
                with patch('medic.ingestion.office.ENGINE_MANIFEST_SHA256',digest(raw)):
                    verify_engine(root);(root/'module.py').write_bytes(b'modified')
                    with self.assertRaisesRegex(ParseError,'engine_changed'):verify_engine(root)
                    (root/'module.py').write_bytes(b'original');(root/'unexpected.py').write_bytes(b'additional')
                    with self.assertRaisesRegex(ParseError,'engine_changed'):verify_engine(root)


class OfficeMapping(unittest.TestCase):
    def test_unique_text_selects_actual_page_but_repetition_never_highlights(self):
        quote='Distinct evidence in the second page.'
        h={'mode':'unique_text','quote':quote,'quote_hash':digest(quote),'source_unique':True}
        with pymupdf.open() as d:
            d.new_page().insert_text((40,80),'Unrelated first page.')
            d.new_page().insert_text((40,80),quote);data=d.tobytes()
        info,png=render_pdf(data,page_number=1,width=800,source_sha256=digest(data),highlight=h)
        self.assertEqual(info['page'],2);self.assertEqual(info['highlight'],'unique_text');self.assertIsNotNone(info['highlight_pixels'])
        for changes in ({'source_unique':False},{'quote_hash':'0'*64},{'quote':'in','quote_hash':digest('in')}):
            info,_=render_pdf(data,page_number=1,width=800,source_sha256=digest(data),highlight={**h,**changes})
            self.assertEqual(info['highlight'],'unavailable');self.assertEqual(info['page'],1)
        with pymupdf.open(stream=data,filetype='pdf') as d:
            d[0].insert_text((40,130),quote);duplicate=d.tobytes()
        info,marked=render_pdf(duplicate,page_number=1,width=800,source_sha256=digest(duplicate),highlight=h)
        self.assertEqual(info['highlight'],'unavailable')
        self.assertEqual(marked,render_pdf(duplicate,page_number=1,width=800,source_sha256=digest(duplicate))[1])


@unittest.skipUnless(os.name=='nt','Windows converter harness')
class OfficeProcess(unittest.TestCase):
    def test_timeout_closes_whole_converter_job(self):
        real=subprocess.Popen;children=[]
        def start(*args,**kwargs):
            grand='import os,time; from pathlib import Path; Path('+repr(str(grand_pid))+').write_text(str(os.getpid())); time.sleep(60)'
            script='import json,sys,time,os,subprocess; from pathlib import Path; from medic.ingestion.preview_worker import join_job; r=json.loads(sys.stdin.buffer.read()); join_job(r["job_name"]); Path('+repr(str(worker_pid))+').write_text(str(os.getpid())); subprocess.Popen([sys.executable,"-c",'+repr(grand)+'],creationflags=subprocess.CREATE_NO_WINDOW); time.sleep(60)'
            child=real([sys.executable,'-c',script],**kwargs);children.append(child);return child
        with tempfile.TemporaryDirectory() as temp:
            worker_pid=Path(temp)/'worker.pid';grand_pid=Path(temp)/'grand.pid'
            path=Path(temp)/'input.docx';path.write_bytes(word())
            with patch('medic.ingestion.office.engine_available',return_value=True),patch('medic.ingestion.office.subprocess.Popen',side_effect=start):
                with self.assertRaisesRegex(ParseError,'conversion_timeout'):convert_file(path,kind='docx',source_sha256=digest(path.read_bytes()),limits={'seconds':3})
            self.assertIsNotNone(children[0].poll())
            import ctypes
            from ctypes import wintypes as w
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
            kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];kernel.CloseHandle.argtypes=[w.HANDLE]
            for pidfile in (worker_pid,grand_pid):
                self.assertTrue(pidfile.exists());handle=kernel.OpenProcess(0x100000,False,int(pidfile.read_text()))
                if handle:
                    try:self.assertEqual(kernel.WaitForSingleObject(handle,3000),0)
                    finally:kernel.CloseHandle(handle)

    def test_real_conversion_all_three_formats_preserves_original_and_extraction(self):
        fixtures=os.environ.get('MEDIC_TEST_OFFICE_FIXTURES')
        if not fixtures or not engine_available():self.skipTest('Pinned engine and synthetic Office fixtures required')
        for kind,pages,needle in [('docx',1,'Synthetic Office preview'),('pptx',2,'Distinct page evidence 2'),('xlsx',1,'Synthetic worksheet')]:
            with self.subTest(kind=kind):
                path=Path(fixtures)/('synthetic-office.'+kind);original=path.read_bytes();before=extract(original,kind)
                info,pdf=convert_file(path,kind=kind,source_sha256=digest(original))
                self.assertEqual(info['page_count'],pages);self.assertEqual(info['pdf_sha256'],digest(pdf))
                self.assertEqual(path.read_bytes(),original);self.assertEqual(extract(original,kind),before)
                self.assertTrue(info['quality_state_unchanged']);self.assertFalse(info['ocr_applied'])
                with pymupdf.open(stream=pdf,filetype='pdf') as d:self.assertIn(needle,' '.join(p.get_text() for p in d))
                self.assertEqual(verify_engine(engine_root())['engine_manifest_sha256'],info['engine_manifest_sha256'])

    def test_real_child_rejects_external_reference_before_engine_start(self):
        if not engine_available():self.skipTest('Pinned engine required')
        data=add_parts(word(),{'word/_rels/document.xml.rels':f'<Relationships xmlns="{PACKAGE_R}"><Relationship Id="r1" Type="{R}/image" TargetMode="External" Target="http://127.0.0.1:9/sentinel"/></Relationships>'})
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'external.docx';path.write_bytes(data)
            with self.assertRaisesRegex(ParseError,'external_content_rejected'):convert_file(path,kind='docx',source_sha256=digest(data))
