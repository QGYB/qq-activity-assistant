# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qq_activity import native_boot as boot


class FakeWindows:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def create(self, *args):
        self.calls.append('create')
        return SimpleNamespace(pid=42, process='process', thread='thread')

    def inject(self, *args):
        self.calls.append('inject')
        if self.fail == 'inject':
            raise OSError('load failed')

    def resume(self, *args):
        self.calls.append('resume')
        if self.fail == 'resume':
            raise OSError('resume failed')

    def terminate(self, *args):
        self.calls.append('terminate')

    def close(self, handle):
        self.calls.append('close-' + handle)


class NativeBootTests(unittest.TestCase):
    def test_success_detaches_without_killing_gui(self):
        api = FakeWindows()
        result = boot._start_injected(api, Path('QQ.exe'), [], {}, Path('own.dll'))
        self.assertEqual(result['pid'], 42)
        self.assertEqual(api.calls, ['create', 'inject', 'resume', 'close-thread', 'close-process'])

    def test_failed_load_or_resume_never_leaves_child_running(self):
        for failure in ('inject', 'resume'):
            with self.subTest(failure=failure):
                api = FakeWindows(failure)
                with self.assertRaises(OSError):
                    boot._start_injected(api, Path('QQ.exe'), [], {}, Path('own.dll'))
                self.assertEqual(api.calls[-3:], ['terminate', 'close-thread', 'close-process'])
                if failure == 'inject':
                    self.assertNotIn('resume', api.calls)

    def test_modified_or_unknown_qq_build_rejected(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            app = root / 'versions' / boot.SUPPORTED_BUILD / 'resources/app'
            with patch.object(boot, '_digest', return_value='changed'):
                with self.assertRaisesRegex(ValueError, '指纹'):
                    boot.validate_build(root, app)
            with self.assertRaisesRegex(ValueError, '版本'):
                boot.validate_build(root, root / 'versions/new/resources/app')

    def test_exact_manifest_paths_and_inherited_bootstrap_cleanup(self):
        with TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            app = base / 'app'
            app.mkdir()
            shadow, script = base / 'shadow.json', base / 'start.cjs'
            shadow.write_text(json.dumps({'main': './qq-activity-start.cjs'}))
            script.write_text('')
            env = boot.build_environment(app, base / 'QQNT.dll', shadow, script,
                {'Path': 'keep', 'NODE_OPTIONS': 'bad', 'electron_run_as_node': '1', 'NAPCAT_INJECT_PATH': 'bad'})
            self.assertEqual(env['Path'], 'keep')
            self.assertNotIn('NODE_OPTIONS', env)
            self.assertNotIn('NAPCAT_INJECT_PATH', env)
            self.assertNotIn('electron_run_as_node', env)
            self.assertEqual(env['QQ_ACTIVITY_VIRTUAL_BOOTSTRAP'], str(app / 'qq-activity-start.cjs'))
            for main in ('../outside.cjs', './package.json'):
                shadow.write_text(json.dumps({'main': main}))
                with self.assertRaises(ValueError):
                    boot.build_environment(app, base / 'QQNT.dll', shadow, script)

    def test_environment_nul_rejected_and_block_terminated(self):
        self.assertEqual(boot._environment_block({'b': '2', 'A': '1'}), 'A=1\x00b=2\x00\x00')
        with self.assertRaises(ValueError):
            boot._environment_block({'X': 'value\x00INJECTED=1'})


if __name__ == '__main__':
    unittest.main()
