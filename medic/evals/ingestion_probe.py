"""Read local source cases and publish only hashes, counts and fixed issue codes."""
import argparse
import json
from pathlib import Path

from medic.ingestion.vault import Vault
from medic.p0.common import ROOT, digest, now, save


def verify(config, root, summary):
    root = Path(root).resolve()
    if not root.is_relative_to((ROOT/'medic/artifacts').resolve()):
        raise ValueError('probe_requires_private_artifacts')
    vault = Vault(root/'vault')
    cases = json.loads(Path(config).read_text(encoding='utf-8'))
    records = []
    for case in cases:
        path = Path(case['path'])
        original = digest(path.read_bytes())
        result = vault.ingest('private-ingestion-audit', 'non-employee-reference-materials', path,
                              period='parser-validation', owner_claim='reference_document_not_employee_evidence')
        extracted = result['extraction']
        records.append({'format': extracted['format'], 'parser_version': extracted['parser_version'],
            'engine': extracted['engine'], 'runtime_version':result['execution_build']['runtime_version'],
            'source_sha256': original,
            'source_unchanged': digest(path.read_bytes()) == original == result['source_sha256'],
            'snapshot_integrity_verified': bool(vault.get('private-ingestion-audit','non-employee-reference-materials',result['snapshot_id'])),
            'state': extracted['state'], 'unit_count': len(extracted['units']), 'block_count': len(extracted['blocks']),
            'issue_codes': sorted({i['code'] for i in extracted['issues']}),
            'parsed_snapshot_sha256': digest(extracted)})
    checks = {'four_formats_covered': {r['format'] for r in records} == {'pdf','docx','pptx','xlsx'},
              'sources_unchanged': all(r['source_unchanged'] for r in records),
              'snapshots_verified': all(r['snapshot_integrity_verified'] for r in records),
              'no_technical_parse_failure': all(r['state'] != 'failed' for r in records)}
    result = {'created_at':now(), 'status':'pass' if all(checks.values()) else 'fail', 'checks':checks,
              'cases':records, 'quality_claim':False, 'employee_assessment_performed':False,
              'scope':'local_read_only_reference_documents_and_private_ingestion_snapshots'}
    save(summary,result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True); parser.add_argument('--root',required=True); parser.add_argument('--summary',required=True)
    args=parser.parse_args()
    result=verify(args.config,args.root,args.summary)
    print(json.dumps({'status':result['status'],'checks':result['checks'],'states':[c['state'] for c in result['cases']]}))
    raise SystemExit(0 if result['status']=='pass' else 1)
