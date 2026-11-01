from pathlib import Path
import tempfile
import unittest

from acceptance_real import verify_cover_and_metadata
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


if __name__ == '__main__':
    unittest.main()
