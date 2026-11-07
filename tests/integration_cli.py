"""Explicit integration suite: python -m unittest discover -s tests -p integration_cli.py -v."""

import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

from cbz2kepub.epub_outputs import inspect_epub
from cbz2kepub import converter


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='c2k-integration-')
        self.root = Path(self.temporary.name)
        self.source = self.root / 'input' / '測試漫畫' / 'compressed'
        self.source.mkdir(parents=True)
        self.chapters = ['24.05 - 第1話', '24.1 - 第2話', '24.2 - ' + '長章節名稱' * 7]
        self.hashes = {}
        for index, chapter in enumerate(self.chapters):
            path = self.source / f'{chapter}.cbz'
            with zipfile.ZipFile(path, 'w') as archive:
                for page in (1, 2, 10):
                    data = random.Random(index * 100 + page).randbytes(600 * 800 * 3)
                    buffer = BytesIO()
                    Image.frombytes('RGB', (600, 800), data).save(buffer, 'PNG')
                    archive.writestr(f'{page}.png', buffer.getvalue())
            self.hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        Image.new('RGB', (600, 800), (255, 0, 255)).save(self.source / 'cover.jpg')
        self.author = '測試作者 B, 作者 A'
        self.description = '中文簡介 & < > &amp;\n'
        (self.source / 'info.json').write_text(json.dumps({'title': '不同來源名', 'author': self.author, 'description': self.description}), encoding='utf-8')
        self.config_path = self.root / 'config.toml'
        text = (converter.ROOT / 'config.example.toml').read_text(encoding='utf-8').replace(
            '範例漫畫 A', '測試漫畫').replace('chapter_pairing = true', 'chapter_pairing = false')
        self.config_path.write_text(text, encoding='utf-8')

    def tearDown(self):
        for path in self.source.glob('*.cbz'):
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), self.hashes[path.name])
        self.temporary.cleanup()

    def check_metadata(self, results):
        for index, path in enumerate(results, 1):
            info = inspect_epub(path)
            expected_title = '測試漫畫' + (f' [{index}/{len(results)}]' if len(results) > 1 else '')
            self.assertEqual(info['title'], expected_title)
            self.assertEqual(info['authors'], [self.author])
            self.assertEqual(info['description'], self.description)
            self.assertEqual(info['series'], '測試漫畫')
            self.assertEqual(info['series_index'], str(index))
            self.assertTrue(info['has_cover_page'])
            self.assertEqual(info['direction'], 'rtl')

    def test_fusion_long_chapter_and_forced_page_split_keep_navigation(self):
        config = converter.load_config(self.config_path)
        job = config['jobs'][0]
        native_hashes = []

        def inspect_native(path, max_bytes):
            self.check_metadata_for_native(path)
            native_hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
            return inspect_epub(path, max_bytes)

        with converter.output_lock(config['paths']['output_dir']), \
                patch.object(converter, 'inspect_epub', side_effect=inspect_native):
            results = converter.convert_comic(job, config, target_mib=2)
        self.assertGreater(len(results), 1)
        self.check_metadata(results)
        self.assertEqual(native_hashes, [hashlib.sha256(path.read_bytes()).hexdigest() for path in results])
        labels = [info['chapter_titles'] for info in map(inspect_epub, results)]
        self.assertGreater(sum(self.chapters[0] in titles for titles in labels), 1)

    def check_metadata_for_native(self, path):
        info = inspect_epub(path)
        self.assertTrue(info['has_cover_page'])
        self.assertEqual(info['authors'], [self.author])
        self.assertEqual(info['description'], self.description)
        self.assertEqual(info['series'], '測試漫畫')

    def pairing_case(self, chapters):
        for path in self.source.glob('*.cbz'):
            path.unlink()
        self.hashes = {}
        for name, entries in chapters:
            path = self.source / f'{name}.cbz'
            with zipfile.ZipFile(path, 'w') as archive:
                for filename, size, seed in entries:
                    pixels = random.Random(seed).randbytes(size[0] * size[1] * 3)
                    buffer = BytesIO()
                    Image.frombytes('RGB', size, pixels).save(buffer, 'PNG')
                    archive.writestr(filename, buffer.getvalue())
            self.hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        text = self.config_path.read_text(encoding='utf-8').replace('chapter_pairing = false', 'chapter_pairing = true')
        self.config_path.write_text(text, encoding='utf-8')

    def convert_capturing_pairing_report(self, config, target_mib=10):
        captured = {}
        validate = converter.validate_pairing_report

        def save_report(path, summaries, job, target, direction):
            report = validate(path, summaries, job, target, direction)
            captured['report'] = json.loads(Path(path).read_text(encoding='utf-8'))
            captured['target'] = target
            captured['direction'] = direction
            return report

        with converter.output_lock(config['paths']['output_dir']), \
                patch.object(converter, 'validate_pairing_report', side_effect=save_report):
            outputs = converter.convert_comic(config['jobs'][0], config, target_mib=target_mib)
        return outputs, captured

    def test_pairing_single_nested_cbz_reports_one_chapter(self):
        self.pairing_case([('50 - Nested', [(f'chapter/{int(index > 1)}/{index:03}.png', (160, 240), index)
                                             for index in range(1, 5)])])
        config = converter.load_config(self.config_path)
        outputs, captured = self.convert_capturing_pairing_report(config)
        report = captured['report']
        self.assertTrue(config['jobs'][0]['chapter_pairing'])
        self.assertEqual(len(report['chapters']), 1)
        self.assertEqual(report['chapters'][0]['title'], '50 - Nested')
        self.assertEqual(report['chapters'][0]['source_ids'], [1, 2, 3, 4])
        self.assertEqual(len(report['groups']), 2)
        self.assertEqual(len(report['pages']), 4)
        self.check_metadata(outputs)

    def test_pairing_fusion_fallback_and_forced_volume_split_keep_pairs_together(self):
        self.pairing_case([
            ('01 - Long', [(f'{index:03}.png', (160, 240), index) for index in range(1, 13)]),
            ('02 - Wide', [('nested/wide.png', (320, 200), 101)]),
            ('03 - Mixed', [(f'{index:03}.png', size, 200 + index)
                            for index, size in ((1, (160, 240)), (2, (160, 320)), (3, (160, 240)))]),
        ])
        config = converter.load_config(self.config_path)
        outputs, captured = self.convert_capturing_pairing_report(config, target_mib=2)
        report = captured['report']
        self.assertGreater(len(outputs), 1)
        self.assertEqual([chapter['title'] for chapter in report['chapters']],
                         ['01 - Long', '02 - Wide', '03 - Mixed'])
        wide = report['chapters'][1]
        self.assertEqual((wide['mode'], wide['reason']), ('native', 'wide_image'))
        self.assertGreater(sum(page['source_id'] == wide['source_ids'][0] for page in report['pages']), 1)
        page_by_group = {}
        for page in report['pages']:
            page_by_group.setdefault(page['group_id'], []).append(page)
        for group in report['groups']:
            if group['mode'] == 'paired':
                pages = page_by_group[group['id']]
                self.assertEqual([page['source_id'] for page in pages], group['source_ids'])
                self.assertEqual(len({page['tome'] for page in pages}), 1)
        self.check_metadata(outputs)

    def test_single_source_keeps_embedded_metadata_when_book_fields_are_missing_or_empty(self):
        sources = list(self.source.glob('*.cbz'))
        for path in sources[1:]:
            path.unlink()
        source = sources[0]
        with zipfile.ZipFile(source, 'a') as archive:
            archive.writestr('ComicInfo.xml', '<ComicInfo><Series>來源系列</Series>'
                             '<Writer>來源作者</Writer><Summary>來源簡介</Summary></ComicInfo>')
        self.hashes[source.name] = hashlib.sha256(source.read_bytes()).hexdigest()
        (self.source / 'info.json').unlink()
        config = converter.load_config(self.config_path)
        results = converter.convert_comic(config['jobs'][0], config)
        info = inspect_epub(results[0])
        self.assertEqual(info['authors'], ['來源作者'])
        self.assertEqual(info['description'], '來源簡介')
        self.assertEqual(info['series'], '測試漫畫')
        (self.source / 'info.json').write_text('{"description":""}', encoding='utf-8')
        config = converter.load_config(self.config_path)
        results = converter.convert_comic(config['jobs'][0], config)
        info = inspect_epub(results[0])
        self.assertEqual(info['authors'], ['來源作者'])
        self.assertEqual(info['description'], '來源簡介')

    def test_single_source_custom_cover_shift_and_default_cover_rerun(self):
        for path in list(self.source.glob('*.cbz'))[1:]:
            path.unlink()
        self.config_path.write_text(self.config_path.read_text(encoding='utf-8').replace('spreadshift = false', 'spreadshift = true'), encoding='utf-8')
        config = converter.load_config(self.config_path)
        with converter.output_lock(config['paths']['output_dir']):
            results = converter.convert_comic(config['jobs'][0], config)
        self.assertEqual(len(results), 1)
        self.check_metadata(results)
        first_spreads = inspect_epub(results[0])['page_spreads']
        (self.source / 'cover.jpg').unlink()
        config = converter.load_config(self.config_path)
        config['jobs'][0]['spreadshift'] = False
        with converter.output_lock(config['paths']['output_dir']):
            second = converter.convert_comic(config['jobs'][0], config)
        self.check_metadata(second)
        self.assertNotEqual(first_spreads, inspect_epub(second[0])['page_spreads'])

    def test_main_preflights_batch_and_replaces_without_backup(self):
        for path in list(self.source.glob('*.cbz'))[1:]:
            path.unlink()
        command = [sys.executable, '-B', str(converter.ROOT / 'main.py'), '--config', self.config_path.name]
        result = subprocess.run(command, cwd=self.root, env=dict(os.environ, PYTHONUTF8='1'),
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = self.root / 'output'
        before = list(output.glob('*.kepub.epub'))
        self.assertEqual(len(before), 1)
        self.config_path.write_text(self.config_path.read_text(encoding='utf-8').replace(
            'keep_backup = true', 'keep_backup = false'), encoding='utf-8')
        result = subprocess.run(command, cwd=self.root, env=dict(os.environ, PYTHONUTF8='1'),
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.check_metadata(list(output.glob('*.kepub.epub')))
        self.assertEqual(list(output.glob('.cbz2kepub-publish-*')), [])
        self.assertEqual(list((self.root / '.cbz2kepub-work').glob('c2k-*')), [])
        self.assertFalse((output / '.cbz2kepub.lock').exists())

    def test_single_source_ignores_excluded_entries_and_replaces_malformed_embedded_xml(self):
        sources = list(self.source.glob('*.cbz'))
        for path in sources[1:]:
            path.unlink()
        source = sources[0]
        with zipfile.ZipFile(source, 'a') as archive:
            archive.writestr('ComicInfo.xml', '<ComicInfo><Summary>broken')
            archive.writestr('__MACOSX/invalid.png', b'x' * 1_000_000)
            archive.writestr('.DS_Store', b'metadata')
            archive.writestr('Thumbs.db', b'thumbnail')
            archive.writestr('._1.png', b'AppleDouble sidecar')
        self.hashes[source.name] = hashlib.sha256(source.read_bytes()).hexdigest()
        config = converter.load_config(self.config_path)
        self.assertEqual(config['jobs'][0]['image_count'], 3)
        result = converter.convert_comic(config['jobs'][0], config)
        self.check_metadata(result)
        self.assertEqual(sum(inspect_epub(path)['body_pages'] for path in result), 3)


if __name__ == '__main__':
    unittest.main()
