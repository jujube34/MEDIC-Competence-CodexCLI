"""Local, bounded OCR assistance. Recognized text always requires human review."""
from contextlib import contextmanager
import math
import os
from pathlib import Path
import tempfile

from medic.ingestion.contracts import ParseError
from medic.p0.common import digest

OCR_VERSION = 'medic-ocr-v1'
DATA_COMMIT = '65727574dfcd264acbb0c3e07860e4e9e9b22185'
LANGUAGE = 'chi_sim+eng'
DATA_FILES = {
    'eng.traineddata': (4113088, '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2'),
    'chi_sim.traineddata': (2469156, 'a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730'),
}
MAX_PAGES = 5
MAX_PIXELS = 4_000_000


def data_root():
    return Path(os.environ.get('MEDIC_OCR_DATA_ROOT', Path(__file__).resolve().parents[1]/'artifacts/ocr/tessdata-fast-4.1.0'))


@contextmanager
def verified_data(root=None):
    """Copy verified bytes into a private directory; engine never loads unchecked data."""
    root = Path(root) if root is not None else data_root()
    with tempfile.TemporaryDirectory(prefix='medic-ocr-data-') as temporary:
        try:
            for name, (size, expected) in DATA_FILES.items():
                path = root/name
                if not path.is_file():
                    raise ParseError('ocr_data_unavailable')
                if path.is_symlink() or path.stat().st_size != size:
                    raise ParseError('ocr_data_integrity_failure')
                with path.open('rb') as source:
                    data = source.read(size+1)
                if len(data) != size or digest(data) != expected:
                    raise ParseError('ocr_data_integrity_failure')
                (Path(temporary)/name).write_bytes(data)
        except OSError:
            raise ParseError('ocr_data_unavailable') from None
        yield temporary


def recognize_page(page, collector, page_number):
    """Called only inside the resource-bound parser, for an image-only page."""
    import pymupdf
    rect = page.rect
    if not all(math.isfinite(v) and 0 < v <= 20000 for v in (rect.width, rect.height)):
        raise ParseError('ocr_page_size_exceeded')
    dpi = min(200, math.floor(72*math.sqrt(MAX_PIXELS/(rect.width*rect.height))))
    while dpi >= 100 and math.ceil(rect.width*dpi/72)*math.ceil(rect.height*dpi/72) > MAX_PIXELS:
        dpi -= 1
    if dpi < 100:
        raise ParseError('ocr_page_size_exceeded')
    with verified_data() as training, pymupdf.open() as copy:
        # Render a private copy without viewer rotation or annotations. The
        # original page/bytes are untouched and crop bounds remain in force.
        copy.insert_pdf(page.parent, from_page=page.number, to_page=page.number, links=False, annots=False)
        target = copy[0]
        target.set_rotation(0)
        textpage = target.get_textpage_ocr(language=LANGUAGE, dpi=dpi, full=True, tessdata=training)
        raw = textpage.extractDICT()['blocks']
        blocks = []
        for block in raw:
            if block['type'] != 0:
                continue
            quote = '\n'.join(''.join(s['text'] for s in line['spans']) for line in block['lines'])
            if not quote.strip():
                continue
            bbox = [float(v) for v in block['bbox']]
            if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
                raise ParseError('ocr_invalid_geometry')
            blocks.append((quote, bbox))
        # Validate budgets before publishing any OCR blocks to the collector.
        if len(blocks)+len(collector.blocks) > collector.limits.blocks:
            raise ParseError('block_limit_exceeded')
        if sum(len(q) for q, _ in blocks)+collector.chars > collector.limits.text_chars:
            raise ParseError('text_limit_exceeded')
        provenance = {'ocr_applied': True, 'ocr_version': OCR_VERSION,
            'engine': 'PyMuPDF/'+pymupdf.version[0]+'+MuPDF/'+pymupdf.version[1]+'/Tesseract',
            'language': LANGUAGE, 'data_commit': DATA_COMMIT,
            'data_sha256': {name: pair[1] for name, pair in DATA_FILES.items()},
            'dpi': dpi, 'render_rotation': 0, 'coordinate_space': 'pymupdf_unrotated_page_points',
            'confidence': None, 'confidence_available': False, 'human_verified': False}
        for index, (quote, bbox) in enumerate(blocks, 1):
            collector.add('pdf_ocr', {'page': page_number, 'ocr_block': index, 'bbox': bbox}, quote,
                          provenance, flags=['ocr_text_requires_review'])
        return {**provenance, 'recognized_blocks': len(blocks)}
