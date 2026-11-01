from io import BytesIO
from itertools import product
from pathlib import Path
from types import SimpleNamespace
import tempfile
import random
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

from cbz2kepub import space_preflight as space


class SpaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.images = self.work / 't' / 'OEBPS' / 'Images'
        self.images.mkdir(parents=True)
        self.data = dict(work=str(self.work), output_dir=str(self.work / 'output'), version=1, target=[100, 150], target_bytes=1000,
                         images=dict(page_bytes=100, originals=[200, 200], predictions=[100, 100]),
                         sources=[], expanded=450, cover_bytes=10)

    def test_image_peak_accounts_for_arbitrary_parallel_completion_order(self):
        originals, predicted = [100, 500, 200], [400, 100, 300]
        for workers in (1, 2, 3):
            budget = space.image_peak(originals, predicted, workers)
            actual = []
            for states in product(('pending', 'writing', 'done'), repeat=3):
                if states.count('writing') <= workers:
                    actual.append(sum(u if state == 'pending' else p if state == 'done' else u + p
                                      for u, p, state in zip(originals, predicted, states)))
            self.assertGreaterEqual(budget, max(actual))
        self.assertLess(space.image_peak([500] * 20, [100] * 20, 2), 12000)

    def test_five_volume_relationships_do_not_charge_links_or_merge_separate_free_space(self):
        for source, work, output in ((1, 1, 1), (1, 2, 3), (1, 1, 2), (1, 2, 2), (1, 2, 1)):
            copied = 0 if source == work else 100
            with self.subTest(source=source, work=work, output=output):
                result = space.batch_requirements([(work, output, 1000 + copied, 300, 200, True)])
                self.assertEqual(result[work], 1000 + copied)
                if work != output:
                    self.assertEqual(result[output], 300)

    def test_batch_keeps_previous_outputs_and_credits_old_deletion_only_on_its_volume(self):
        self.assertEqual(space.batch_requirements([(1, 1, 1000, 300, 200, True)] * 3), {1: 1200})
        self.assertEqual(space.batch_requirements([(1, 1, 1000, 300, 200, False)] * 3), {1: 1000})
        self.assertEqual(space.batch_requirements([(1, 2, 1000, 300, 200, False)]), {1: 1000, 2: 100})

    def test_no_backup_can_use_reclaimed_space_for_later_operation_margin(self):
        # The margin is needed during writes after deletion, not before reclaiming the old set.
        self.assertEqual(space.batch_requirements([(1, 1, 100, 80, 1000, False)], margin=50), {1: 0})
        self.assertEqual(space.batch_requirements([(1, 1, 100, 80, 1000, True)], margin=50), {1: 150})

    def test_capacity_rechecks_current_free_and_explicit_margin(self):
        with patch.object(space.shutil, 'disk_usage', return_value=SimpleNamespace(free=100)):
            space.require_space(self.work, 80, 'test', margin=20)
            with self.assertRaisesRegex(OSError, '空間不足'):
                space.require_space(self.work, 81, 'test', margin=20)
            space.require_space(self.work, 150, 'before deletion', credit=100, margin=20)
            with self.assertRaises(OSError):
                space.require_space(self.work, 150, 'after deletion', margin=20)

    def test_volume_lookup_uses_existing_ancestor_for_uncreated_paths(self):
        self.assertTrue(space.same_volume(self.work, self.work / 'future' / 'file.epub'))
        self.assertEqual(space.existing_directory(self.work / 'future'), self.work)
        with patch.object(Path, 'exists', return_value=False):
            with self.assertRaises(FileNotFoundError):
                space.existing_directory(self.work)

    def test_linked_cbz_not_counted_and_copies_persist_through_work_peak(self):
        plan = space.SpacePlan(self.data)
        linked_peak, output = plan.initial_budget(0)
        copied_peak, copied_output = plan.initial_budget(5000)
        self.assertEqual(copied_peak - linked_peak, 5000)
        self.assertEqual(output, copied_output)

    def test_preserved_chapters_and_metadata_are_in_initial_output_budget(self):
        self.data['images']['originals'] = [600] * 6
        self.data['metadata_bytes'] = 2_000_000
        _, output = space.SpacePlan(self.data).packaging_peak(3600)
        # Six 0.6-target chapters need six volumes, despite ceil(total/target) being four.
        self.assertGreaterEqual(output, 3600 + 6 * (100 + 2_000_000))

    def test_generated_xml_is_counted_in_staging_and_fusion(self):
        self.data['expanded'] = 1_000_000
        initial, _ = space.SpacePlan(self.data).initial_budget(0)
        self.data['sidecar_bytes'] = 50_000
        with_xml, _ = space.SpacePlan(self.data).initial_budget(0)
        self.assertEqual(with_xml - initial, 100_000)

    def test_extract_uses_current_archive_sizes_and_rejects_unregistered_source(self):
        source = self.work / 'source.cbz'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('page.jpg', b'12345')
            archive.writestr('other.txt', b'abc')
        self.data['sources'] = [dict(path=str(source), expanded=1)]
        plan = space.SpacePlan(self.data)
        with patch.object(space, 'require_space') as check:
            plan.before_extract(source, self.images)
        self.assertEqual(check.call_args.args[1], 8)
        with self.assertRaisesRegex(ValueError, 'absent'):
            plan.before_extract(self.work / 'unknown.cbz', self.images)

    def test_extract_exclusions_match_initial_stats_and_do_not_charge_skipped_entries(self):
        from cbz2kepub import converter
        source = self.work / 'source.cbz'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('page.jpg', b'12345')
            archive.writestr('notes.txt', b'abc')
            archive.writestr('._page.jpg', b'four')
            for name in ('__MACOSX/unused.dat', 'nested/__MACOSX/unused.png', '.DS_Store',
                         'nested/.DS_Store', 'thumbs.db', 'nested/Thumbs.db'):
                archive.writestr(name, b'x' * 100_000)
        self.data['sources'] = [dict(path=str(source))]
        stats = converter.inspect_source(source)
        self.assertEqual(stats['expanded'], 12)
        self.assertEqual(stats['image_count'], 1)
        with patch.object(space.shutil, 'disk_usage', return_value=SimpleNamespace(free=space.MARGIN + 12)):
            space.SpacePlan(self.data).before_extract(source, self.images)
        with patch.object(space.shutil, 'disk_usage', return_value=SimpleNamespace(free=space.MARGIN + 11)):
            with self.assertRaisesRegex(OSError, '空間不足'):
                space.SpacePlan(self.data).before_extract(source, self.images)

    def test_images_check_only_remaining_increase_and_rejects_paths_outside_work(self):
        Image.new('RGB', (100, 150), 'red').save(self.images / 'page.png')
        plan = space.SpacePlan(self.data)
        current = (self.images / 'page.png').stat().st_size
        with patch.object(space, 'require_space') as check:
            plan.before_images(self.images)
        expected = max(space.image_peak([200, 200], [100, 100], plan.workers), plan.packaging_peak(200)[0]) - current
        self.assertEqual(check.call_args.args[1], expected)
        with self.assertRaisesRegex(ValueError, 'outside'):
            plan.before_images(self.work.parent)

    def test_zip_checks_only_one_new_archive_against_current_free(self):
        (self.images / 'page.jpg').write_bytes(b'bytes')
        plan = space.SpacePlan(self.data)
        with patch.object(space, 'require_space') as check:
            plan.before_zip(self.work / 't', self.work / 'o' / 'book.epub')
            needed = check.call_args.args[1]
        self.assertGreater(needed, 5)
        with patch.object(space.shutil, 'disk_usage', return_value=SimpleNamespace(free=needed + space.MARGIN)):
            plan.before_zip(self.work / 't', self.work / 'o' / 'book.epub')
        with patch.object(space.shutil, 'disk_usage', return_value=SimpleNamespace(free=needed + space.MARGIN - 1)):
            with self.assertRaisesRegex(OSError, '空間不足'):
                plan.before_zip(self.work / 't', self.work / 'o' / 'book.epub')

    def test_after_images_replaces_predictions_with_actual_sizes_and_volume_count(self):
        first, second = self.work / 't' / 'volume1', self.work / 't' / 'volume2'
        first.mkdir()
        second.mkdir()
        (first / 'page.jpg').write_bytes(b'a' * 500)
        (second / 'page.jpg').write_bytes(b'b' * 800)
        overhead = 4096
        with patch.object(space, 'require_space') as check, \
                patch.object(space, 'same_volume', side_effect=lambda first, second: Path(second) == self.work):
            space.SpacePlan(self.data).after_images([first, second])
        # Completed ZIPs replace their work images; only the current ZIP adds a content copy.
        expected = max(overhead + (500 + overhead),
                       overhead + overhead + (800 + overhead))
        self.assertEqual(check.call_args_list[0].args[1], expected)
        self.assertEqual(check.call_args_list[1].args[1], 1300 + 2 * overhead)

    def test_after_images_measures_each_volume_cover_in_memory(self):
        tome = self.work / 't' / 'book'
        tome.mkdir()
        (tome / 'page.jpg').write_bytes(b'x' * 500)
        calls = []

        def cover(target, index, count):
            calls.append((index, count))
            target.write(b'cover' * 10)

        self.data['images']['page_bytes'] = 100_000_000
        with patch.object(space, 'require_space') as check:
            space.SpacePlan(self.data).after_images([tome], SimpleNamespace(save_to_folder=cover))
        self.assertEqual(calls, [(0, 1)])
        self.assertEqual(check.call_args.args[1], 500 + 2 * (50 + 4096))

    def test_calibration_reads_sources_without_disk_outputs_and_models_split_pages(self):
        source = self.work / 'source.cbz'
        with zipfile.ZipFile(source, 'w') as archive:
            for index, size in enumerate(((30, 50), (60, 40), (40, 60), (50, 80))):
                image = BytesIO()
                Image.new('RGB', size, (index * 60, 40, 100)).save(image, 'PNG')
                archive.writestr(f'{index}.png', image.getvalue())
            archive.writestr('__MACOSX/invalid.png', b'not an image')
            archive.writestr('thumbs.db/invalid.png', b'not an image')
        before = source.read_bytes()
        with patch.object(space, 'require_space'):
            result = space.calibrate_images([source], (100, 150))
        self.assertEqual(result['samples'], 4)
        self.assertEqual(len(result['predictions']), 4)
        self.assertGreater(result['predictions'][1], result['predictions'][0])
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.work.iterdir()), ['source.cbz', 't'])

    def test_large_sample_budget_applies_only_to_its_stratum(self):
        source = self.work / 'source.cbz'
        with zipfile.ZipFile(source, 'w') as archive:
            for index in range(48):
                image = Image.new('RGB', (100, 150), 'white') if index < 47 else Image.frombytes(
                    'RGB', (100, 150), random.Random(1).randbytes(100 * 150 * 3))
                buffer = BytesIO()
                image.save(buffer, 'PNG')
                archive.writestr(f'{index}.png', buffer.getvalue())
        before = source.read_bytes()
        result = space.calibrate_images([source], (100, 150))
        self.assertEqual(result['samples'], 24)
        self.assertGreater(max(result['predictions']), 10 * min(result['predictions']))
        self.assertLess(sum(result['predictions']), result['page_bytes'] * 48 // 4)
        self.assertEqual(source.read_bytes(), before)

    def test_calibration_honors_upscale_and_color_settings_without_filling_target(self):
        source = self.work / 'source.cbz'
        with zipfile.ZipFile(source, 'w') as archive:
            buffer = BytesIO()
            Image.new('RGB', (30, 60), 'red').save(buffer, 'PNG')
            archive.writestr('page.png', buffer.getvalue())
        saved = []
        save = Image.Image.save

        def record(image, *args, **kwargs):
            saved.append((image.size, image.mode))
            return save(image, *args, **kwargs)

        with patch.object(Image.Image, 'save', record):
            space.calibrate_images([source], (100, 150), upscale=False, forcecolor=False)
            space.calibrate_images([source], (100, 150), upscale=True, forcecolor=True)
        self.assertEqual(saved, [((30, 60), 'L'), ((75, 150), 'RGB')])

    def test_split_estimate_matches_default_kcc_landscape_thresholds(self):
        self.assertEqual(space.page_count((110, 100), (100, 150)), 1)
        self.assertEqual(space.page_count((150, 100), (100, 150)), 2)
        self.assertEqual(space.page_count((200, 100), (100, 150)), 1)

    def test_image_phase_keeps_stratified_predictions_instead_of_global_maximum(self):
        self.data['images']['predictions'] = [10, 30]
        self.data['images']['page_bytes'] = 1000
        with patch.object(space.SpacePlan, 'packaging_peak', return_value=(0, 0)) as packing, \
                patch.object(space, 'require_space') as check:
            space.SpacePlan(self.data).before_images(self.images)
        packing.assert_called_once_with(40)
        self.assertEqual(check.call_args.args[1], 440)


if __name__ == '__main__':
    unittest.main()
