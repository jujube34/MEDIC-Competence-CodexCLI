"""Bind declared execution versions to locally verified execution assets."""
import json

from medic.domain.contracts import AssessmentInput
from medic.p0.common import ROOT, digest
from medic.prompts import load_bundles
from medic.runtime import build


def verify_execution(source, provider, *, profile=None, baseline=None):
    source = AssessmentInput.model_validate(source)
    build.verify(source.versions.runtime_version)
    profile = profile or json.loads((ROOT / 'medic/p0/results/provider-profile.json').read_text(encoding='utf-8'))
    baseline = baseline or json.loads((ROOT / 'medic/p0/results/baseline.lock.json').read_text(encoding='utf-8'))
    if source.versions.codex_revision != baseline['codex_revision']:
        raise ValueError('declared_codex_revision_not_verified')
    if source.versions.prompt_bundle_version not in load_bundles():
        raise ValueError('unknown_frozen_prompt_bundle')
    if source.versions.provider_profile_version != profile['provider_profile_version']:
        raise ValueError('unknown_frozen_provider_profile')
    if provider.model != profile['model'] or digest(provider.base_url) != profile['upstream_endpoint_sha256']:
        raise ValueError('provider_does_not_match_verified_profile')
