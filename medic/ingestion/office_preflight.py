"""Reject active/external OOXML before handing a document to an office engine.

This is an admission policy, not an OS or network sandbox. Original files are
never rewritten to make them pass; extraction remains independent of previews.
"""
import posixpath
import re
from urllib.parse import urlsplit,unquote

from medic.ingestion.contracts import Collector, ParseError, ParseLimits
from medic.ingestion.ooxml import Package

SAFE_FUNCTIONS = set('SUM AVERAGE MIN MAX COUNT COUNTA COUNTBLANK COUNTIF COUNTIFS SUMIF SUMIFS AVERAGEIF AVERAGEIFS IF IFS IFERROR IFNA AND OR NOT TRUE FALSE ABS ROUND ROUNDUP ROUNDDOWN INT MOD SQRT POWER PRODUCT SUMPRODUCT SUBTOTAL AGGREGATE MEDIAN MODE STDEV STDEV.S STDEV.P VAR VAR.S VAR.P LEN LEFT RIGHT MID TRIM CLEAN CONCAT CONCATENATE TEXTJOIN UPPER LOWER PROPER SUBSTITUTE REPLACE FIND SEARCH EXACT TEXT VALUE DATE YEAR MONTH DAY DAYS EDATE EOMONTH TODAY NOW TIME HOUR MINUTE SECOND VLOOKUP HLOOKUP XLOOKUP LOOKUP INDEX MATCH XMATCH CHOOSE ROW ROWS COLUMN COLUMNS ADDRESS TRANSPOSE SORT SORTBY FILTER UNIQUE SEQUENCE LET'.split())


def check_office(data, kind, limits=None):
    limits=ParseLimits.model_validate(limits or {})
    if kind not in ('docx','pptx','xlsx'): raise ParseError('office_format_not_supported')
    if len(data)>limits.input_bytes: raise ParseError('input_size_exceeded')
    collector=Collector(data,kind,limits)
    try:
        package=Package(data,collector)
        try:
            required={'docx':'word/document.xml','pptx':'ppt/presentation.xml','xlsx':'xl/workbook.xml'}[kind]
            if required not in package.names: raise ParseError('invalid_office_package')
            instructions=[]
            for name in sorted(package.names):
                lower=name.lower()
                if any(marker in lower for marker in ('vbaproject','/embeddings/','/activex/','/externallinks/','/querytables/','connections.xml','customui/')):
                    raise ParseError('office_active_content_rejected')
                if lower.endswith(('.svg','.html','.htm','.js','.vbs','.exe','.dll','.com','.ps1','.bat','.cmd','.lnk','.url')):
                    raise ParseError('office_active_content_rejected')
                if not lower.endswith(('.xml','.rels','.vml')): continue
                root=package.xml(name)
                if lower.endswith('.rels'):
                    base=posixpath.dirname(posixpath.dirname(name))
                    ids=set()
                    for rel in root:
                        target=unquote(rel.get('Target',''));mode=rel.get('TargetMode','Internal')
                        if rel.get('Id') in ids: raise ParseError('invalid_relationship_target')
                        ids.add(rel.get('Id'))
                        if mode=='External':
                            # Hyperlinks are rendered as inert pixels; linked resources
                            # and templates are not admitted, even with NO_UPDATE.
                            if rel.get('Type','').rsplit('/',1)[-1]!='hyperlink' or urlsplit(target).scheme.lower() not in ('https','http','mailto'):
                                raise ParseError('office_external_content_rejected')
                        elif mode!='Internal' or any(x in target for x in ('\\',':','%','\x00','\n','\r')):
                            raise ParseError('office_external_content_rejected')
                        else:
                            resolved=posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(base,target))
                            if resolved not in package.names: raise ParseError('invalid_relationship_target')
                for element in root.iter():
                    local=element.tag.rsplit('}',1)[-1].lower()
                    if local in ('oleobject','object','altchunk','mailmerge','attachedtemplate','ddelink','ddeitem','externaldata','externalreference','connection','webpr','datafeedpr','control'):
                        raise ParseError('office_active_content_rejected')
                    if local=='override' and any(x in element.get('ContentType','').lower() for x in ('macroenabled','vbaproject','oleobject')):
                        raise ParseError('office_active_content_rejected')
                    if local=='instrtext': instructions.append(element.text or '')
                    if local=='fldsimple': instructions.append(next((v for k,v in element.attrib.items() if k.rsplit('}',1)[-1]=='instr'),''))
                    if kind=='xlsx' and local in ('f','definedname','formula','formula1','formula2'):
                        formula=element.text or ''
                        if any(x in formula for x in ('[',']','|','\\')) or re.search(r'(?:https?|file|ftp|smb):',formula,re.I): raise ParseError('office_formula_not_supported')
                        for function in re.findall(r'([\w.]+)\s*\(',formula):
                            if function.upper().removeprefix('_XLFN.').removeprefix('_XLWS.') not in SAFE_FUNCTIONS:
                                raise ParseError('office_formula_not_supported')
            # Field instructions can be split across runs. Permit only inert page
            # counters; reject unknown/dynamic fields rather than resolving them.
            if instructions and re.sub(r'\b(?:PAGE|NUMPAGES)\b|\\\*\s*(?:MERGEFORMAT|Arabic)|\s+', '', ''.join(instructions),flags=re.I):
                raise ParseError('office_dynamic_field_rejected')
            return {'external_content_fetched':False,'macros_executed':False}
        finally: package.zip.close()
    except ParseError: raise
    except Exception: raise ParseError('invalid_office_package') from None
