"""Convert configured comic batches through the pinned KCC command-line entry point."""

import argparse
from contextlib import contextmanager
from decimal import Decimal
from html import escape as html_escape
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
import zipfile

from .epub_outputs import discard_owned_outputs, inspect_owned_outputs, inspect_epub, publish_outputs
from .fat32_filename import sanitize_filename_for_fat32
from .space_preflight import MARGIN, SpacePlan, batch_requirements, calibrate_images, extracted_entry, require_space, same_volume, volume


ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 2_000_000_000
TARGET_MIB = 1850
IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.jp2', '.avif'}
FLAGS = ('manga_style', 'upscale', 'forcecolor', 'eraserainbow')


def check_keys(value, required, optional=(), label='設定'):
    if not isinstance(value, dict):
        raise ValueError(f'{label} 必須是 table')
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise ValueError(f'{label} 缺少欄位 {sorted(missing)}；未知欄位 {sorted(unknown)}')


def safe_stem(title):
    stem = sanitize_filename_for_fat32(title + '.kepub.epub')[:-len('.kepub.epub')]
    # Reserve space for volume numbers as well as the compound extension.
    while len(stem.encode('utf-16-le')) // 2 > 230:
        stem = stem[:-1]
    stem = stem.rstrip(' .') or '_'
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', stem, re.I):
        raise ValueError(f'輸出名稱為 Windows 保留名稱：{title}')
    return stem


def chapter_key(path):
    match = re.match(r'^(\d+(?:\.\d+)?)(?:\s|$)', path.stem)
    if not match:
        raise ValueError(f'CBZ 檔名必須以十進位章號開頭：{path.name}')
    return Decimal(match[1]), path.name.casefold(), path.name


def inspect_source(path):
    with zipfile.ZipFile(path) as archive:
        names = set()
        count = 0
        expanded = 0
        comic_info_bytes = 0
        for item in archive.infolist():
            name = item.filename.replace('\\', '/')
            relative = PurePosixPath(name)
            if (relative.is_absolute() or '..' in relative.parts or
                    re.match(r'^[A-Za-z]:', name) or name.casefold() in names or
                    any(any(ord(c) < 32 or c in ':*?"<>|' for c in part) or part.endswith((' ', '.'))
                        for part in relative.parts) or
                    (item.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError(f'CBZ 含不安全或重複路徑：{path.name}: {name}')
            names.add(name.casefold())
            if item.flag_bits & 1:
                raise ValueError(f'不支援加密 CBZ：{path.name}')
            if not extracted_entry(item):
                continue
            expanded += item.file_size
            if relative.name.startswith('._'):
                continue
            if relative.name.casefold() == 'comicinfo.xml':
                comic_info_bytes += item.file_size
            if relative.suffix.lower() in IMAGE_TYPES:
                if item.file_size == 0:
                    raise ValueError(f'CBZ 含空圖片：{path.name}: {name}')
                count += 1
        if not count:
            raise ValueError(f'CBZ 沒有支援的圖片：{path.name}')
        return {'image_count': count, 'expanded': expanded, 'comic_info_bytes': comic_info_bytes}


def load_config(config_path):
    config_path = Path(config_path).resolve()
    with config_path.open('rb') as handle:
        config = tomllib.load(handle)
    check_keys(config, ('batch', 'paths', 'conversion'), ('publication',))
    publication = config.setdefault('publication', {'keep_backup': True})
    check_keys(publication, ('keep_backup',), label='publication')
    if type(publication['keep_backup']) is not bool:
        raise ValueError('publication.keep_backup 必須是 true 或 false')
    check_keys(config['batch'], ('comics',), label='batch')
    check_keys(config['paths'], ('input_root', 'output_dir'), ('work_root',), 'paths')
    check_keys(config['conversion'], ('profile', 'cropping', *FLAGS), label='conversion')
    conversion = config['conversion']
    if not isinstance(conversion['profile'], str) or not conversion['profile'].strip():
        raise ValueError('profile 必須是非空字串')
    if type(conversion['cropping']) is not int or conversion['cropping'] not in (0, 1, 2):
        raise ValueError('cropping 必須是 0、1 或 2')
    for flag in FLAGS:
        if type(conversion[flag]) is not bool:
            raise ValueError(f'{flag} 必須是 true 或 false')

    paths = {}
    for name, value in config['paths'].items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'paths.{name} 必須是非空路徑字串')
        path = Path(value).expanduser()
        paths[name] = (config_path.parent / path).resolve()
    paths.setdefault('work_root', ROOT / '.cbz2kepub-work')
    comics = config['batch']['comics']
    if not isinstance(comics, list):
        raise ValueError('batch.comics 必須是陣列')
    jobs = []
    stems = []
    for comic in comics:
        check_keys(comic, ('title', 'spreadshift'), ('chapter_pairing',), 'comics 項目')
        title = comic['title']
        if (not isinstance(title, str) or not title.strip() or title in ('.', '..') or
                any(c in title for c in '/\\\x00') or Path(title).name != title):
            raise ValueError('title 必須是單一作品資料夾名稱')
        if type(comic['spreadshift']) is not bool:
            raise ValueError(f'{title}: spreadshift 必須是 true 或 false')
        if type(comic.get('chapter_pairing', False)) is not bool:
            raise ValueError(f'{title}: chapter_pairing 必須是 true 或 false')
        comic.setdefault('chapter_pairing', False)
        stem = safe_stem(title)
        for previous in stems:
            a, b = previous.casefold(), stem.casefold()
            if a == b or re.fullmatch(re.escape(a) + r' [1-9]\d*', b) or re.fullmatch(re.escape(b) + r' [1-9]\d*', a):
                raise ValueError(f'作品輸出名稱或卷號可能碰撞：{previous} / {stem}')
        stems.append(stem)
        source = (paths['input_root'] / title / 'compressed').resolve()
        if not source.is_relative_to(paths['input_root']) or not source.is_dir():
            raise ValueError(f'找不到來源資料夾，或來源超出 input_root：{source}')
        sources = sorted((p for p in source.iterdir() if p.suffix.lower() == '.cbz' and p.is_file()), key=chapter_key)
        if not sources:
            raise ValueError(f'找不到 CBZ：{source}')
        archive_stats = [inspect_source(path) for path in sources]
        image_count = sum(item['image_count'] for item in archive_stats)
        metadata = {}
        if (source / 'info.json').exists():
            with (source / 'info.json').open(encoding='utf-8-sig') as handle:
                metadata = json.load(handle)
            if not isinstance(metadata, dict):
                raise ValueError(f'{title}: info.json 必須是 JSON object')
            for key in ('title', 'author', 'description'):
                if key in metadata and not isinstance(metadata[key], str):
                    raise ValueError(f'{title}: info.json.{key} 必須是字串')
        cover = source / 'cover.jpg'
        if cover.exists():
            from PIL import Image
            with Image.open(cover) as image:
                image.verify()
        jobs.append(dict(comic, stem=stem, sources=sources, source=source,
                         image_count=image_count, metadata=metadata,
                         archive_stats=archive_stats,
                         cover=cover if cover.exists() else None))
    return dict(paths=paths, conversion=conversion, publication=publication, jobs=jobs)


def comic_info_bytes(job):
    root = ET.Element('ComicInfo')
    for field, value in (('Series', job['title']), ('Title', job['title']),
                         ('Writer', job['metadata'].get('author')),
                         ('Summary', job['metadata'].get('description'))):
        if value:
            # KCC v12.1.4 unescapes parsed text again; compensate only for our generated XML.
            ET.SubElement(root, field).text = escape(value)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def write_comic_info(job, staging):
    (staging / 'ComicInfo.xml').write_bytes(comic_info_bytes(job))


def build_command(job, conversion, staged_sources, output_dir, target_mib=TARGET_MIB):
    command = [sys.executable, str(ROOT / 'kcc' / 'kcc-c2e.py'),
               '--profile', conversion['profile'], '--format', 'EPUB',
               '--title', job['title'], '--output', str(output_dir),
               '--cropping', str(conversion['cropping']), '--ts', str(target_mib), '--batchsplit', '1']
    for flag in FLAGS:
        if conversion[flag]:
            command.append('--' + flag.replace('_', '-'))
    if job['spreadshift']:
        command.append('--spreadshift')
    if job.get('chapter_pairing', False):
        command.append('--chapter-pairing')
    if job['metadata'].get('author'):
        command.extend(('--author', job['metadata']['author']))
    if len(staged_sources) > 1:
        command.append('--filefusion')
    command.extend(str(path) for path in staged_sources)
    if os.name == 'nt' and len(subprocess.list2cmdline(command).encode('utf-16-le')) // 2 >= 30_000:
        raise ValueError('本次來源超過 Windows 命令列長度；請縮短來源檔名或分批選取，不會默默改變 Fusion 行為')
    return command


def ordered_outputs(output_dir):
    files = list(output_dir.glob('*.epub'))
    if len(files) == 1:
        return files
    numbered = {}
    for path in files:
        match = re.search(r' ([1-9]\d*)\.kepub\.epub$', path.name)
        if not match or int(match[1]) in numbered:
            raise ValueError(f'無法識別 KCC 分卷檔名：{path.name}')
        numbered[int(match[1])] = path
    if not numbered or sorted(numbered) != list(range(1, len(files) + 1)):
        raise ValueError('KCC 沒有輸出，或分卷序號不完整')
    return [numbered[index] for index in sorted(numbered)]


def run_kcc(command, work, environment):
    with (work / 'kcc.log').open('w', encoding='utf-8') as log:
        with subprocess.Popen(command, cwd=work, env=environment, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1) as process:
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line, end='', flush=True)
            if process.wait():
                raise subprocess.CalledProcessError(process.returncode, command)


def validate_book(job, summaries):
    if any(not result['has_cover_page'] for result in summaries):
        raise ValueError('成品缺少封面閱讀頁')
    expected = [path.stem for path in job['sources']]
    if len(expected) > 1:
        actual = []
        for summary in summaries:
            for title in summary['chapter_titles']:
                if not actual or actual[-1] != title:
                    actual.append(title)
        if actual != expected:
            raise ValueError(f'章節導航與來源不符：預期 {expected}，實際 {actual}')
    ids = set()
    resources = set()
    previous_id = 0
    for summary in summaries:
        for name in summary['body_images']:
            base = PurePosixPath(name).name
            match = re.fullmatch(r'kcc-(\d+)-kcc-[^.]+\.[^.]+', base)
            if not match:
                raise ValueError(f'未知的固定版 KCC 成品圖片命名：{base}')
            if base in resources:
                raise ValueError(f'跨卷重複正文圖片：{base}')
            resources.add(base)
            source_id = int(match[1])
            if source_id < previous_id:
                raise ValueError('成品正文頁序與來源順序不符')
            previous_id = source_id
            ids.add(source_id)
    if ids != set(range(1, job['image_count'] + 1)):
        raise ValueError('成品來源圖片序號有缺漏或額外頁面')


def validate_pairing_report(path, summaries, job, target, direction):
    """Validate the v2 report against the completed EPUBs before publication."""
    try:
        report = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'章內配對報告缺失或無效：{path}') from exc
    if (not isinstance(report, dict) or type(report.get('version')) is not int or report.get('version') != 2
            or report.get('state') != 'completed'
            or report.get('target') != list(target) or report.get('direction') != direction
            or not all(isinstance(report.get(key), list) for key in ('chapters', 'groups', 'pages'))):
        raise ValueError('章內配對報告頂層欄位與本次轉檔不符')
    chapters, groups, pages = report['chapters'], report['groups'], report['pages']
    if len(chapters) != len(job['sources']):
        raise ValueError('報告章節數與 CBZ 來源數不符')
    chapter_by_id = {}
    next_source = 1
    for index, chapter in enumerate(chapters, 1):
        if (not isinstance(chapter, dict) or type(chapter.get('id')) is not int or chapter.get('id') != index
                or not isinstance(chapter.get('title'), str) or chapter.get('mode') not in ('paired', 'native')
                or type(chapter.get('spreadshift')) is not bool
                or chapter.get('reason') not in ('detected', 'spreadshift', 'wide_image')):
            raise ValueError('章內配對報告含無效章節')
        if chapter['title'] != job['sources'][index - 1].stem:
            raise ValueError('報告章節名稱與來源不符')
        if ((chapter['mode'] == 'native') != (chapter['reason'] == 'wide_image')
                or (chapter['reason'] == 'detected' and chapter['mode'] != 'paired')):
            raise ValueError('報告章節 mode/reason 不一致')
        if chapter['reason'] != 'detected' and chapter['spreadshift'] != job['spreadshift']:
            raise ValueError('報告章節 spreadshift 與作品設定不符')
        source_ids = chapter.get('source_ids')
        if (not isinstance(source_ids, list) or not source_ids
                or any(type(source_id) is not int for source_id in source_ids)
                or source_ids != list(range(next_source, next_source + len(source_ids)))):
            raise ValueError('報告章節來源 ID 不連續或順序錯誤')
        anchor = chapter.get('anchor')
        if chapter['reason'] == 'detected':
            if (not isinstance(anchor, dict) or anchor.get('source_ids') not in
                    [chapter['source_ids'][index:index + 2] for index in range(len(chapter['source_ids']) - 1)]
                    or type(anchor.get('score')) not in (int, float) or not math.isfinite(anchor['score'])):
                raise ValueError('報告偵測章節缺少有效 anchor')
            method, score = anchor.get('method', 'rgb'), anchor['score']
            if not ((method == 'rgb' and 0 <= score < 30)
                    or (method == 'strip' and 0.93 <= score <= 1)
                    or (method == 'strip_fallback' and 0.82 <= score <= 1)):
                raise ValueError('報告 anchor 的方法或分數無效')
            if chapter['spreadshift'] != ((anchor['source_ids'][0] - chapter['source_ids'][0]) % 2 == 1):
                raise ValueError('偵測 anchor 起點與 spreadshift 不符')
        elif anchor is not None:
            raise ValueError('非偵測章節 anchor 必須為 null')
        next_source += len(source_ids)
        chapter_by_id[index] = chapter
    counts = [item['image_count'] for item in (job.get('archive_stats') or [])]
    if counts and [chapter['source_ids'] for chapter in chapters] != _source_ranges(counts):
        raise ValueError('報告章節來源範圍與 CBZ 章節圖片數不符')
    if next_source != job['image_count'] + 1:
        raise ValueError('報告章節未完整涵蓋來源圖片')
    group_by_id = {}
    for index, group in enumerate(groups, 1):
        if (not isinstance(group, dict) or type(group.get('id')) is not int or group.get('id') != index
                or type(group.get('chapter_id')) is not int or group['chapter_id'] not in chapter_by_id
                or group.get('mode') not in ('paired', 'single', 'native')):
            raise ValueError('章內配對報告含無效組')
        source_ids = group.get('source_ids')
        chapter = chapter_by_id[group['chapter_id']]
        if (not isinstance(source_ids, list) or not source_ids
                or any(type(source_id) is not int or source_id not in chapter['source_ids'] for source_id in source_ids)
                or len(source_ids) != len(set(source_ids))
                or source_ids != sorted(source_ids)
                or (group['mode'] == 'paired' and len(source_ids) != 2)
                or (group['mode'] != 'paired' and len(source_ids) != 1)
                or (chapter['mode'] == 'native') != (group['mode'] == 'native')):
            raise ValueError('報告組來源 ID 或模式無效')
        group_by_id[index] = group
    actual = []
    for tome, summary in enumerate(summaries, 1):
        for item in summary['body_layout']:
            for image in item['images']:
                match = re.search(r'kcc-(\d+)-kcc-', PurePosixPath(image['path']).name)
                if match:
                    actual.append((tome, item['xhtml'], image['path'], int(match[1]), image['size'], item['viewport'], item['side']))
    actual_map = {(row[1], row[2]): (position, row) for position, row in enumerate(actual)}
    page_keys = []
    page_positions = []
    group_pages = {group_id: [] for group_id in group_by_id}
    for page in pages:
        if (not isinstance(page, dict) or type(page.get('source_id')) is not int
                or type(page.get('group_id')) is not int or page['group_id'] not in group_by_id
                or type(page.get('tome')) is not int or page.get('side') not in ('left', 'right', 'center')):
            raise ValueError('章內配對報告含無效正文頁')
        group = group_by_id[page['group_id']]
        actual_entry = actual_map.get((page.get('xhtml'), page.get('image')))
        if actual_entry is None:
            raise ValueError('報告正文頁與 EPUB 資源不符')
        position, row = actual_entry
        if row[0] != page['tome'] or row[3] != page['source_id']:
            raise ValueError('報告正文頁與 EPUB 資源／卷號／來源 ID 不符')
        if (page.get('image_size') != list(row[4]) or page.get('viewport') != (list(row[5]) if row[5] else None)
                or page['side'] != row[6] or page['source_id'] not in group['source_ids']):
            raise ValueError('報告尺寸、viewport、頁側或組來源與 EPUB 不符')
        if group['mode'] == 'paired':
            geometry = page.get('geometry')
            if (not isinstance(geometry, dict) or set(geometry) != {'crop_box', 'rendered_size'}
                    or not isinstance(geometry.get('crop_box'), list) or len(geometry['crop_box']) != 4
                    or not isinstance(geometry.get('rendered_size'), list) or len(geometry['rendered_size']) != 2
                    or any(type(value) is not int for value in geometry['crop_box'])
                    or not (geometry['crop_box'][0] >= 0 and geometry['crop_box'][1] >= 0
                            and geometry['crop_box'][2] > geometry['crop_box'][0]
                            and geometry['crop_box'][3] > geometry['crop_box'][1])
                    or any(type(value) is not int for value in geometry['rendered_size'])
                    or not (0 < geometry['rendered_size'][0] <= target[0]
                            and 0 < geometry['rendered_size'][1] <= target[1])
                    or page['image_size'] != geometry['rendered_size']
                    or page['viewport'] != geometry['rendered_size']):
                raise ValueError('paired 報告缺少有效共同幾何')
        elif page.get('geometry') is not None:
            raise ValueError('非 paired 頁 geometry 必須為 null')
        page_keys.append((page['xhtml'], page['image']))
        page_positions.append(position)
        group_pages[page['group_id']].append((page, row, position))
    if (len(page_keys) != len(set(page_keys)) or set(page_keys) != set(actual_map)
            or page_positions != sorted(page_positions)):
        raise ValueError('報告正文頁未與 EPUB 資源一對一完整對應')
    for group_id, entries in group_pages.items():
        group = group_by_id[group_id]
        if not entries:
            raise ValueError('報告組沒有正文頁')
        if group['mode'] == 'paired' and len(entries) != 2:
            raise ValueError('paired 組必須包含兩個正文頁')
        source_order = [entry[0]['source_id'] for entry in entries]
        if group['mode'] == 'paired':
            expected_sides = ('right', 'left') if direction == 'rtl' else ('left', 'right')
            first_geometry, second_geometry = (entry[0]['geometry'] for entry in entries)
            first_size, second_size = first_geometry['rendered_size'], second_geometry['rendered_size']
            if (source_order != group['source_ids'] or entries[0][1][0] != entries[1][1][0]
                    or entries[1][2] != entries[0][2] + 1
                    or tuple(entry[0]['side'] for entry in entries) != expected_sides
                    or first_size[1] != second_size[1]):
                raise ValueError('paired 組必須按來源順序相鄰且位於同卷')
        if sorted({entry[0]['source_id'] for entry in entries}) != group['source_ids']:
            raise ValueError('報告組未完整涵蓋其來源 ID')
        if group['mode'] == 'single':
            page = entries[0][0]
            if (len(entries) != 1 or page['side'] != 'center' or page['image_size'] != page['viewport']
                    or not (0 < page['image_size'][0] <= target[0] and 0 < page['image_size'][1] <= target[1])):
                raise ValueError('single 頁須對應一個置中頁，且 viewport 等於 profile 內圖片尺寸')
    for chapter_id, chapter in chapter_by_id.items():
        chapter_groups = [group for group in groups if group['chapter_id'] == chapter_id]
        chapter_sources = [source_id for group in chapter_groups for source_id in group['source_ids']]
        if sorted(chapter_sources) != chapter['source_ids']:
            raise ValueError('章節組未完整且唯一涵蓋章來源 ID')
        expected = []
        source_ids = chapter['source_ids']
        if chapter['mode'] == 'native':
            expected = [(source_id, 'native') for source_id in source_ids]
        else:
            cursor = 0
            if chapter['spreadshift']:
                expected.append((source_ids[0], 'single'))
                cursor = 1
            while cursor + 1 < len(source_ids):
                expected.append((source_ids[cursor:cursor + 2], 'paired'))
                cursor += 2
            if cursor < len(source_ids):
                expected.append((source_ids[cursor], 'single'))
        if [(group['source_ids'] if len(group['source_ids']) == 2 else group['source_ids'][0], group['mode'])
                for group in chapter_groups] != expected:
            raise ValueError('報告組與章節 spreadshift 分組不符')
    return report


def _source_ranges(counts):
    start = 1
    ranges = []
    for count in counts:
        ranges.append(list(range(start, start + count)))
        start += count
    return ranges


@contextmanager
def output_lock(output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    lock = output_dir / '.cbz2kepub.lock'
    try:
        handle = lock.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise ValueError(f'輸出目錄已有工作鎖：{lock}；確認沒有轉檔程序後再移除殘留鎖') from exc
    try:
        with handle:
            json.dump({'pid': os.getpid()}, handle)
        yield
    finally:
        lock.unlink()


def prepare_space(job, config, target_mib):
    conversion = config['conversion']
    chapter_pairing = job.get('chapter_pairing', False)
    key = (*sorted(conversion.items()), target_mib, chapter_pairing)
    if job.get('_space_key') == key:
        return job['_space_data']
    sys.path.insert(0, str(ROOT / 'kcc'))
    try:
        from kindlecomicconverter.image import ProfileData
        if chapter_pairing and conversion['profile'] not in ProfileData.ProfilesKobo:
            raise ValueError('章內配對需要 Kobo profile')
        target = ProfileData.Profiles[conversion['profile']][1]
        if min(target) <= 0:
            raise ValueError('容量預檢需要有固定解析度的 profile')
        quality = 90 if conversion['profile'].startswith('KS') or conversion['profile'] == 'KCS' else 85
        images = calibrate_images(job['sources'], target, quality, upscale=conversion['upscale'],
                                  forcecolor=conversion['forcecolor'], eraserainbow=conversion['eraserainbow'],
                                  chapter_pairing=chapter_pairing)
    except KeyError as exc:
        raise ValueError(f'未知的 KCC profile：{conversion["profile"]}') from exc
    finally:
        sys.path.pop(0)
    stats = job.get('archive_stats') or [inspect_source(path) for path in job['sources']]
    # Title appears in OPF/collection and NAV/NCX titles/labels; summary/author once per volume.
    metadata_bytes = 7 * len(html_escape(job['title']).encode('utf-8')) + sum(
        len(html_escape(job['metadata'].get(field, '')).encode('utf-8')) for field in ('author', 'description'))
    if len(job['sources']) == 1:
        metadata_bytes += stats[0]['comic_info_bytes']
    data = dict(version=1, work=str(config['paths']['work_root']), output_dir=str(config['paths']['output_dir']), target=list(target),
                target_bytes=int(target_mib * 1024 * 1024), images=images, chapter_pairing=chapter_pairing,
                metadata_bytes=metadata_bytes, sidecar_bytes=len(comic_info_bytes(job)),
                expanded=sum(item['expanded'] for item in stats),
                sources=[dict(path=str(path), expanded=item['expanded']) for path, item in zip(job['sources'], stats)],
                cover_bytes=job['cover'].stat().st_size if job['cover'] else 0)
    job['_space_key'], job['_space_data'] = key, data
    print(f'容量估算 {job["title"]}：解壓已知 {data["expanded"]:,} bytes；'
          f'圖片輸出估計 {sum(images["predictions"]):,} bytes'
          f'（{images["samples"]} 張來源記憶體 JPEG 試編碼，品質 {quality}，'
          f'按單頁／拆頁及來源大小分層加權，＋25% 估計餘裕；非大小上界）', flush=True)
    return data


def preflight_batch(config, jobs, target_mib=TARGET_MIB, copied_override=None):
    keep = config.get('publication', {}).get('keep_backup', True)
    output, work = config['paths']['output_dir'], config['paths']['work_root']
    rows, locations = [], {volume(output): output, volume(work): work}
    for index, job in enumerate(jobs):
        data = prepare_space(job, config, target_mib)
        _, old = inspect_owned_outputs(output, job['stem'], job['title'])
        if index == 0 and copied_override is not None:
            copied = copied_override
        else:
            copied = 0
            work.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='c2k-link-check-', dir=work) as probe:
                for number, path in enumerate(job['sources']):
                    link = Path(probe) / str(number)
                    try:
                        os.link(path, link)
                    except OSError:
                        copied += path.stat().st_size
                    else:
                        link.unlink()
        peak, predicted_output = SpacePlan(data).initial_budget(copied)
        rows.append((volume(work), volume(output), peak, predicted_output,
                     sum(path.stat().st_size for path in old), keep))
    for device, needed in batch_requirements(rows, margin=MARGIN).items():
        free = require_space(locations[device], needed, '整批容量預檢（含圖片估計）', margin=0)
        print(f'整批容量預檢：{locations[device]} 可用 {free:,} bytes；'
              f'估計最大新增 {needed:,} bytes（已含後續操作的 32 MiB 餘裕）', flush=True)


def convert_comic(job, config, target_mib=TARGET_MIB):
    work_root = config['paths']['work_root']
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='c2k-', dir=work_root))
    print(f'處理 {job["title"]}：{len(job["sources"])} 個 CBZ；工作目錄 {work}', flush=True)
    try:
        staging = work / 's'
        output = work / 'o'
        temporary = work / 't'
        for folder in (staging, output, temporary):
            folder.mkdir()
        data = dict(prepare_space(job, config, target_mib), work=str(work))
        data['sources'] = [dict(item, path=str(staging / source.name))
                           for item, source in zip(data['sources'], job['sources'])]
        plan = SpacePlan(data)
        plan_path = work / 'space-plan.json'
        plan_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        pending = []
        sources = []
        for path in job['sources']:
            destination = staging / path.name
            try:
                os.link(path, destination)
            except OSError:
                pending.append((path, destination))
            # KCC's CLI launcher changes cwd to its own source directory.
            sources.append(destination)
        command = build_command(job, config['conversion'], sources, output, target_mib)
        copied_bytes = sum(path.stat().st_size for path, _ in pending)
        print(f'來源 staging：{len(sources) - len(pending)} 個硬連結；'
              f'{len(pending)} 個需複製，共 {copied_bytes:,} bytes', flush=True)
        remaining = config.get('_remaining_jobs', [job])
        preflight_batch(config, remaining, target_mib, copied_override=copied_bytes)
        keep_backup = config.get('publication', {}).get('keep_backup', True)
        if not keep_backup:
            print(f'不備份：刪除本工具擁有的 {job["title"]} 舊成品；轉檔失敗無法還原舊卷。', flush=True)
            discard_owned_outputs(config['paths']['output_dir'], job['stem'], job['title'])
            # Reclaimed bytes may differ from file sizes (hardlinks/open handles).
            peak, _ = plan.initial_budget(copied_bytes)
            require_space(work, peak, '刪除舊卷後的實際 work 容量預檢')
            if not same_volume(work, config['paths']['output_dir']):
                require_space(config['paths']['output_dir'], plan.initial_budget(copied_bytes)[1], '刪除舊卷後的 output 容量預檢')
        for path, destination in pending:
            # Each preceding copy now consumes real free space; count only the remainder.
            peak, _ = plan.initial_budget(copied_bytes)
            require_space(work, peak, 'CBZ staging 複製及剩餘流程預檢')
            shutil.copy2(path, destination)
            copied_bytes -= path.stat().st_size
        if job['cover']:
            covers = staging / 'Covers'
            covers.mkdir()
            shutil.copy2(job['cover'], covers / 'cover.jpg')
        write_comic_info(job, staging)
        environment = dict(os.environ, TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary),
                           PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1', QT_QPA_PLATFORM='offscreen',
                           CBZ2KEPUB_SPACE_PLAN=str(plan_path),
                           CBZ2KEPUB_SPACE_MODULE=str(Path(__file__).resolve().with_name('space_preflight.py')))
        pairing_report = work / 'pairing.json'
        if job.get('chapter_pairing', False):
            environment['CBZ2KEPUB_PAIRING_REPORT'] = str(pairing_report)
        else:
            environment.pop('CBZ2KEPUB_PAIRING_REPORT', None)
        run_kcc(command, work, environment)
        files = ordered_outputs(output)
        summaries = []
        renamed = []
        for index, path in enumerate(files, 1):
            summary = inspect_epub(path, MAX_BYTES)
            summaries.append(summary)
            suffix = f' {index}' if len(files) > 1 else ''
            destination = output / f'{job["stem"]}{suffix}.kepub.epub'
            if path != destination:
                path.rename(destination)
            renamed.append(destination)
        validate_book(job, summaries)
        if job.get('chapter_pairing', False):
            direction = 'rtl' if config['conversion']['manga_style'] else 'ltr'
            validate_pairing_report(pairing_report, summaries, job, data['target'], direction)
        result = publish_outputs(renamed, config['paths']['output_dir'], job['stem'], work_title=job['title'], keep_backup=keep_backup)
        print(f'完成 {job["title"]}：{len(result)} 卷，{sum(item["body_pages"] for item in summaries)} 個正文頁', flush=True)
    except BaseException:
        print(f'處理失敗，已保留工作目錄與 kcc.log：{work}', file=sys.stderr, flush=True)
        raise
    shutil.rmtree(work)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config.toml')
    arguments = parser.parse_args(argv)
    try:
        config = load_config(arguments.config)
        if not config['jobs']:
            print('未選擇漫畫，沒有執行轉檔。')
            return 0
        if not (ROOT / 'kcc' / 'kcc-c2e.py').is_file():
            raise ValueError('缺少 KCC submodule，請先執行 git submodule update --init --recursive')
        with output_lock(config['paths']['output_dir']):
            preflight_batch(config, config['jobs'])
            for index, job in enumerate(config['jobs']):
                config['_remaining_jobs'] = config['jobs'][index:]
                convert_comic(job, config)
        return 0
    except (ValueError, OSError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        print(f'錯誤：{exc}', file=sys.stderr)
        return 1
