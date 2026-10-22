"""Read OOXML without executing macros, evaluating formulas or fetching links."""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
from pathlib import PurePosixPath
from defusedxml.ElementTree import fromstring

from medic.ingestion.contracts import ParseError

NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
      's': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}


def tag(prefix, name):
    return '{'+NS[prefix]+'}'+name


class Package:
    def __init__(self, data, collector):
        self.c = collector
        self.zip = zipfile.ZipFile(io.BytesIO(data))
        infos = self.zip.infolist()
        limits = collector.limits
        if len(infos) > limits.members or sum(i.file_size for i in infos) > limits.expanded_bytes:
            raise ParseError('archive_budget_exceeded')
        self.names = set()
        for item in infos:
            path = PurePosixPath(item.filename)
            if (item.filename.startswith('/') or '\\' in item.filename or ':' in item.filename
                    or '..' in path.parts or item.filename in self.names):
                raise ParseError('unsafe_archive_member')
            if item.flag_bits & 1 or item.file_size > limits.member_bytes:
                raise ParseError('archive_member_not_allowed')
            self.names.add(item.filename)
        if '[Content_Types].xml' not in self.names:
            raise ParseError('invalid_office_package')
        # Every member counts, including large embedded binaries, not just XML.
        for name in sorted(self.names):
            lower = name.lower()
            if 'vbaproject' in lower:
                self.c.issue('macros_not_executed', name)
            if '/embeddings/' in lower or '/media/' in lower or '/charts/' in lower and lower.endswith('.xml'):
                self.c.issue('embedded_content_not_extracted', name)
            if lower.endswith('.rels'):
                for rel in self.xml(name):
                    if rel.get('TargetMode') == 'External':
                        self.c.issue('external_reference_not_fetched', name, False)

    def xml(self, name):
        if name not in self.names:
            raise ParseError('missing_office_part')
        # Defused XML rejects entities/DTDs; ZIP contents are never extracted.
        return fromstring(self.zip.read(name), forbid_dtd=True)

    def relationships(self, part):
        relpath = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part)+'.rels')
        if relpath not in self.names:
            return {}
        result = {}
        for rel in self.xml(relpath):
            if rel.get('TargetMode') == 'External':
                continue
            target = rel.get('Target', '')
            if '\\' in target or ':' in target:
                raise ParseError('unsafe_relationship')
            target = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(posixpath.dirname(part), target))
            if target.startswith('../') or target not in self.names or rel.get('Id') in result:
                raise ParseError('invalid_relationship_target')
            result[rel.get('Id')] = (target, rel.get('Type', '').rsplit('/', 1)[-1])
        return result


def text_runs(node, prefix):
    values = []
    def visit(element):
        if element.tag == tag('s', 'rPh'):
            return
        if element.tag in (tag(prefix, 't'), tag(prefix, 'delText')):
            values.append(element.text or '')
        elif element.tag == tag(prefix, 'tab'):
            values.append('\t')
        elif element.tag in (tag(prefix, 'br'), tag(prefix, 'cr')):
            values.append('\n')
        else:
            for child in element:
                visit(child)
    visit(node)
    return ''.join(values)


def docx(package):
    c = package.c
    parts = ['word/document.xml'] + sorted(n for n in package.names if re.fullmatch(r'word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml', n))
    def walk(node, part, path):
        counters = {}
        for child in node:
            local = child.tag.rsplit('}', 1)[-1]
            counters[local] = counters.get(local, 0)+1
            childpath = path+'/'+local+'['+str(counters[local])+']'
            if child.tag == tag('w', 'p'):
                flags = []
                if any(x.tag in (tag('w', 'ins'), tag('w', 'del'), tag('w', 'delText')) for x in child.iter()):
                    flags.append('tracked_changes_present')
                    c.issue('tracked_changes_require_review', part+childpath)
                if any(x.tag in (tag('w', 'drawing'), tag('w', 'pict'), tag('w', 'object')) for x in child.iter()):
                    c.issue('drawing_not_extracted', part+childpath)
                if child.find('.//w:vanish', NS) is not None:
                    flags.append('hidden_text')
                    c.issue('hidden_text_requires_review', part+childpath)
                c.add('paragraph', {'part': part, 'path': childpath}, text_runs(child, 'w'), flags=flags)
            else:
                if local in ('altChunk', 'fldSimple'):
                    c.issue('dynamic_content_not_resolved', part+childpath)
                walk(child, part, childpath)
    for part in parts:
        root = package.xml(part)
        if part == 'word/document.xml' and root.tag != tag('w', 'document'):
            raise ParseError('invalid_document_root')
        c.units.append({'part': part, 'kind': 'document_part', 'page': None})
        walk(root, part, '')


def pptx(package):
    c = package.c
    part = 'ppt/presentation.xml'
    root, rels = package.xml(part), package.relationships(part)
    slides = root.findall('./p:sldIdLst/p:sldId', NS)
    if len(slides) > c.limits.pages:
        raise ParseError('page_limit_exceeded')
    def shapes(part, slide, notes=False):
        root = package.xml(part)
        c.units.append({'part': part, 'kind': 'notes' if notes else 'slide', 'slide': slide, 'hidden': root.get('show') == '0'})
        tree = root.find('./p:cSld/p:spTree', NS)
        if tree is None:
            raise ParseError('missing_slide_shapes')
        def walk(parent, prefix):
            for index, shape in enumerate(parent, 1):
                path = prefix+'/'+str(index)
                if shape.tag == tag('p', 'grpSp'):
                    walk(shape, path)
                    continue
                shape_id = next((n.get('id') for n in shape.iter() if n.tag == tag('p', 'cNvPr')), None)
                base = {'part': part, 'slide': slide, 'layer': 'notes' if notes else 'slide', 'shape': shape_id, 'path': path}
                if shape.tag == tag('p', 'pic'):
                    c.issue('slide_image_not_extracted', part+path)
                tables = shape.findall('.//a:tbl', NS)
                if tables:
                    for ti, table in enumerate(tables, 1):
                        for ri, row in enumerate(table.findall('a:tr', NS), 1):
                            for ci, cell in enumerate(row.findall('a:tc', NS), 1):
                                for pi, paragraph in enumerate(cell.findall('.//a:p', NS), 1):
                                    c.add('table_cell', {**base, 'table': ti, 'row': ri, 'column': ci, 'paragraph': pi}, text_runs(paragraph, 'a'))
                else:
                    for pi, paragraph in enumerate(shape.findall('.//a:p', NS), 1):
                        c.add('slide_text', {**base, 'paragraph': pi}, text_runs(paragraph, 'a'))
                if shape.tag == tag('p', 'graphicFrame') and not tables:
                    c.issue('slide_graphic_not_extracted', part+path)
        walk(tree, '')
    for index, slide in enumerate(slides, 1):
        target, kind = rels.get(slide.get(tag('r', 'id')), (None, None))
        if kind != 'slide':
            raise ParseError('invalid_slide_relationship')
        shapes(target, index)
        for related, relkind in package.relationships(target).values():
            if relkind == 'notesSlide':
                shapes(related, index, True)


def xlsx(package):
    c = package.c
    part = 'xl/workbook.xml'
    root, rels = package.xml(part), package.relationships(part)
    if root.tag != tag('s', 'workbook'):
        raise ParseError('invalid_workbook_root')
    if len(root.findall('s:sheets/s:sheet', NS)) > c.limits.pages:
        raise ParseError('sheet_limit_exceeded')
    shared = []
    if 'xl/sharedStrings.xml' in package.names:
        shared = [text_runs(si, 's') for si in package.xml('xl/sharedStrings.xml').findall('s:si', NS)]
    for sheet in root.findall('s:sheets/s:sheet', NS):
        target, kind = rels.get(sheet.get(tag('r', 'id')), (None, None))
        if kind != 'worksheet':
            c.issue('non_worksheet_sheet', 'sheet:'+str(sheet.get('sheetId')))
            continue
        xml = package.xml(target)
        sheetid, name = sheet.get('sheetId'), sheet.get('name')
        state = sheet.get('state', 'visible')
        merged = [m.get('ref') for m in xml.findall('s:mergeCells/s:mergeCell', NS)]
        columns = [dict(col.attrib) for col in xml.findall('s:cols/s:col', NS)]
        c.units.append({'kind': 'sheet', 'part': target, 'sheet_id': sheetid, 'name': name, 'state': state, 'merged_ranges': merged, 'columns': columns})
        if state != 'visible':
            c.issue('hidden_sheet', target, False)
        if xml.find('s:drawing', NS) is not None:
            c.issue('worksheet_drawing_not_extracted', target)
        seen = set()
        for row in xml.findall('s:sheetData/s:row', NS):
            for cell in row.findall('s:c', NS):
                coordinate = cell.get('r', '')
                if not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}', coordinate) or coordinate in seen:
                    raise ParseError('invalid_or_duplicate_cell')
                letters = re.match('[A-Z]+', coordinate)[0]
                column = 0
                for letter in letters: column = column*26+ord(letter)-64
                if column > 16384 or int(coordinate[len(letters):]) > 1048576:
                    raise ParseError('cell_outside_sheet_bounds')
                seen.add(coordinate)
                dtype, formula, value = cell.get('t', 'n'), cell.find('s:f', NS), cell.find('s:v', NS)
                cached = value.text if value is not None else None
                flags = []
                if state != 'visible': flags.append('hidden_sheet')
                if row.get('hidden') == '1': flags.append('hidden_row')
                if any(col.get('hidden') == '1' and int(col['min']) <= column <= int(col['max']) for col in columns):
                    flags.append('hidden_column')
                if dtype == 's' and cached is not None:
                    try:
                        index = int(cached)
                        if index < 0: raise IndexError()
                        quote = shared[index]
                    except (IndexError, ValueError):
                        raise ParseError('invalid_shared_string') from None
                elif dtype == 'inlineStr':
                    inline = cell.find('s:is', NS)
                    quote = text_runs(inline, 's') if inline is not None else ''
                else:
                    quote = cached or ''
                formula_text = None
                if formula is not None:
                    formula_text = '='+formula.text if formula.text else None
                    if cached is None:
                        flags.append('formula_uncached')
                        c.issue('formula_uncached', target+'!'+coordinate)
                        quote = formula_text or ''
                    else:
                        flags.append('formula_cache_not_recomputed')
                    if formula.get('t') in ('shared', 'array', 'dataTable'):
                        c.issue('complex_formula_requires_review', target+'!'+coordinate)
                if dtype == 'e':
                    flags.append('cell_error')
                    c.issue('cell_error', target+'!'+coordinate)
                c.add('cell', {'part': target, 'sheet_id': sheetid, 'sheet': name, 'cell': coordinate}, quote,
                    {'cell_type': dtype, 'raw_value': cached, 'value_element_present': value is not None, 'formula': formula_text,
                     'formula_attributes': dict(formula.attrib) if formula is not None else None,
                     'cache_state': 'missing' if formula is not None and cached is None else 'present' if formula is not None else 'not_formula',
                     'recalculated': False, 'style_index': cell.get('s'), 'row_hidden': row.get('hidden') == '1'}, flags)
