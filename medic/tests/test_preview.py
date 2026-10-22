"""Original-file rendering and evidence geometry; all documents are synthetic."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pymupdf
from medic.apps.api.database import AppError
from medic.apps.api.preview import preview_slots
from medic.ingestion.contracts import ParseError
from medic.ingestion.parser import extract
from medic.ingestion.preview import MAX_PIXELS,render_pdf,render_file
from medic.ingestion.vault import Vault
from medic.p0.common import digest


def pdf_fixture(rotation=0,crop=False):
    with pymupdf.open() as d:
        p=d.new_page(width=600,height=800)
        p.insert_text((120,200),'Synthetic exact evidence. No employee facts.',fontsize=14)
        if crop:p.set_cropbox(pymupdf.Rect(60,80,540,720))
        p.set_rotation(rotation)
        d.new_page(width=300,height=450).insert_text((40,80),'Second page - distinct evidence.')
        return d.tobytes()


def highlight(data):
    block=extract(data,'pdf').blocks[0]
    return {**block.locator,'quote_hash':digest(block.quote)}


class PreviewGeometry(unittest.TestCase):
    def test_exact_red_rectangle_matches_rotated_and_cropped_originals(self):
        for crop in (False,True):
            for rotation in (0,90,180,270):
                with self.subTest(crop=crop,rotation=rotation):
                    data=pdf_fixture(rotation,crop);h=highlight(data)
                    info,png=render_pdf(data,page_number=1,width=1200,source_sha256=digest(data),highlight=h)
                    self.assertEqual(info['highlight'],'exact_block');self.assertFalse(info['ocr_applied'])
                    self.assertEqual(digest(data),info['source_sha256'])
                    x0,y0,x1,y1=h['bbox'];w,hgt=(480,640) if crop else (600,800)
                    box={0:(x0,y0,x1,y1),90:(hgt-y1,x0,hgt-y0,x1),180:(w-x1,hgt-y1,w-x0,hgt-y0),270:(y0,w-x1,y1,w-x0)}[rotation]
                    scale=1200/(hgt if rotation in (90,270) else w)
                    expected=[x*scale for x in box]
                    for actual,desired in zip(info['highlight_pixels'],expected):self.assertAlmostEqual(actual,desired,places=3)
                    pix=pymupdf.Pixmap(png);pixels=pix.samples
                    # Locate red pixels independently of the returned rectangle metadata.
                    red=[(i//3%pix.width,i//3//pix.width) for i in range(0,len(pixels),3)
                         if pixels[i]>150 and pixels[i+1]<100 and pixels[i+2]<100]
                    self.assertTrue(red)
                    observed=(min(x for x,y in red),min(y for x,y in red),max(x for x,y in red),max(y for x,y in red))
                    for actual,desired in zip(observed,expected):self.assertLessEqual(abs(actual-desired),4)
                    self.assertEqual(extract(data,'pdf').blocks[0].locator['bbox'],highlight(data)['bbox'])

    def test_mismatched_quote_or_coordinate_never_gets_a_red_box(self):
        data=pdf_fixture();original=highlight(data)
        for changes in ({'quote_hash':digest('different quote')},{'bbox':[1,2,3,4]},{'block':999}):
            info,png=render_pdf(data,page_number=1,width=800,source_sha256=digest(data),highlight={**original,**changes})
            self.assertEqual(info['highlight'],'unavailable');self.assertIsNone(info['highlight_pixels'])
            plain=render_pdf(data,page_number=1,width=800,source_sha256=digest(data))[1]
            self.assertEqual(png,plain)

    def test_pixel_bounds_blank_pages_and_original_integrity(self):
        with pymupdf.open() as d:
            d.new_page(width=1000,height=12000);data=d.tobytes()
        original=extract(data,'pdf')
        info,png=render_pdf(data,page_number=1,width=1600,source_sha256=digest(data))
        self.assertLessEqual(info['width']*info['height'],MAX_PIXELS);self.assertLessEqual(info['height'],8192)
        self.assertEqual(original.state,'empty');self.assertEqual(extract(data,'pdf'),original)
        for options,code in [({'source_sha256':'0'*64},'source_changed'),({'page_number':2},'page_not_found'),({'width':1601},'invalid_preview')]:
            with self.assertRaisesRegex(ParseError,code):render_pdf(data,**{'page_number':1,'width':1200,'source_sha256':digest(data),**options})
        with pymupdf.open(stream=data,filetype='pdf') as d:
            encrypted=d.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,user_pw='secret',owner_pw='owner')
        with self.assertRaisesRegex(ParseError,'encrypted_pdf'):render_pdf(encrypted,page_number=1,width=800,source_sha256=digest(encrypted))


class PreviewIsolation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'source.pdf';self.data=pdf_fixture(90,True);self.path.write_bytes(self.data)

    def test_real_child_preserves_source_and_returns_verified_image(self):
        info,png=render_file(self.path,page_number=1,width=800,source_sha256=digest(self.data),highlight=highlight(self.data))
        self.assertEqual(info['highlight'],'exact_block');self.assertEqual(info['image_sha256'],digest(png))
        self.assertEqual(self.path.read_bytes(),self.data)
        with self.assertRaisesRegex(ParseError,'source_changed'):
            render_file(self.path,page_number=1,width=800,source_sha256='0'*64)

    def test_timeout_terminates_render_child(self):
        real=subprocess.Popen;children=[]
        def start(*args,**kwargs):
            command='import json,sys,time; r=json.loads(sys.stdin.buffer.read()); '
            if os.name=='nt':command+='from medic.ingestion.preview_worker import join_job; join_job(r["job_name"]); '
            child=real([sys.executable,'-c',command+'time.sleep(60)'],**kwargs);children.append(child);return child
        with patch('medic.ingestion.preview.subprocess.Popen',side_effect=start):
            with self.assertRaisesRegex(ParseError,'preview_timeout'):
                render_file(self.path,page_number=1,width=800,source_sha256=digest(self.data),limits={'seconds':1})
        self.assertIsNotNone(children[0].poll())

    def test_cross_instance_slots_release_after_error_and_keep_user_scope(self):
        vault=Vault(Path(self.temp.name)/'vault');other=Vault(vault.root)
        with preview_slots(vault,'T','U'):
            with self.assertRaisesRegex(AppError,'preview_busy'):
                with preview_slots(other,'T','U'):pass
            with preview_slots(other,'T','V'):
                with self.assertRaisesRegex(AppError,'preview_busy'):
                    with preview_slots(vault,'T','W'):pass
        with self.assertRaisesRegex(RuntimeError,'synthetic'):
            with preview_slots(other,'T','U'):raise RuntimeError('synthetic')
        with preview_slots(vault,'T','U'):pass
