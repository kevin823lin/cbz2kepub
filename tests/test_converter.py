import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from xml.sax.saxutils import unescape
from unittest.mock import patch
import zipfile

from cbz2kepub import converter


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'input' / 'Demo' / 'compressed'
        self.source.mkdir(parents=True)
        self.config_path = self.root / 'config.toml'
        self.config_text = (converter.ROOT / 'config.example.toml').read_text(encoding='utf-8').replace('範例漫畫 A', 'Demo')
        self.config_path.write_text(self.config_text, encoding='utf-8')
        self.add_cbz('24.1 - B.cbz')
        self.add_cbz('24.05 - A.cbz')

    def tearDown(self):
        self.temporary.cleanup()

    def add_cbz(self, name, entries=None):
        path = self.source / name
        with zipfile.ZipFile(path, 'w') as archive:
            for filename, data in (entries or {'1.jpg': b'fake-image', '2.jpg': b'fake-image'}).items():
                archive.writestr(filename, data)
        return path

    def load(self, text=None):
        if text is not None:
            self.config_path.write_text(text, encoding='utf-8')
        return converter.load_config(self.config_path)

    def test_config_paths_and_decimal_order_are_independent_of_cwd(self):
        with patch('os.getcwd', return_value=str(converter.ROOT)):
            result = self.load()
        self.assertEqual(result['paths']['output_dir'], self.root / 'output')
        self.assertEqual(result['paths']['work_root'], self.root / '.cbz2kepub-work')
        self.assertEqual([p.name for p in result['jobs'][0]['sources']], ['24.05 - A.cbz', '24.1 - B.cbz'])
        self.assertEqual(result['jobs'][0]['image_count'], 4)

    def test_unknown_key_bad_type_and_missing_sources_fail_before_conversion(self):
        for text in (self.config_text.replace('cropping =', 'croping ='),
                     self.config_text.replace('spreadshift = false', 'spreadshift = "false"'),
                     self.config_text.replace('"Demo"', '"Missing"'),
                     self.config_text.replace('"Demo"', '"../Demo"')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)

    def test_empty_list_does_not_require_input_folder(self):
        text = self.config_text.replace('    { title =', '    # { title =')
        self.assertEqual(self.load(text)['jobs'], [])

    def test_duplicate_or_volume_overlapping_titles_are_rejected(self):
        for title in ('Demo', 'Demo 1'):
            text = self.config_text.replace('    # { title = "範例漫畫 B", spreadshift = true },',
                                            f'    {{ title = "{title}", spreadshift = true }},')
            with self.subTest(title=title), self.assertRaises(ValueError):
                self.load(text)

    def test_metadata_types_and_invalid_chapter_names(self):
        (self.source / 'info.json').write_text(json.dumps({'author': []}), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.load()
        (self.source / 'info.json').unlink()
        self.add_cbz('chapter.cbz')
        with self.assertRaises(ValueError):
            self.load()

    def test_comic_info_matches_native_kcc_text_reading_and_omits_empty_values(self):
        job = self.load()['jobs'][0]
        job['metadata'] = {'title': 'ignored', 'author': 'B, A & C', 'description': 'literal &amp; <字>\n'}
        converter.write_comic_info(job, self.root)
        root = ET.parse(self.root / 'ComicInfo.xml').getroot()
        self.assertEqual(root.findtext('Series'), 'Demo')
        self.assertEqual(root.findtext('Title'), 'Demo')
        self.assertEqual(unescape(root.findtext('Writer')), 'B, A & C')
        self.assertEqual(unescape(root.findtext('Summary')), 'literal &amp; <字>\n')
        job['metadata'] = {'description': ''}
        converter.write_comic_info(job, self.root)
        self.assertIsNone(ET.parse(self.root / 'ComicInfo.xml').getroot().find('Summary'))
        job['metadata'] = {}
        converter.write_comic_info(job, self.root)
        self.assertIsNone(ET.parse(self.root / 'ComicInfo.xml').getroot().find('Summary'))

    def test_archive_traversal_empty_image_and_encryption_boundary(self):
        for entries in ({'../page.jpg': b'no'}, {'1.jpg': b''}, {'info.txt': b'no'},
                        {'page.jpg:stream': b'no'}, {'1.JPG': b'a', '1.jpg': b'b'}):
            path = self.add_cbz('99 - Bad.cbz', entries)
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                converter.inspect_source(path)

    def test_windows_command_length_fails_without_silent_batching(self):
        config = self.load()
        with self.assertRaises(ValueError):
            converter.build_command(config['jobs'][0], config['conversion'], [Path('s/' + 'x' * 1000)] * 40, self.root)

    def test_cli_uses_native_entrypoint_and_conditional_fusion(self):
        config = self.load()
        job = config['jobs'][0]
        command = converter.build_command(job, config['conversion'], [Path('s/a.cbz')], self.root)
        self.assertEqual(command[1], str(converter.ROOT / 'kcc' / 'kcc-c2e.py'))
        self.assertNotIn('--filefusion', command)
        self.assertNotIn('--spreadshift', command)
        self.assertIn('--forcecolor', command)
        self.assertIn('--manga-style', command)
        job['spreadshift'] = True
        job['metadata']['author'] = 'Author'
        command = converter.build_command(job, config['conversion'], [Path('s/a.cbz'), Path('s/b.cbz')], self.root)
        self.assertIn('--filefusion', command)
        self.assertIn('--spreadshift', command)
        self.assertEqual(command[command.index('--author') + 1], 'Author')

    def test_kcc_output_is_displayed_and_saved_as_it_arrives_on_failure(self):
        command = [sys.executable, '-u', '-c',
                   "import sys; print('進度'); print('錯誤', file=sys.stderr); sys.exit(3)"]
        displayed = []

        def show(line, **kwargs):
            self.assertIn(line, (self.root / 'kcc.log').read_text(encoding='utf-8'))
            displayed.append(line)

        with patch('builtins.print', side_effect=show):
            with self.assertRaises(subprocess.CalledProcessError) as error:
                converter.run_kcc(command, self.root, dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertEqual(error.exception.returncode, 3)
        self.assertEqual(''.join(displayed), '進度\n錯誤\n')

    def test_output_volumes_are_numeric_and_complete(self):
        folder = self.root / 'volumes'
        folder.mkdir()
        for index in range(1, 12):
            (folder / f'work [fused] {index}.kepub.epub').touch()
        result = converter.ordered_outputs(folder)
        self.assertTrue(result[1].name.endswith(' 2.kepub.epub'))
        (folder / 'work [fused] 2.kepub.epub').unlink()
        with self.assertRaises(ValueError):
            converter.ordered_outputs(folder)

    def test_source_id_and_chapter_coverage(self):
        job = self.load()['jobs'][0]
        first = dict(has_cover_page=True, chapter_titles=['24.05 - A'], body_images=['Images/kcc-0001-kcc-x.jpg'])
        second = dict(has_cover_page=True, chapter_titles=['24.05 - A', '24.1 - B'],
                      body_images=[f'Images/kcc-{n:04}-kcc-x.jpg' for n in range(2, 5)])
        converter.validate_book(job, [first, second])
        second['body_images'][0], second['body_images'][1] = second['body_images'][1], second['body_images'][0]
        with self.assertRaisesRegex(ValueError, '頁序'):
            converter.validate_book(job, [first, second])
        second['body_images'][0], second['body_images'][1] = second['body_images'][1], second['body_images'][0]
        second['body_images'].pop()
        with self.assertRaises(ValueError):
            converter.validate_book(job, [first, second])

    def test_output_lock_rejects_parallel_work_and_releases_after_failure(self):
        output = self.root / 'output'
        with self.assertRaisesRegex(RuntimeError, 'test'):
            with converter.output_lock(output):
                with self.assertRaises(ValueError):
                    with converter.output_lock(output):
                        self.fail('second lock acquired')
                raise RuntimeError('test')
        self.assertFalse((output / '.cbz2kepub.lock').exists())

    def space_data(self, config):
        job = config['jobs'][0]
        return dict(version=1, work=str(config['paths']['work_root']), target=[100, 150],
                    target_bytes=1000, expanded=44, cover_bytes=0,
                    images=dict(page_bytes=10, originals=[11] * 4, predictions=[10] * 4),
                    sources=[dict(path=str(path), expanded=22) for path in job['sources']])

    def test_staging_link_fallback_is_budgeted_before_copy_and_before_old_deletion(self):
        config = self.load()
        config['publication']['keep_backup'] = False
        with patch.object(converter, 'prepare_space', return_value=self.space_data(config)), \
                patch.object(converter.os, 'link', side_effect=OSError('no hardlinks')), \
                patch.object(converter, 'preflight_batch', side_effect=OSError('no capacity')) as check, \
                patch.object(converter.shutil, 'copy2') as copy, \
                patch.object(converter, 'discard_owned_outputs') as discard, \
                patch.object(converter, 'run_kcc') as run:
            with self.assertRaisesRegex(OSError, 'no capacity'):
                converter.convert_comic(config['jobs'][0], config)
        self.assertEqual(check.call_args.kwargs['copied_override'],
                         sum(path.stat().st_size for path in config['jobs'][0]['sources']))
        copy.assert_not_called()
        discard.assert_not_called()
        run.assert_not_called()

    def test_no_backup_rechecks_actual_free_before_staging_copy(self):
        config = self.load()
        config['publication']['keep_backup'] = False
        with patch.object(converter, 'prepare_space', return_value=self.space_data(config)), \
                patch.object(converter.os, 'link', side_effect=OSError('no hardlinks')), \
                patch.object(converter, 'preflight_batch'), \
                patch.object(converter, 'discard_owned_outputs') as discard, \
                patch.object(converter, 'require_space', side_effect=OSError('still full')), \
                patch.object(converter.shutil, 'copy2') as copy, \
                patch.object(converter, 'run_kcc') as run:
            with self.assertRaisesRegex(OSError, 'still full'):
                converter.convert_comic(config['jobs'][0], config)
        discard.assert_called_once()
        copy.assert_not_called()
        run.assert_not_called()

    def test_publication_config_defaults_to_backup_and_rejects_non_boolean(self):
        self.assertTrue(self.load()['publication']['keep_backup'])
        self.assertTrue(self.load(self.config_text.split('[publication]')[0])['publication']['keep_backup'])
        with self.assertRaises(ValueError):
            self.load(self.config_text.replace('keep_backup = true', 'keep_backup = "false"'))

    def test_source_stats_include_extracted_non_image_files(self):
        path = self.add_cbz('99 - Metadata.cbz', {'1.jpg': b'123', 'info.txt': b'45678'})
        self.assertEqual(converter.inspect_source(path), dict(image_count=1, expanded=8, comic_info_bytes=0))

    def test_initial_batch_probes_hardlinks_instead_of_assuming_same_drive_support(self):
        config = self.load()
        with patch.object(converter, 'prepare_space', return_value=self.space_data(config)), \
                patch.object(converter.os, 'link', side_effect=OSError('filesystem has no hardlinks')), \
                patch.object(converter, 'require_space', return_value=1_000_000), \
                patch.object(converter, 'SpacePlan') as plan:
            plan.return_value.initial_budget.return_value = (1000, 300)
            converter.preflight_batch(config, config['jobs'])
        self.assertEqual(plan.return_value.initial_budget.call_args.args[0],
                         sum(path.stat().st_size for path in config['jobs'][0]['sources']))
        self.assertEqual(list(config['paths']['work_root'].iterdir()), [])

    def test_large_metadata_and_generated_xml_have_separate_byte_budgets(self):
        config = self.load()
        job = config['jobs'][0]
        job['metadata']['description'] = '&' * 4000
        calibration = dict(page_bytes=100, sample_bytes=80, samples=1,
                           originals=[11] * 4, predictions=[100] * 4)
        with patch.object(converter, 'calibrate_images', return_value=calibration):
            data = converter.prepare_space(job, config, converter.TARGET_MIB)
        self.assertGreaterEqual(data['metadata_bytes'], 5 * 4000)
        converter.write_comic_info(job, self.root)
        self.assertEqual(data['sidecar_bytes'], (self.root / 'ComicInfo.xml').stat().st_size)


if __name__ == '__main__':
    unittest.main()
