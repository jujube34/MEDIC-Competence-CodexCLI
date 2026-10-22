"""Local ingestion CLI. Prints IDs/counts only, never source text or exceptions."""
import argparse
import json
from pathlib import Path

from medic.ingestion.vault import Vault
from medic.p0.common import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file')
    parser.add_argument('--tenant', required=True)
    parser.add_argument('--employee', required=True)
    parser.add_argument('--period', required=True)
    parser.add_argument('--owner-claim', default='unverified_submission')
    parser.add_argument('--root', default=str(ROOT/'medic/var/materials'))
    args = parser.parse_args()
    target = Path(args.root).resolve()
    if not any(target.is_relative_to((ROOT/'medic'/folder).resolve()) for folder in ('var','private','artifacts')):
        parser.error('material vault must be inside an ignored private directory')
    try:
        result = Vault(target).ingest(args.tenant, args.employee, args.file, period=args.period, owner_claim=args.owner_claim)
        extraction = result['extraction']
        print(json.dumps({'snapshot_id': result['snapshot_id'], 'source_sha256': result['source_sha256'],
                          'state': extraction['state'], 'blocks': len(extraction['blocks']),
                          'issue_codes': sorted({i['code'] for i in extraction['issues']})}))
        return 0 if extraction['state'] in ('ready','needs_review','empty') else 2
    except Exception:
        print(json.dumps({'state': 'failed', 'error': 'material_import_failed'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
