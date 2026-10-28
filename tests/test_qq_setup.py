# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
import json
import tempfile
import unittest
import shutil
from unittest.mock import patch
from pathlib import Path

from qq_activity.qq_setup import install_receiver, restore_receiver


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = self.root / 'versions' / '9.9.35-52892' / 'resources' / 'app'
        self.app.mkdir(parents=True)
        self.package = self.app / 'package.json'
        self.original = b'{"name":"qq-chat","main":"./application.asar/app_launcher/index.js","version":"9.9.35-52892"}\r\n'
        self.package.write_bytes(self.original)
        (self.root / 'QQ.exe').write_bytes(b'fixture')
        self.module = self.app.parent.parent / 'QQNT.dll'
        self.module.write_bytes(b'fixture')
        self.validation = patch('qq_activity.native_boot.validate_build', return_value=(self.root / 'QQ.exe', self.module))
        self.validation.start()
        # Installation tests need known bytes, not a prebuilt executable DLL.
        self.assets = self.root / 'test-assets'
        (self.assets / 'native').mkdir(parents=True)
        (self.assets / 'receiver').mkdir()
        (self.assets / 'native/qq_activity_boot.dll').write_bytes(b'fixture-only-not-a-DLL')
        source = Path(__file__).resolve().parents[1] / 'receiver/preload.cjs'
        shutil.copyfile(source, self.assets / 'receiver/preload.cjs')
        self.asset_patch = patch('qq_activity.qq_setup._assets', return_value=self.assets)
        self.asset_patch.start()

    def tearDown(self):
        self.validation.stop()
        self.asset_patch.stop()
        self.tmp.cleanup()

    def test_install_is_idempotent_and_restores_exact_bytes(self):
        install_receiver(self.root, self.root / 'data')
        first = self.package.read_bytes()
        self.assertEqual(first, self.original)
        config = json.loads((self.root / 'data/receiver/launch.json').read_text(encoding='utf-8'))
        shadow = json.loads(Path(config['shadow_package']).read_text(encoding='utf-8'))
        self.assertEqual(shadow['main'], './qq-activity-entry.cjs')
        install_receiver(self.root, self.root / 'data')
        self.assertEqual(self.package.read_bytes(), first)
        restore_receiver(self.root, self.root / 'data')
        self.assertEqual(self.package.read_bytes(), self.original)

    def test_restore_does_not_destroy_unrelated_manifest_changes(self):
        install_receiver(self.root, self.root / 'data')
        data = json.loads(self.package.read_text())
        data['extra'] = 'another change'
        self.package.write_text(json.dumps(data), encoding='utf-8')
        restore_receiver(self.root, self.root / 'data')
        self.assertEqual(json.loads(self.package.read_text())['extra'], 'another change')


if __name__ == '__main__':
    unittest.main()
