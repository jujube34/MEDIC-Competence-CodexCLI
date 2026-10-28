import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from medic.p0.binaries import verify_binary
from medic.p0.common import REVISION, digest


class BinaryAdmission(unittest.TestCase):
    def test_platform_revision_and_contract_verification_are_all_required(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); target = root/'medic/p0'; (target/'results').mkdir(parents=True)
            binary=root/'codex';binary.write_bytes(b'synthetic-binary-not-executable')
            (target/'results/baseline.lock.json').write_text(json.dumps({'binary':{'sha256':'legacy'}}))
            item={'platform':sys.platform,'sha256':digest(binary.read_bytes()),'generated_contracts_verified':True}
            with patch('medic.p0.binaries.ROOT', root):
                for change in ({'platform':'other-os'},{'sha256':'wrong'},{'generated_contracts_verified':False}):
                    (target/'binaries.json').write_text(json.dumps({'codex_revision':REVISION,'binaries':[item|change]}))
                    with self.assertRaisesRegex(ValueError,'not_p0_verified'):verify_binary(binary)
                (target/'binaries.json').write_text(json.dumps({'codex_revision':'unverified-revision','binaries':[item]}))
                with self.assertRaises(ValueError):verify_binary(binary)
                (target/'binaries.json').write_text(json.dumps({'codex_revision':REVISION,'binaries':[item]}))
                verify_binary(binary)


if __name__=='__main__':unittest.main()
