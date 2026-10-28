"""Platform binary admission, retaining the original Windows baseline record."""
import json
import sys
from pathlib import Path

from medic.p0.common import ROOT, REVISION, digest


def verify_binary(binary):
    actual = digest(Path(binary).read_bytes())
    legacy = json.loads((ROOT / 'medic/p0/results/baseline.lock.json').read_text(encoding='utf-8'))
    if actual == legacy['binary']['sha256']:
        return
    index = json.loads((ROOT / 'medic/p0/binaries.json').read_text(encoding='utf-8'))
    if index.get('codex_revision') == REVISION and any(
        item.get('platform') == sys.platform and item.get('sha256') == actual
        and item.get('generated_contracts_verified') is True
        for item in index.get('binaries', [])
    ):
        return
    raise ValueError('codex_binary_not_p0_verified')
