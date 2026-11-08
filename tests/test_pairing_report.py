import json
from pathlib import Path
import tempfile
import unittest

from cbz2kepub.converter import validate_pairing_report


class PairingReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'pairing.json'
        self.job = {
            'sources': [Path('24.05 - A.cbz')],
            'archive_stats': [{'image_count': 2}],
            'image_count': 2,
            'spreadshift': False,
        }
        self.summaries = [{
            'body_layout': [
                {'xhtml': 'Text/1.xhtml', 'images': [{'path': 'Images/kcc-0001-kcc-x.jpg', 'size': (80, 120)}], 'viewport': (80, 120), 'side': 'right'},
                {'xhtml': 'Text/2.xhtml', 'images': [{'path': 'Images/kcc-0002-kcc-x.jpg', 'size': (90, 120)}], 'viewport': (90, 120), 'side': 'left'},
            ]
        }]
        self.report = {
            'version': 2, 'state': 'completed', 'target': [100, 150], 'direction': 'rtl',
            'chapters': [{'id': 1, 'title': '24.05 - A', 'source_ids': [1, 2], 'mode': 'paired',
                          'spreadshift': False, 'reason': 'detected',
                          'anchor': {'source_ids': [1, 2], 'score': 12.5}}],
            'groups': [{'id': 1, 'chapter_id': 1, 'source_ids': [1, 2], 'mode': 'paired'}],
            'pages': [
                {'source_id': 1, 'group_id': 1, 'image': 'Images/kcc-0001-kcc-x.jpg', 'xhtml': 'Text/1.xhtml',
                 'tome': 1, 'side': 'right', 'image_size': [80, 120], 'viewport': [80, 120],
                 'geometry': {'crop_box': [0, 0, 100, 150], 'rendered_size': [80, 120]}},
                {'source_id': 2, 'group_id': 1, 'image': 'Images/kcc-0002-kcc-x.jpg', 'xhtml': 'Text/2.xhtml',
                 'tome': 1, 'side': 'left', 'image_size': [90, 120], 'viewport': [90, 120],
                 'geometry': {'crop_box': [0, 0, 100, 150], 'rendered_size': [90, 120]}},
            ],
        }

    def validate(self):
        self.path.write_text(json.dumps(self.report), encoding='utf-8')
        return validate_pairing_report(self.path, self.summaries, self.job, (100, 150), 'rtl')

    def test_valid_pairing_report_matches_epub(self):
        self.assertEqual(self.validate()['state'], 'completed')

    def test_strip_anchor_has_a_distinct_score_range(self):
        anchor = self.report['chapters'][0]['anchor']
        anchor.update(method='strip', score=0.968)
        self.assertEqual(self.validate()['chapters'][0]['anchor'], anchor)
        for method, score in [('strip', 12.5), ('strip', 0.2), ('unknown', 0.99)]:
            with self.subTest(method=method, score=score):
                anchor.update(method=method, score=score)
                with self.assertRaisesRegex(ValueError, 'anchor'):
                    self.validate()

    def test_missing_malformed_or_analyzing_report_fails(self):
        with self.assertRaisesRegex(ValueError, '缺失或無效'):
            validate_pairing_report(self.path, self.summaries, self.job, (100, 150), 'rtl')
        self.path.write_text('{bad json', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '缺失或無效'):
            validate_pairing_report(self.path, self.summaries, self.job, (100, 150), 'rtl')
        self.report['state'] = 'analyzing'
        with self.assertRaisesRegex(ValueError, '頂層'):
            self.validate()

    def test_v1_and_v2_with_padding_offsets_are_rejected(self):
        self.report['version'] = 1
        with self.assertRaisesRegex(ValueError, '頂層'):
            self.validate()
        self.report['version'] = 2
        self.report['pages'][0]['geometry']['offset'] = [0, 0]
        with self.assertRaisesRegex(ValueError, '共同幾何'):
            self.validate()

    def test_report_must_match_geometry_resources_and_page_sides(self):
        for mutate, error in (
            (lambda: self.report.update(target=[101, 150]), '頂層'),
            (lambda: self.report['pages'][0].update(image='Images/missing.jpg'), '資源'),
            (lambda: self.report['pages'][0].update(side='left'), '頁側'),
            (lambda: self.report['pages'][1].update(tome=2), '卷號'),
            (lambda: self.report['pages'][1].update(viewport=[99, 120]), '尺寸'),
            (lambda: self.report['pages'][1]['geometry'].update(rendered_size=[89, 120]), '共同幾何'),
            (lambda: self.report['chapters'][0]['anchor'].update(score=float('nan')), 'anchor'),
        ):
            with self.subTest(error=error):
                self.setUp()
                mutate()
                with self.assertRaisesRegex(ValueError, error):
                    self.validate()

    def test_paired_content_may_have_different_widths_but_must_share_height(self):
        self.report['pages'][1]['image_size'] = [90, 119]
        self.report['pages'][1]['viewport'] = [90, 119]
        self.report['pages'][1]['geometry']['rendered_size'] = [90, 119]
        self.summaries[0]['body_layout'][1]['images'][0]['size'] = (90, 119)
        self.summaries[0]['body_layout'][1]['viewport'] = (90, 119)
        with self.assertRaisesRegex(ValueError, '相鄰'):
            self.validate()

    def test_pair_must_be_adjacent_in_same_volume_in_reading_order(self):
        self.summaries[0]['body_layout'].reverse()
        with self.assertRaisesRegex(ValueError, '正文頁'):
            self.validate()

    def test_single_is_centered_and_native_source_may_map_to_multiple_pages(self):
        self.job['archive_stats'] = [{'image_count': 1}]
        self.job['image_count'] = 1
        self.report['chapters'][0].update(source_ids=[1], mode='native', reason='wide_image', anchor=None)
        self.report['groups'] = [{'id': 1, 'chapter_id': 1, 'source_ids': [1], 'mode': 'native'}]
        self.report['pages'] = [
            {'source_id': 1, 'group_id': 1, 'image': f'Images/kcc-0001-kcc-{suffix}.jpg', 'xhtml': f'Text/{index}.xhtml',
             'tome': 1, 'side': 'center', 'image_size': [100, 150], 'viewport': None, 'geometry': None}
            for index, suffix in ((1, 'a'), (2, 'b'))
        ]
        self.summaries[0]['body_layout'] = [
            {'xhtml': f'Text/{index}.xhtml', 'images': [{'path': f'Images/kcc-0001-kcc-{suffix}.jpg', 'size': (100, 150)}],
             'viewport': None, 'side': 'center'}
            for index, suffix in ((1, 'a'), (2, 'b'))
        ]
        self.assertEqual(self.validate()['state'], 'completed')

        self.report['chapters'][0].update(mode='paired', reason='spreadshift')
        self.report['groups'][0]['mode'] = 'single'
        for page, layout in zip(self.report['pages'], self.summaries[0]['body_layout']):
            page['viewport'] = page['image_size']
            layout['viewport'] = tuple(page['image_size'])
        with self.assertRaisesRegex(ValueError, '一個置中頁'):
            self.validate()

    def test_single_pages_require_center_spread(self):
        self.job['archive_stats'] = [{'image_count': 1}]
        self.job['image_count'] = 1
        self.report['chapters'][0].update(source_ids=[1], mode='paired', spreadshift=False, reason='spreadshift', anchor=None)
        self.report['groups'] = [{'id': 1, 'chapter_id': 1, 'source_ids': [1], 'mode': 'single'}]
        page = self.report['pages'][0]
        page.update(source_id=1, image='Images/kcc-0001-kcc-x.jpg', xhtml='Text/1.xhtml', side='right',
                    image_size=[100, 150], viewport=None, geometry=None)
        self.report['pages'] = [page]
        self.summaries[0]['body_layout'] = [{'xhtml': 'Text/1.xhtml',
            'images': [{'path': 'Images/kcc-0001-kcc-x.jpg', 'size': (100, 150)}], 'viewport': None, 'side': 'right'}]
        with self.assertRaisesRegex(ValueError, 'single 頁'):
            self.validate()

    def test_spreadshift_true_even_count_keeps_first_and_last_as_singles(self):
        self.job['archive_stats'] = [{'image_count': 4}]
        self.job['image_count'] = 4
        self.job['spreadshift'] = True
        self.report['chapters'][0].update(source_ids=[1, 2, 3, 4], mode='paired', spreadshift=True,
                                          reason='spreadshift', anchor=None)
        self.report['groups'] = [
            {'id': 1, 'chapter_id': 1, 'source_ids': [1], 'mode': 'single'},
            {'id': 2, 'chapter_id': 1, 'source_ids': [2, 3], 'mode': 'paired'},
            {'id': 3, 'chapter_id': 1, 'source_ids': [4], 'mode': 'single'},
        ]
        self.report['pages'] = []
        self.summaries[0]['body_layout'] = []
        for source_id, group_id, side in ((1, 1, 'center'), (2, 2, 'right'), (3, 2, 'left'), (4, 3, 'center')):
            image = f'Images/kcc-{source_id:04}-kcc-x.jpg'
            xhtml = f'Text/{source_id}.xhtml'
            paired = group_id == 2
            self.summaries[0]['body_layout'].append({
                'xhtml': xhtml, 'images': [{'path': image, 'size': (100, 150)}],
                'viewport': (100, 150), 'side': side,
            })
            self.report['pages'].append({
                'source_id': source_id, 'group_id': group_id, 'image': image, 'xhtml': xhtml,
                'tome': 1, 'side': side, 'image_size': [100, 150],
                'viewport': [100, 150],
                'geometry': ({'crop_box': [0, 0, 100, 150], 'rendered_size': [100, 150]} if paired else None),
            })
        self.assertEqual(self.validate()['state'], 'completed')


if __name__ == '__main__':
    unittest.main()
