"""Extraction core. Application uploads enter through bounded.parse_file."""
import importlib.metadata
import io
from collections import Counter

from medic.ingestion.contracts import Collector, Extraction, ParseError, ParseLimits
from medic.ingestion.ooxml import Package, docx, pptx, xlsx
from medic.p0.common import digest


def pdf(data, collector, *, ocr=False):
    import pymupdf
    from pypdf import PdfReader
    collector.engine = 'PyMuPDF/'+importlib.metadata.version('PyMuPDF')+'+pypdf/'+importlib.metadata.version('pypdf')
    with pymupdf.open(stream=data, filetype='pdf') as document:
        if document.needs_pass:
            raise ParseError('encrypted_pdf')
        if document.page_count > collector.limits.pages:
            raise ParseError('page_limit_exceeded')
        try:
            independent = PdfReader(io.BytesIO(data), strict=False)
            if len(independent.pages) != document.page_count:
                collector.issue('pdf_page_count_disagreement', 'document')
        except Exception:
            independent = None
            collector.issue('independent_pdf_extraction_failed', 'document')
        ocr_attempts = 0
        for index, page in enumerate(document, 1):
            # TEXTFLAGS_TEXT excludes image byte payloads. Images are counted
            # separately and never silently described as extracted text.
            blocks = page.get_text('dict', flags=pymupdf.TEXTFLAGS_TEXT)['blocks']
            text_blocks = [b for b in blocks if b['type'] == 0]
            quotes = ['\n'.join(''.join(span['text'] for span in line['spans']) for line in b['lines']) for b in text_blocks]
            agreement = None
            try:
                if independent is not None:
                    other_text = independent.pages[index-1].extract_text() or ''
                    agreement = Counter(ch for ch in ''.join(quotes) if not ch.isspace()) == Counter(ch for ch in other_text if not ch.isspace())
                    if not agreement:
                        collector.issue('pdf_text_extraction_disagreement', 'page:'+str(index))
            except Exception:
                collector.issue('independent_pdf_extraction_failed', 'page:'+str(index))
            image_count = len(page.get_image_info())
            vector_count = len(page.get_drawings())
            collector.units.append({'kind': 'page', 'page': index, 'width': float(page.rect.width),
                'height': float(page.rect.height), 'text_blocks': len(text_blocks), 'images': image_count,
                'vector_paths': vector_count, 'ocr_applied': False, 'cross_engine_text_agreement': agreement})
            if image_count:
                collector.issue('image_content_requires_ocr_or_review', 'page:'+str(index))
            if vector_count:
                collector.issue('vector_content_not_interpreted', 'page:'+str(index))
            if not text_blocks:
                collector.issue('page_without_extractable_text', 'page:'+str(index))
            if page.first_annot is not None or page.first_widget is not None:
                collector.issue('annotations_or_forms_require_review', 'page:'+str(index))
            for bi, block in enumerate(text_blocks, 1):
                quote = quotes[bi-1]
                collector.add('pdf_text', {'page': index, 'block': bi, 'bbox': [float(v) for v in block['bbox']]}, quote,
                              {'ocr_applied': False, 'coordinate_space': 'pymupdf_unrotated_page_points'},
                              flags=['pdf_text_extraction_disagreement'] if agreement is False else [])
            if ocr and image_count and not any(q.strip() for q in quotes):
                from medic.ingestion.ocr import MAX_PAGES, recognize_page
                if ocr_attempts >= MAX_PAGES:
                    collector.issue('ocr_page_limit_exceeded', 'page:'+str(index))
                    continue
                ocr_attempts += 1
                try:
                    info = recognize_page(page, collector, index)
                    collector.units[-1]['ocr'] = info
                    collector.units[-1]['ocr_applied'] = True
                    collector.issue('ocr_text_requires_review' if info['recognized_blocks'] else 'ocr_no_text_recognized', 'page:'+str(index))
                except ParseError as error:
                    if str(error) in ('text_limit_exceeded', 'block_limit_exceeded'):
                        raise
                    collector.issue(str(error), 'page:'+str(index))
                except Exception:
                    collector.issue('ocr_recognition_failed', 'page:'+str(index))


def extract(data, kind, limits=None, *, ocr=False):
    limits = ParseLimits.model_validate(limits or {})
    if kind not in ('pdf', 'docx', 'pptx', 'xlsx'):
        raise ParseError('unsupported_format')
    if len(data) > limits.input_bytes:
        raise ParseError('input_size_exceeded')
    collector = Collector(data, kind, limits)
    try:
        if kind == 'pdf':
            if not data.startswith(b'%PDF-'):
                raise ParseError('format_signature_mismatch')
            pdf(data, collector, ocr=ocr)
        else:
            if not data.startswith(b'PK'):
                raise ParseError('format_signature_mismatch')
            collector.engine += '/defusedxml-'+importlib.metadata.version('defusedxml')
            package = Package(data, collector)
            try:
                {'docx': docx, 'pptx': pptx, 'xlsx': xlsx}[kind](package)
            finally:
                package.zip.close()
        return collector.result()
    except ParseError:
        raise
    except Exception:
        # Library errors can contain source text, file paths or external URLs.
        raise ParseError('document_parse_failed') from None


def failure(source_hash, kind, code):
    from medic.ingestion.contracts import Issue
    return Extraction(source_sha256=source_hash, format=kind, state='failed', engine='not_completed',
                      units=[], blocks=[], issues=[Issue(code=code, locator='document', blocking=True)])
