from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from acceptance_real import pairing_summary, verify_cover_and_metadata
from cbz2kepub.epub_outputs import inspect_epub
from test_epub_outputs import make_epub, rewrite


class AcceptanceChecksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'book.kepub.epub'
        make_epub(self.path)
        rewrite(self.path, 'Package/book.opf', lambda data: data.replace(b'Old title', '作品'.encode())
                .replace(b'Old author', '作者'.encode()).replace(b'Old description', '簡介\n'.encode())
                .replace(b'Old series', '作品'.encode()).replace(b'>99<', b'>1<'))
        self.summary = inspect_epub(self.path)
        self.job = {'title': '作品', 'metadata': {'author': '作者', 'description': '簡介\n'}, 'cover': None}

    def test_cover_page_and_metadata_are_checked_and_preview_is_saved(self):
        result = verify_cover_and_metadata(self.path, self.summary, self.job, {}, 1, 1, self.root)
        self.assertTrue(result['author_exact_match'])
        self.assertTrue(result['description_exact_match'])
        self.assertTrue(result['cover_page_first'])
        self.assertTrue(result['cover_page_centered'])
        self.assertEqual(result['cover_dimensions'], [2, 3])
        self.assertTrue((self.root / 'embedded-cover-1.jpg').is_file())

    def test_metadata_mismatch_fails_acceptance(self):
        self.job['metadata']['description'] = '另一個簡介'
        with self.assertRaises(AssertionError):
            verify_cover_and_metadata(self.path, self.summary, self.job, {}, 1, 1, self.root)

    def test_pairing_acceptance_summary_retains_anchors_and_per_mode_counts(self):
        summary = pairing_summary({
            'direction': 'rtl', 'target': [1264, 1680],
            'chapters': [
                {'id': 1, 'title': '01', 'mode': 'paired', 'reason': 'detected', 'spreadshift': False,
                 'anchor': {'source_ids': [1, 2], 'score': 18.2}, 'source_ids': [1, 2, 3]},
                {'id': 2, 'title': '02', 'mode': 'native', 'reason': 'wide_image', 'spreadshift': False,
                 'anchor': None, 'source_ids': [4]},
            ],
            'groups': [{'mode': 'paired'}, {'mode': 'single'}, {'mode': 'native'}],
            'pages': [{}, {}, {}, {}],
        })
        self.assertEqual(summary['chapter_modes'], {'paired': 1, 'native': 1})
        self.assertEqual(summary['groups'], {'paired': 1, 'single': 1, 'native': 1})
        self.assertEqual(summary['chapters'][0]['anchor']['score'], 18.2)
        self.assertEqual(summary['pages'], 4)


if __name__ == '__main__':
    unittest.main()
