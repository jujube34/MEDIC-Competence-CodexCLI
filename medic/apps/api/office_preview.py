"""Derived preview cache, scoped to a material and release; not scoring evidence."""
import json
import os
from pathlib import Path
import tempfile

from medic.apps.api.database import AppError
from medic.apps.api.uploads import lock_slot
from medic.ingestion.office import convert_file,ENGINE_MANIFEST_SHA256,MAX_PDF_BYTES
from medic.ingestion.contracts import ParseError
from medic.p0.common import digest
from medic.runtime import build


def office_document(vault,snapshot):
    identity=build.verify(build.VERSION)
    root=vault.root/'office-previews'/build.VERSION/identity['manifest_sha256']/snapshot['scope']
    destination=root/snapshot['snapshot_id']
    if not destination.resolve().is_relative_to(vault.root) or destination.is_symlink():
        raise ParseError('office_cache_integrity_failure')
    root.mkdir(parents=True,exist_ok=True)
    handle=lock_slot(root/(snapshot['snapshot_id']+'.lock'))
    if handle is None:raise AppError('preview_busy',429)
    try:
        if not destination.exists():
            info,pdf=convert_file(vault._blob(snapshot['scope'],snapshot['source_sha256']),kind=snapshot['format'],source_sha256=snapshot['source_sha256'])
            info={**info,'snapshot_id':snapshot['snapshot_id'],'scope':snapshot['scope'],'execution_build':identity}
            # Publish complete cache atomically; failures leave no visible entry.
            with tempfile.TemporaryDirectory(prefix='office-cache-',dir=root) as temp:
                staged=Path(temp)/'complete';staged.mkdir()
                (staged/'document.pdf').write_bytes(pdf)
                (staged/'manifest.json').write_text(json.dumps({'payload':info,'sha256':digest(info)}),encoding='utf-8')
                os.rename(staged,destination)
        metadata=destination/'manifest.json';pdf_path=destination/'document.pdf'
        if metadata.is_symlink() or pdf_path.is_symlink() or metadata.stat().st_size>65536 or pdf_path.stat().st_size>MAX_PDF_BYTES:
            raise ParseError('office_cache_integrity_failure')
        envelope=json.loads(metadata.read_text(encoding='utf-8'));info=envelope['payload']
        if (digest(info)!=envelope['sha256'] or info['source_sha256']!=snapshot['source_sha256'] or
                info['snapshot_id']!=snapshot['snapshot_id'] or info['scope']!=snapshot['scope'] or
                info['execution_build']!=identity or info['engine_manifest_sha256']!=ENGINE_MANIFEST_SHA256 or digest(pdf_path.read_bytes())!=info['pdf_sha256']):
            raise ParseError('office_cache_integrity_failure')
        return pdf_path,info
    except (OSError,KeyError,ValueError) as error:
        if isinstance(error,(ParseError,AppError)):raise
        raise ParseError('office_cache_integrity_failure') from None
    finally:handle.close()
