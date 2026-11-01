"""Explicit real-data acceptance; sources stay untouched and outputs remain local."""

import argparse
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cbz2kepub.epub_outputs import COVER_ID, NS, _package, _resolve, inspect_epub
from cbz2kepub import converter


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def verify_cover_and_metadata(path, summary, job, conversion, index, volume_count, case):
    number = str(index).zfill(2) if volume_count > 9 else str(index)
    expected_title = job['title'] + (f' [{number}/{volume_count}]' if volume_count > 1 else '')
    assert summary['title'] == expected_title
    assert summary['series'] == job['title'] and summary['series_index'] == str(index)
    checks = {'title': summary['title'], 'authors': summary['authors'], 'series': summary['series'],
              'series_index': summary['series_index'], 'has_cover_page': summary['has_cover_page']}
    metadata = job['metadata']
    if metadata.get('author'):
        assert summary['authors'] == [metadata['author']]
        checks['author_exact_match'] = True
    if 'description' in metadata:
        assert summary['description'] == metadata['description']
        checks['description_exact_match'] = True
        checks['description_characters'] = len(summary['description'])
        checks['description_sha256'] = hashlib.sha256(summary['description'].encode('utf-8')).hexdigest()
    with ZipFile(path) as archive:
        opf_path, package = _package(archive)
        manifest = {item.get('id'): item for item in package.find('opf:manifest', NS)}
        first = package.find('opf:spine/opf:itemref', NS)
        assert first.get('idref') == COVER_ID
        assert first.get('properties') == 'rendition:page-spread-center'
        cover_path = _resolve(opf_path, manifest[COVER_ID].get('href'))
        document = ET.fromstring(archive.read(cover_path))
        svg_image = document.find('.//{http://www.w3.org/2000/svg}image')
        image_path = _resolve(cover_path, svg_image.get('href'))
        image_bytes = archive.read(image_path)
        with Image.open(BytesIO(image_bytes)) as image:
            dimensions = image.size
        viewport = document.find('html:head/html:meta[@name="viewport"]', NS).get('content')
        assert viewport == f'width={dimensions[0]}, height={dimensions[1]}'
        cover_item = next(item for item in manifest.values() if 'cover-image' in item.get('properties', '').split())
        assert image_path == _resolve(opf_path, cover_item.get('href'))
        assert svg_image.get('href') == svg_image.get('{http://www.w3.org/1999/xlink}href')
        (case / f'embedded-cover-{index}.jpg').write_bytes(image_bytes)
        checks.update(cover_page=cover_path, cover_image=image_path, cover_dimensions=list(dimensions),
                      cover_page_first=True, cover_page_centered=True, cover_image_sha256=hashlib.sha256(image_bytes).hexdigest())
        if job['cover'] is not None and volume_count == 1:
            assert conversion['profile'] == 'KoLC', 'This real-data cover reference uses the KoLC profile'
            with Image.open(job['cover']) as original:
                expected = ImageOps.autocontrast(original.convert('RGB'), preserve_tone=True)
            if not conversion['forcecolor']:
                expected = expected.convert('L')
            expected.thumbnail((1264, 1680), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            expected.save(buffer, 'JPEG', optimize=1, quality=85)
            assert image_bytes == buffer.getvalue(), 'Embedded cover differs from independently prepared KCC cover reference'
            checks['custom_cover_exact_match'] = True
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', required=True, type=Path)
    parser.add_argument('--results-root', required=True, type=Path)
    parser.add_argument('--limits', nargs='+', type=int, default=[15, 59, 60, 70])
    arguments = parser.parse_args()
    source = arguments.source_dir.resolve()
    results_root = arguments.results_root.resolve()
    results_root.mkdir(parents=True, exist_ok=True)
    files = sorted(source.glob('*.cbz'), key=converter.chapter_key)
    if not files or any(limit < 1 or limit > len(files) for limit in arguments.limits):
        raise ValueError('Acceptance limits must refer to existing nonempty source sets')
    original_hashes = {path.name: digest(path) for path in files}
    extra_hashes = {name: digest(source / name) for name in ('cover.jpg', 'info.json') if (source / name).is_file()}
    report = {'status': 'running', 'source_cbz_count': len(files), 'cases': []}
    report_path = results_root / 'acceptance.json'
    try:
        for limit in arguments.limits:
            started = time.perf_counter()
            case = results_root / f'chapters-{limit}'
            facade = case / 'input' / source.name / 'compressed'
            facade.mkdir(parents=True)
            for path in files[:limit]:
                try:
                    os.link(path, facade / path.name)
                except OSError:
                    shutil.copy2(path, facade / path.name)
            skipped = []
            for name in ('cover.jpg', 'info.json'):
                original = source / name
                if original.is_file() and original.stat().st_size:
                    shutil.copy2(original, facade / name)
                elif original.exists():
                    skipped.append(name)
            config_path = case / 'config.toml'
            text = (converter.ROOT / 'config.example.toml').read_text(encoding='utf-8')
            text = text.replace('範例漫畫 A', source.name)
            config_path.write_text(text, encoding='utf-8')
            config = converter.load_config(config_path)
            with converter.output_lock(config['paths']['output_dir']):
                outputs = converter.convert_comic(config['jobs'][0], config)
            summaries = [inspect_epub(path) for path in outputs]
            converter.validate_book(config['jobs'][0], summaries)
            cover_metadata = [verify_cover_and_metadata(path, summary, config['jobs'][0], config['conversion'], index, len(outputs), case)
                              for index, (path, summary) in enumerate(zip(outputs, summaries), 1)]
            hashes_unchanged = all(digest(path) == original_hashes[path.name] for path in files[:limit])
            extras_unchanged = all(digest(source / name) == value for name, value in extra_hashes.items())
            if not hashes_unchanged or not extras_unchanged:
                raise ValueError('Source archive SHA256 changed')
            report['cases'].append({
                'chapters': limit, 'source_images': config['jobs'][0]['image_count'],
                'body_pages': sum(item['body_pages'] for item in summaries),
                'volumes': len(outputs), 'sizes': [item['size'] for item in summaries],
                'navigation_entries': [len(item['chapter_titles']) for item in summaries],
                'source_sha256_unchanged': hashes_unchanged,
                'source_cover_metadata_sha256_unchanged': extras_unchanged,
                'cover_metadata_checks': cover_metadata,
                'skipped_empty_source_placeholders': skipped,
                'elapsed_seconds': round(time.perf_counter() - started, 3),
                'outputs': [str(path) for path in outputs],
            })
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        report['status'] = 'completed'
    except BaseException as exc:
        report['status'] = 'failed'
        report['error'] = str(exc)
        raise
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
