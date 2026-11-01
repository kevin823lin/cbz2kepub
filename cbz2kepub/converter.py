"""Convert configured comic batches through the pinned KCC command-line entry point."""

import argparse
from contextlib import contextmanager
from decimal import Decimal
from html import escape as html_escape
import json
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
    paths.setdefault('work_root', paths['input_root'].parent / '.cbz2kepub-work')
    comics = config['batch']['comics']
    if not isinstance(comics, list):
        raise ValueError('batch.comics 必須是陣列')
    jobs = []
    stems = []
    for comic in comics:
        check_keys(comic, ('title', 'spreadshift'), label='comics 項目')
        title = comic['title']
        if (not isinstance(title, str) or not title.strip() or title in ('.', '..') or
                any(c in title for c in '/\\\x00') or Path(title).name != title):
            raise ValueError('title 必須是單一作品資料夾名稱')
        if type(comic['spreadshift']) is not bool:
            raise ValueError(f'{title}: spreadshift 必須是 true 或 false')
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
    key = (config['conversion']['profile'], config['conversion']['forcecolor'], target_mib)
    if job.get('_space_key') == key:
        return job['_space_data']
    sys.path.insert(0, str(ROOT / 'kcc'))
    try:
        from kindlecomicconverter.image import ProfileData
        target = ProfileData.Profiles[key[0]][1]
    except KeyError as exc:
        raise ValueError(f'未知的 KCC profile：{key[0]}') from exc
    finally:
        sys.path.pop(0)
    if min(target) <= 0:
        raise ValueError('容量預檢需要有固定解析度的 profile')
    quality = 90 if key[0].startswith('KS') or key[0] == 'KCS' else 85
    images = calibrate_images(job['sources'], target, quality)
    stats = job.get('archive_stats') or [inspect_source(path) for path in job['sources']]
    # Title appears in OPF/collection and NAV/NCX titles/labels; summary/author once per volume.
    metadata_bytes = 7 * len(html_escape(job['title']).encode('utf-8')) + sum(
        len(html_escape(job['metadata'].get(field, '')).encode('utf-8')) for field in ('author', 'description'))
    if len(job['sources']) == 1:
        metadata_bytes += stats[0]['comic_info_bytes']
    data = dict(version=1, work=str(config['paths']['work_root']), output_dir=str(config['paths']['output_dir']), target=list(target),
                target_bytes=int(target_mib * 1024 * 1024), images=images,
                metadata_bytes=metadata_bytes, sidecar_bytes=len(comic_info_bytes(job)),
                expanded=sum(item['expanded'] for item in stats),
                sources=[dict(path=str(path), expanded=item['expanded']) for path, item in zip(job['sources'], stats)],
                cover_bytes=job['cover'].stat().st_size if job['cover'] else 0)
    job['_space_key'], job['_space_data'] = key, data
    print(f'容量估算 {job["title"]}：解壓已知 {data["expanded"]:,} bytes；'
          f'圖片輸出估計 {sum(images["predictions"]):,} bytes'
          f'（{images["samples"]} 張來源記憶體 JPEG 試編碼，品質 {quality}，'
          f'樣本最大每頁 {images["sample_bytes"]:,} bytes ＋25% 估計餘裕；非大小上界）', flush=True)
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
