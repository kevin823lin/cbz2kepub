import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZIP_STORED, ZipFile
import zlib

from cbz2kepub.epub_outputs import COVER_ID, DC, NS, OPF, discard_owned_outputs, inspect_epub, inspect_owned_outputs, publish_outputs


def png(width=2, height=3):
    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress((b"\0" + b"\x80" * width * 3) * height)) + chunk(b"IEND", b"")


def make_epub(path):
    contents = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="Package/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        "Package/book.opf": f'''<package xmlns="{OPF}" xmlns:dc="{DC}" version="3.0" prefix="rendition: http://www.idpf.org/vocab/rendition/#">
          <metadata><dc:title>Old title</dc:title><dc:creator id="author">Old author</dc:creator><dc:description>Old description</dc:description>
            <meta property="role" refines="#author">aut</meta>
            <meta property="belongs-to-collection" id="c02">Old series</meta><meta property="collection-type" refines="#c02">series</meta><meta property="group-position" refines="#c02">99</meta>
            <meta name="calibre:series" content="Duplicate series"/><meta name="calibre:series_index" content="99"/>
            <meta property="rendition:layout">pre-paginated</meta></metadata>
          <manifest><item id="page" href="Text/page%201.xhtml" media-type="application/xhtml+xml"/>
            <item id="cbz2kepub-cover" href="Text/cbz2kepub-cover.xhtml" media-type="application/xhtml+xml"/>
            <item id="cover-image" href="Images/cover.png" media-type="image/png" properties="cover-image"/>
            <item id="image" href="Images/kcc-0001-kcc-x.png" media-type="image/png"/>
            <item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>
            <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest>
          <spine toc="ncx" page-progression-direction="rtl"><itemref idref="cbz2kepub-cover" properties="rendition:page-spread-center"/><itemref idref="page" properties="page-spread-right"/></spine>
          </package>'''.encode(),
        "Package/Text/page 1.xhtml": b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Page</title></head><body><div id="page"><img src="../Images/kcc-0001-kcc-x.png"/><img src="../Images/kcc-0001-kcc-x.png"/></div></body></html>',
        "Package/Text/cbz2kepub-cover.xhtml": b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Cover</title><meta name="viewport" content="width=2, height=3"/></head><body><svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 2 3"><image width="2" height="3" href="../Images/cover.png" xlink:href="../Images/cover.png"/></svg></body></html>',
        "Package/nav.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body><nav epub:type="toc"><ol><li><a href="Text/page%201.xhtml#page">24.05 中文章名</a></li></ol></nav></body></html>'.encode(),
        "Package/toc.ncx": '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint id="chapter"><navLabel><text>24.05 中文章名</text></navLabel><content src="Text/page%201.xhtml#page"/></navPoint></navMap></ncx>'.encode(),
        "Package/Images/cover.png": png(),
        "Package/Images/kcc-0001-kcc-x.png": png(),
    }
    with ZipFile(path, "w", compression=ZIP_STORED) as archive:
        for name, content in contents.items():
            archive.writestr(name, content)


def rewrite(path, name, change):
    with ZipFile(path) as archive:
        data = {item.filename: archive.read(item) for item in archive.infolist()}
    data[name] = change(data[name])
    with ZipFile(path, "w", compression=ZIP_STORED) as archive:
        for filename, content in data.items():
            archive.writestr(filename, content)


class EpubTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "book.kepub.epub"
        make_epub(self.path)

    def test_inspection_reads_metadata_cover_and_body_without_modifying_epub(self):
        original = self.path.read_bytes()
        summary = inspect_epub(self.path)
        self.assertEqual(summary['body_pages'], 1)
        self.assertTrue(summary['has_cover_page'])
        self.assertEqual(summary['title'], 'Old title')
        self.assertEqual(summary['authors'], ['Old author'])
        self.assertEqual(summary['description'], 'Old description')
        self.assertEqual(summary['series'], 'Old series')
        self.assertEqual(summary['series_index'], '99')
        self.assertEqual(summary['page_spreads'], ['page-spread-right'])
        self.assertEqual(summary['body_layout'][0]['xhtml'], 'Package/Text/page 1.xhtml')
        self.assertEqual(summary['body_layout'][0]['images'][0]['size'], (2, 3))
        self.assertEqual(summary['body_layout'][0]['side'], 'right')
        self.assertIsNone(summary['body_layout'][0]['viewport'])
        self.assertEqual(self.path.read_bytes(), original)

    def test_inspection_reads_kobo_prefixed_spread_side(self):
        rewrite(self.path, 'Package/book.opf', lambda data: data.replace(
            b'properties="page-spread-right"', b'properties="rendition:page-spread-right"'))
        summary = inspect_epub(self.path)
        self.assertEqual(summary['body_layout'][0]['side'], 'right')

    def test_missing_resource_and_bad_fragment_fail(self):
        rewrite(self.path, "Package/Text/page 1.xhtml", lambda data: data.replace(b"kcc-0001-kcc-x.png", b"missing.png"))
        with self.assertRaisesRegex(ValueError, "Missing EPUB reference"):
            inspect_epub(self.path)
        make_epub(self.path)
        rewrite(self.path, "Package/nav.xhtml", lambda data: data.replace(b"#page", b"#missing"))
        with self.assertRaisesRegex(ValueError, "Missing EPUB fragment"):
            inspect_epub(self.path)

    def test_empty_body_and_size_limit_fail(self):
        rewrite(self.path, "Package/book.opf", lambda data: data.replace(b'<itemref idref="page" properties="page-spread-right"/>', b""))
        with self.assertRaisesRegex(ValueError, "no body pages"):
            inspect_epub(self.path)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            inspect_epub(self.path, max_bytes=1)

    def test_navigation_disagreement_fails(self):
        rewrite(self.path, "Package/toc.ncx", lambda data: data.replace("24.05 中文章名".encode(), b"Wrong chapter"))
        with self.assertRaisesRegex(ValueError, "disagree"):
            inspect_epub(self.path)

    def test_duplicate_chapter_targets_fail(self):
        rewrite(self.path, "Package/nav.xhtml", lambda data: data.replace(b"</ol>", b'<li><a href="Text/page%201.xhtml#page">Duplicate</a></li></ol>'))
        rewrite(self.path, "Package/toc.ncx", lambda data: data.replace(b"</navMap>", b'<navPoint id="duplicate"><navLabel><text>Duplicate</text></navLabel><content src="Text/page%201.xhtml#page"/></navPoint></navMap>'))
        with self.assertRaisesRegex(ValueError, "duplicate targets"):
            inspect_epub(self.path)

    def test_crc_corruption_fails(self):
        with ZipFile(self.path) as archive:
            item = archive.getinfo("Package/Images/kcc-0001-kcc-x.png")
            offset = item.header_offset + 30 + len(item.filename.encode("utf-8")) + len(item.extra)
        with self.path.open("r+b") as stream:
            stream.seek(offset)
            value = stream.read(1)
            stream.seek(offset)
            stream.write(bytes([value[0] ^ 1]))
        with self.assertRaises((ValueError, BadZipFile)):
            inspect_epub(self.path)

class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stage = self.root / "stage"
        self.output = self.root / "output"
        self.stage.mkdir()
        self.output.mkdir()

    def files(self, names, prefix=b"new"):
        paths = [self.stage / name for name in names]
        for path in paths:
            path.write_bytes(prefix + path.name.encode())
        return paths

    def test_first_publish_and_volume_replacement_preserve_prefix_work(self):
        unrelated = self.output / "作品 外傳.kepub.epub"
        unrelated.write_bytes(b"unrelated")
        originals = self.files(["作品 1.kepub.epub", "作品 2.kepub.epub"], b"old")
        publish_outputs(originals, self.output, "作品")
        replacement = self.files(["作品.kepub.epub"])
        expected = replacement[0].read_bytes()
        published = publish_outputs(replacement, self.output, "作品")
        self.assertEqual(published, [self.output / "作品.kepub.epub"])
        self.assertEqual(published[0].read_bytes(), expected)
        self.assertFalse(replacement[0].exists())
        self.assertFalse((self.output / originals[0].name).exists())
        self.assertFalse((self.output / originals[1].name).exists())
        self.assertEqual(unrelated.read_bytes(), b"unrelated")
        manifest = next(self.output.glob(".cbz2kepub-*.json"))
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["files"], ["作品.kepub.epub"])

    def test_unowned_legacy_outputs_fail_without_deleting(self):
        legacy = self.output / "作品 1.kepub.epub"
        legacy.write_bytes(b"legacy")
        with self.assertRaisesRegex(ValueError, "ownership"):
            publish_outputs(self.files(["作品.kepub.epub"]), self.output, "作品")
        self.assertEqual(legacy.read_bytes(), b"legacy")

    def test_sanitized_name_collision_across_runs_keeps_first_work(self):
        old = self.files(["_.kepub.epub"], b"old")
        publish_outputs(old, self.output, "_", work_title="會")
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        with self.assertRaisesRegex(ValueError, "manifest"):
            publish_outputs(self.files(["_.kepub.epub"], b"new"), self.output, "_", work_title="許")
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, before)

    def test_case_only_title_cannot_bypass_unfinished_transaction(self):
        pending = self.output / '.cbz2kepub-publish-test'
        pending.mkdir()
        (pending / 'transaction.json').write_text(json.dumps({'title': 'Work'}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'requires recovery'):
            publish_outputs(self.files(['work.kepub.epub']), self.output, 'work')
        self.assertTrue((pending / 'transaction.json').exists())

    def test_partial_publication_rolls_back_all_old_files_and_manifest(self):
        old = self.files(["作品 1.kepub.epub", "作品 2.kepub.epub"], b"old")
        publish_outputs(old, self.output, "作品")
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        replacement = self.files(["作品 1.kepub.epub", "作品 2.kepub.epub"], b"new")
        from cbz2kepub import epub_outputs
        actual_replace = epub_outputs.os.replace
        failed = False

        def fail_second(source, destination):
            nonlocal failed
            if not failed and Path(source).parent.name == "incoming" and Path(source).name == "作品 2.kepub.epub":
                failed = True
                raise OSError("simulated second volume failure")
            return actual_replace(source, destination)

        with patch("cbz2kepub.epub_outputs.os.replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                publish_outputs(replacement, self.output, "作品")
        self.assertTrue(failed)
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, before)
        self.assertTrue(all(path.exists() for path in replacement))

    def test_same_volume_moves_without_copying_and_preserves_bytes(self):
        staged = self.files(["作品.kepub.epub"])
        expected = staged[0].read_bytes()
        with patch("cbz2kepub.epub_outputs.same_volume", return_value=True), patch("cbz2kepub.epub_outputs.shutil.copyfile") as copy:
            result = publish_outputs(staged, self.output, "作品")
        copy.assert_not_called()
        self.assertEqual(result[0].read_bytes(), expected)
        self.assertFalse(staged[0].exists())

    def test_cross_volume_checks_size_then_copies_and_removes_source_after_commit(self):
        staged = self.files(["作品.kepub.epub"])
        expected = staged[0].read_bytes()
        from cbz2kepub import epub_outputs
        actual_copy = epub_outputs.shutil.copyfile

        def copy(source, destination):
            self.assertTrue(Path(source).exists())
            self.assertFalse((self.output / Path(source).name).exists())
            return actual_copy(source, destination)

        with patch("cbz2kepub.epub_outputs.same_volume", return_value=False), \
                patch("cbz2kepub.epub_outputs.require_space") as check, \
                patch("cbz2kepub.epub_outputs.shutil.copyfile", side_effect=copy) as copy_mock:
            result = publish_outputs(staged, self.output, "作品")
        check.assert_called_once_with(self.output, len(expected), "publication", credit=0)
        copy_mock.assert_called_once()
        self.assertEqual(result[0].read_bytes(), expected)
        self.assertFalse(staged[0].exists())

    def test_cross_volume_capacity_failure_does_not_copy_or_remove_any_files(self):
        publish_outputs(self.files(["作品.kepub.epub"], b"old"), self.output, "作品")
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        staged = self.files(["作品.kepub.epub"])
        with patch("cbz2kepub.epub_outputs.same_volume", return_value=False), \
                patch("cbz2kepub.epub_outputs.require_space", side_effect=OSError("not enough space")), \
                patch("cbz2kepub.epub_outputs.shutil.copyfile") as copy:
            with self.assertRaisesRegex(OSError, "not enough space"):
                publish_outputs(staged, self.output, "作品")
        copy.assert_not_called()
        self.assertTrue(staged[0].exists())
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, before)

    def test_failed_cross_volume_copy_keeps_sources_and_old_output(self):
        publish_outputs(self.files(["作品.kepub.epub"], b"old"), self.output, "作品")
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        staged = self.files(["作品.kepub.epub"])

        def partial_copy(source, destination):
            Path(destination).write_bytes(b"partial")
            raise OSError("copy failed")

        with patch("cbz2kepub.epub_outputs.same_volume", return_value=False), \
                patch("cbz2kepub.epub_outputs.shutil.copyfile", side_effect=partial_copy):
            with self.assertRaisesRegex(OSError, "copy failed"):
                publish_outputs(staged, self.output, "作品")
        self.assertEqual(staged[0].read_bytes(), b"new" + staged[0].name.encode())
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, before)

    def test_failure_during_staging_restores_previously_moved_sources(self):
        staged = self.files(["作品 1.kepub.epub", "作品 2.kepub.epub"])
        before = [path.read_bytes() for path in staged]
        from cbz2kepub import epub_outputs
        actual_replace = epub_outputs.os.replace

        def fail_second(source, destination):
            if Path(source) == staged[1]:
                raise OSError("staging failed")
            return actual_replace(source, destination)

        with patch("cbz2kepub.epub_outputs.os.replace", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "staging failed"):
                publish_outputs(staged, self.output, "作品")
        self.assertEqual([path.read_bytes() for path in staged], before)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_inspection_of_absent_output_is_read_only(self):
        missing = self.root / "missing"
        manifest, old = inspect_owned_outputs(missing, "作品")
        self.assertEqual(manifest.parent, missing)
        self.assertEqual(old, [])
        self.assertFalse(missing.exists())

    def test_discard_removes_only_owned_files_and_manifest(self):
        old = self.files(["作品 1.kepub.epub", "作品 2.kepub.epub"], b"old")
        publish_outputs(old, self.output, "作品")
        unrelated = self.output / "作品 外傳.kepub.epub"
        unrelated.write_bytes(b"unrelated")
        discard_owned_outputs(self.output, "作品")
        self.assertEqual(list(self.output.iterdir()), [unrelated])
        self.assertEqual(unrelated.read_bytes(), b"unrelated")
        self.assertEqual(inspect_owned_outputs(self.output, "作品")[1], [])

    def test_discard_rejects_unknown_ownership_before_deleting(self):
        legacy = self.output / "作品.kepub.epub"
        legacy.write_bytes(b"legacy")
        with self.assertRaisesRegex(ValueError, "ownership"):
            discard_owned_outputs(self.output, "作品")
        self.assertEqual(legacy.read_bytes(), b"legacy")

    def test_interrupted_discard_retains_journal_and_blocks_next_run(self):
        publish_outputs(self.files(["作品 1.kepub.epub", "作品 2.kepub.epub"], b"old"), self.output, "作品")
        actual_unlink = Path.unlink

        def fail_second(path, *args, **kwargs):
            if path == self.output / "作品 2.kepub.epub":
                raise PermissionError("cannot discard")
            return actual_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_second):
            with self.assertRaisesRegex(PermissionError, "cannot discard"):
                discard_owned_outputs(self.output, "作品")
        transaction = next(self.output.glob(".cbz2kepub-publish-*/transaction.json"))
        self.assertEqual(json.loads(transaction.read_text(encoding="utf-8"))["operation"], "discard")
        self.assertFalse((self.output / "作品 1.kepub.epub").exists())
        self.assertTrue((self.output / "作品 2.kepub.epub").exists())
        with self.assertRaisesRegex(ValueError, "requires recovery"):
            inspect_owned_outputs(self.output, "作品")

    def test_no_backup_does_not_restore_discarded_old_files_after_failed_publication(self):
        publish_outputs(self.files(["作品.kepub.epub"], b"old"), self.output, "作品")
        staged = self.files(["作品.kepub.epub"])
        from cbz2kepub import epub_outputs
        actual_replace = epub_outputs.os.replace

        def fail_install(source, destination):
            if Path(source).parent.name == "incoming" and Path(destination).parent == self.output:
                raise OSError("install failed")
            return actual_replace(source, destination)

        with patch("cbz2kepub.epub_outputs.os.replace", side_effect=fail_install):
            with self.assertRaisesRegex(OSError, "install failed"):
                publish_outputs(staged, self.output, "作品", keep_backup=False)
        self.assertFalse((self.output / "作品.kepub.epub").exists())
        self.assertEqual(staged[0].read_bytes(), b"new" + staged[0].name.encode())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_no_backup_rechecks_actual_space_after_deleting_old_output(self):
        publish_outputs(self.files(["作品.kepub.epub"], b"old"), self.output, "作品")
        staged = self.files(["作品.kepub.epub"])

        def check(path, size, label, credit=0, margin=None):
            if label == "publication after old-output deletion":
                raise OSError("space was not reclaimed")

        with patch("cbz2kepub.epub_outputs.same_volume", return_value=False), \
                patch("cbz2kepub.epub_outputs.require_space", side_effect=check) as checks, \
                patch("cbz2kepub.epub_outputs.shutil.copyfile") as copy:
            with self.assertRaisesRegex(OSError, "space was not reclaimed"):
                publish_outputs(staged, self.output, "作品", keep_backup=False)
        self.assertEqual(checks.call_args_list[-1].args, (self.output, staged[0].stat().st_size, "publication after old-output deletion"))
        copy.assert_not_called()
        self.assertTrue(staged[0].exists())
        self.assertFalse((self.output / staged[0].name).exists())

    def test_incomplete_cross_volume_copy_cannot_replace_old_outputs(self):
        publish_outputs(self.files(["作品.kepub.epub"], b"old"), self.output, "作品")
        staged = self.files(["作品.kepub.epub"])
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}

        def incomplete(source, destination):
            Path(destination).write_bytes(b"partial")

        with patch("cbz2kepub.epub_outputs.same_volume", return_value=False), \
                patch("cbz2kepub.epub_outputs.shutil.copyfile", side_effect=incomplete):
            with self.assertRaisesRegex(OSError, "size differs"):
                publish_outputs(staged, self.output, "作品")
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, before)
        self.assertTrue(staged[0].exists())

    def test_staged_output_alias_is_rejected_before_mutation(self):
        old = publish_outputs(self.files(["作品.kepub.epub"], b"old"), self.output, "作品")
        expected = old[0].read_bytes()
        with self.assertRaisesRegex(ValueError, "aliases"):
            publish_outputs(old, self.output, "作品")
        self.assertEqual(old[0].read_bytes(), expected)

    def test_failed_rollback_retains_backups_and_blocks_next_publish(self):
        old = self.files(["作品.kepub.epub"], b"old")
        publish_outputs(old, self.output, "作品")
        replacement = self.files(["作品.kepub.epub"], b"new")
        from cbz2kepub import epub_outputs
        actual_replace = epub_outputs.os.replace

        def fail_install_and_restore(source, destination):
            if Path(source).parent.name in ("incoming", "backup"):
                raise OSError("simulated failure")
            return actual_replace(source, destination)

        with patch("cbz2kepub.epub_outputs.os.replace", side_effect=fail_install_and_restore):
            with self.assertRaisesRegex(RuntimeError, "backups retained"):
                publish_outputs(replacement, self.output, "作品")
        transaction = next(self.output.glob(".cbz2kepub-publish-*"))
        self.assertEqual((transaction / "backup" / old[0].name).read_bytes(), b"old" + old[0].name.encode())
        with self.assertRaisesRegex(ValueError, "requires recovery"):
            publish_outputs(replacement, self.output, "作品")

    def test_interrupt_after_successful_rename_retains_old_and_new_files_for_recovery(self):
        from cbz2kepub import epub_outputs
        actual_replace = epub_outputs.os.replace
        for error in (KeyboardInterrupt, SystemExit):
            for phase in ('backup', 'install', 'manifest'):
                title = f'{error.__name__}-{phase}'
                name = title + '.kepub.epub'
                with self.subTest(error=error, phase=phase):
                    old = self.files([name], b'old')
                    old_bytes = old[0].read_bytes()
                    publish_outputs(old, self.output, title)
                    staged = self.files([name], b'new')
                    new_bytes = staged[0].read_bytes()

                    def interrupt_after_rename(source, destination):
                        actual_replace(source, destination)
                        source, destination = Path(source), Path(destination)
                        if ((phase == 'backup' and destination.parent.name == 'backup' and source.name == name)
                                or (phase == 'install' and source.parent.name == 'incoming' and source.name == name)
                                or (phase == 'manifest' and source.parent.name == 'incoming' and source.suffix == '.json')):
                            raise error()

                    with patch('cbz2kepub.epub_outputs.os.replace', side_effect=interrupt_after_rename):
                        with self.assertRaisesRegex(RuntimeError, 'interrupted; recovery files retained') as caught:
                            publish_outputs(staged, self.output, title)
                    self.assertIsInstance(caught.exception.__cause__, error)
                    journal = next(path for path in self.output.glob('.cbz2kepub-publish-*/transaction.json')
                                   if json.loads(path.read_text(encoding='utf-8'))['title'] == title)
                    self.assertEqual((journal.parent / 'backup' / name).read_bytes(), old_bytes)
                    new_location = journal.parent / 'incoming' / name if phase == 'backup' else self.output / name
                    self.assertEqual(new_location.read_bytes(), new_bytes)
                    with self.assertRaisesRegex(ValueError, 'requires recovery'):
                        inspect_owned_outputs(self.output, title)


if __name__ == "__main__":
    unittest.main()
