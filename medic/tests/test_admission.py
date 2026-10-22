import copy
import tempfile
import unittest
from pathlib import Path

from medic.p0.common import digest, save
from medic.p0.provider import Config
from medic.prompts import load_bundles
from medic.runtime.admission import verify_execution
from medic.tests.test_runtime import fixture


class ExecutionAdmission(unittest.TestCase):
    def test_versions_and_actual_endpoint_must_match_verified_profile(self):
        source = fixture()
        provider = Config('https://synthetic.invalid/v1', 'synthetic-token')
        profile = {'provider_profile_version': source['versions']['provider_profile_version'],
                   'model': provider.model, 'upstream_endpoint_sha256': digest(provider.base_url)}
        baseline = {'codex_revision': source['versions']['codex_revision']}
        verify_execution(source, provider, profile=profile, baseline=baseline)
        for field in ('provider_profile_version', 'codex_revision', 'prompt_bundle_version', 'runtime_version'):
            changed = copy.deepcopy(source)
            changed['versions'][field] = 'unverified'
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_execution(changed, provider, profile=profile, baseline=baseline)
        for changed in (Config('https://other.invalid/v1', 'synthetic-token'),
                        Config(provider.base_url, 'synthetic-token', 'unverified-model')):
            with self.assertRaisesRegex(ValueError, 'provider_does_not_match'):
                verify_execution(source, changed, profile=profile, baseline=baseline)

    def test_prompt_content_cannot_change_under_released_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundle = {'version': 'synthetic-prompt-v1', 'common': 'Original instruction'}
            save(root / 'roles.json', bundle)
            save(root / 'manifest.json', {bundle['version']: {'file': 'roles.json', 'sha256': digest(bundle)}})
            self.assertEqual(load_bundles(root)[bundle['version']], bundle)
            bundle['common'] = 'Changed without version increment'
            save(root / 'roles.json', bundle)
            with self.assertRaisesRegex(ValueError, 'released_prompt_bundle_changed'):
                load_bundles(root)


if __name__ == '__main__':
    unittest.main()
