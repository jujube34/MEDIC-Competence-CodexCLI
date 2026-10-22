"""Verify the immutable file-to-evidence-to-real-assessment chain."""
import argparse
import json
from pathlib import Path

from medic.domain.contracts import AssessmentInput
from medic.evals.verify_run import verify
from medic.ingestion.vault import Vault
from medic.p0.common import save


def verify_chain(folder, output, *, vault_folder=None):
    folder = Path(folder)
    summary = verify(folder, folder/'runtime-verification.json')
    run = json.loads((folder/'run.json').read_text(encoding='utf-8'))
    source = AssessmentInput.model_validate(run['source'])
    vault = Vault(vault_folder or folder/'vault')
    checks = []
    for material in source.materials:
        if material.snapshot_ref is None:
            checks.append(False)
            continue
        saved = vault.get(source.tenant_id, source.employee_ref, material.snapshot_ref)
        rebuilt = vault.material(source.tenant_id, source.employee_ref, material.snapshot_ref)
        checks.append(rebuilt.model_dump() == material.model_dump()
                      and saved['extraction']['parser_version'] == source.versions.parser_version
                      and saved['period'] == source.period
                      and saved['execution_build'] == run['execution_build'])
    summary['checks']['frozen_file_extraction_matches_assessed_materials'] = bool(checks) and all(checks)
    summary['status'] = 'pass' if all(summary['checks'].values()) else 'fail'
    summary['scope'] = 'synthetic_docx_to_frozen_evidence_to_real_six_role_assessment'
    summary['ingestion'] = {'materials':len(checks),'spans':sum(len(m.spans) for m in source.materials),
                            'source_hashes':[m.source_sha256 for m in source.materials]}
    save(output,summary)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('folder'); parser.add_argument('output')
    args=parser.parse_args(); result=verify_chain(args.folder,args.output)
    print(json.dumps({'status':result['status'],'checks':result['checks']}))
    raise SystemExit(0 if result['status']=='pass' else 1)
