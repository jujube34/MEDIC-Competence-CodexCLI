"""Load released prompt bundles without silently changing an existing version."""
import json
from pathlib import Path

from medic.p0.common import digest


def load_bundles(root=None):
    root = Path(root) if root else Path(__file__).parent
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    bundles = {}
    for version, entry in manifest.items():
        filename = entry['file']
        if Path(filename).name != filename:
            raise ValueError('invalid_prompt_manifest_path')
        bundle = json.loads((root / filename).read_text(encoding='utf-8'))
        if bundle['version'] != version or digest(bundle) != entry['sha256']:
            raise ValueError('released_prompt_bundle_changed')
        bundles[version] = bundle
    return bundles
