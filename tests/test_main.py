from contextlib import chdir, redirect_stdout
from io import StringIO
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cbz2kepub import converter


class EntryPointTests(unittest.TestCase):
    def test_main_runs_from_another_directory_with_a_relative_config(self):
        with tempfile.TemporaryDirectory(prefix='c2k 入口-') as folder:
            config = Path(folder) / 'batch.toml'
            text = (converter.ROOT / 'config.example.toml').read_text(encoding='utf-8')
            config.write_text(text.replace('    { title =', '    # { title ='), encoding='utf-8')
            result = subprocess.run([sys.executable, '-B', str(converter.ROOT / 'main.py'),
                                     '--config', config.name], cwd=folder, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_main_default_config_stays_at_project_root(self):
        with tempfile.TemporaryDirectory() as folder, chdir(folder), redirect_stdout(StringIO()), \
                patch.object(sys, 'argv', ['main.py']), \
                patch.object(converter, 'load_config', return_value={'jobs': []}) as load:
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(converter.ROOT / 'main.py'), run_name='__main__')
        self.assertEqual(result.exception.code, 0)
        load.assert_called_once_with(converter.ROOT / 'config.toml')


if __name__ == '__main__':
    unittest.main()
