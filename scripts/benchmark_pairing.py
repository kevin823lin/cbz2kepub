"""Compare chapter pairing off/on against a selected, local CBZ sample."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from unittest.mock import patch

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from acceptance_real import digest, pairing_summary, save_json, verify_cover_and_metadata
from cbz2kepub import converter
from cbz2kepub.epub_outputs import inspect_epub


PHASE = re.compile(r'^(imgFileProcessing|chapterPairingAnalysis):\s*([0-9.]+)\s+seconds\s*$', re.M)


def phase_times(log_text):
    return {name: float(seconds) for name, seconds in PHASE.findall(log_text)}


def rss_sampler(stop, peak):
    process = psutil.Process()
    while not stop.wait(0.1):
        try:
            processes = [process, *process.children(recursive=True)]
            peak[0] = max(peak[0], sum(item.memory_info().rss for item in processes if item.is_running()))
        except (psutil.Error, OSError):
            continue


def load_sample(path):
    sample = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(sample, dict) or not isinstance(sample.get('selected'), list) or not sample['selected']:
        raise ValueError('sample 必須含非空 selected 陣列')
    source = Path(sample['source_dir']).expanduser().resolve()
    title = sample['title']
    if (not isinstance(title, str) or not title.strip() or title in ('.', '..') or
            Path(title).is_absolute() or Path(title).name != title or
            any(char in title for char in '/\\\x00:*?"<>|') or title.endswith((' ', '.'))):
        raise ValueError('sample 的 title 必須是安全的單一目錄名稱')
    if not source.is_dir():
        raise ValueError('sample 的 source_dir 無效')
    selected = []
    seen = set()
    for entry in sample['selected']:
        if not isinstance(entry, dict) or not isinstance(entry.get('file'), str):
            raise ValueError('selected 每筆必須含 file')
        path = (source / entry['file']).resolve()
        if not path.is_relative_to(source) or not path.is_file() or path.suffix.casefold() != '.cbz':
            raise ValueError(f'選取項目不是來源目錄內的 CBZ：{entry["file"]}')
        if path.name.casefold() in seen:
            raise ValueError(f'重複或大小寫衝突的 CBZ 名稱：{path.name}')
        seen.add(path.name.casefold())
        selected.append(path)
    return source, title, selected


def prepare_run(root, title, sources, pairing):
    root.mkdir(parents=True)
    facade = root / 'input' / title / 'compressed'
    facade.mkdir(parents=True)
    for source in sources:
        try:
            os.link(source, facade / source.name)
        except OSError as exc:
            raise OSError(f'無法建立來源硬連結：{source} ({exc})') from exc
    for name in ('cover.jpg', 'info.json'):
        source = sources[0].parent / name
        if source.is_file() and source.stat().st_size:
            shutil.copy2(source, facade / name)
    template = (ROOT / 'config.example.toml').read_text(encoding='utf-8')
    template = template.replace('"範例漫畫 A"', json.dumps(title, ensure_ascii=False)).replace(
        'chapter_pairing = true', f'chapter_pairing = {str(pairing).lower()}')
    template = template.replace('work_root = "./.cbz2kepub-work"', 'work_root = "./work"')
    config_path = root / 'config.toml'
    config_path.write_text(template, encoding='utf-8')
    return config_path


def run_case(results_root, title, sources, pairing, label, report, state):
    state.update(current=label)
    report.update(current=label)
    save_json(results_root / 'state.json', state)
    save_json(results_root / 'benchmark.json', report)
    print(f'[{len(report["runs"]) + 1}/{state["total_runs"]}] starting {label}', flush=True)
    case = results_root / label
    config_path = prepare_run(case, title, sources, pairing)
    config = converter.load_config(config_path)
    job = config['jobs'][0]
    source_hashes = {path.name: digest(path) for path in sources}
    extra_hashes = {name: digest(sources[0].parent / name) for name in ('cover.jpg', 'info.json')
                    if (sources[0].parent / name).is_file()}
    timings = {}
    retained_pairing = {}
    original_run_kcc = converter.run_kcc
    original_validate = converter.validate_pairing_report

    def timed_kcc(command, work, environment):
        log = work / 'kcc.log'
        try:
            original_run_kcc(command, work, environment)
            shutil.copy2(log, case / 'kcc.log')
            timings.update(phase_times(log.read_text(encoding='utf-8', errors='replace')))
            required = {'imgFileProcessing'} | ({'chapterPairingAnalysis'} if pairing else set())
            if required - timings.keys():
                raise ValueError(f'KCC log 缺少計時欄位：{sorted(required - timings.keys())}')
        finally:
            if log.is_file() and not (case / 'kcc.log').exists():
                shutil.copy2(log, case / 'kcc.log')

    def retain_report(path, summaries, current_job, target, direction):
        value = original_validate(path, summaries, current_job, target, direction)
        retained = case / 'pairing.json'
        shutil.copy2(path, retained)
        retained_pairing.update(path=str(retained), report=value)
        return value

    stop = threading.Event()
    peak = [psutil.Process().memory_info().rss]
    sampler = threading.Thread(target=rss_sampler, args=(stop, peak), daemon=True)
    sampler.start()
    started = time.perf_counter()
    convert_elapsed = None
    try:
        with converter.output_lock(config['paths']['output_dir']), \
                patch.object(converter, 'run_kcc', side_effect=timed_kcc), \
                patch.object(converter, 'validate_pairing_report', side_effect=retain_report):
            outputs = converter.convert_comic(job, config)
        convert_elapsed = time.perf_counter() - started
        summaries = [inspect_epub(path) for path in outputs]
        converter.validate_book(job, summaries)
        cover_metadata = [verify_cover_and_metadata(
            path, summary, job, config['conversion'], index, len(outputs), case)
            for index, (path, summary) in enumerate(zip(outputs, summaries), 1)]
        actual_hashes = {path.name: digest(path) for path in sources}
        if actual_hashes != source_hashes:
            raise ValueError('來源 CBZ SHA256 改變')
        actual_extras = {name: digest(sources[0].parent / name) for name in extra_hashes}
        if actual_extras != extra_hashes:
            raise ValueError('來源 cover.jpg 或 info.json SHA256 改變')
        if pairing and not retained_pairing:
            raise AssertionError('配對報告驗證器未執行')
        result = {
            'label': label, 'chapter_pairing': pairing,
            'wall_seconds': round(convert_elapsed, 3),
            'imgFileProcessing_seconds': timings.get('imgFileProcessing'),
            'chapterPairingAnalysis_seconds': timings.get('chapterPairingAnalysis'),
            'process_tree_peak_rss_bytes': peak[0],
            'output_bytes': sum(path.stat().st_size for path in outputs),
            'outputs': [str(path) for path in outputs],
            'source_sha256': source_hashes, 'source_sha256_unchanged': actual_hashes == source_hashes,
            'source_cover_metadata_sha256': extra_hashes,
            'source_cover_metadata_sha256_unchanged': actual_extras == extra_hashes,
            'cover_metadata_checks': cover_metadata,
            'pairing_report': retained_pairing.get('path'),
            'pairing_summary': pairing_summary(retained_pairing['report']) if pairing else None,
        }
        report['runs'].append(result)
        state.update(status='running', completed_runs=len(report['runs']), current=label)
        save_json(results_root / 'benchmark.json', report)
        save_json(results_root / 'state.json', state)
        return result
    except BaseException as exc:
        state.update(status='failed', completed_runs=len(report['runs']), current=label, error=str(exc))
        report.update(status='failed', error=str(exc))
        save_json(results_root / 'benchmark.json', report)
        save_json(results_root / 'state.json', state)
        raise
    finally:
        stop.set()
        sampler.join(timeout=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample', type=Path, required=True)
    parser.add_argument('--results-root', type=Path, required=True)
    parser.add_argument('--self-test', action='store_true', help='check timing parser without conversion')
    args = parser.parse_args(argv)
    if args.self_test:
        load_sample(args.sample.expanduser().resolve())
        assert phase_times('imgFileProcessing: 1.25 seconds\nchapterPairingAnalysis: 0.5 seconds') == {
            'imgFileProcessing': 1.25, 'chapterPairingAnalysis': 0.5}
        print('benchmark self-test passed')
        return 0
    results_root = args.results_root.expanduser().resolve()
    if results_root.exists() and any(results_root.iterdir()):
        raise ValueError('--results-root 必須不存在或為空目錄')
    source, title, sources = load_sample(args.sample.expanduser().resolve())
    files = sorted(sources, key=converter.chapter_key)
    original_hashes = {path.name: digest(path) for path in files}
    state = {'status': 'running', 'source_dir': str(source), 'title': title,
             'selected_files': [p.name for p in files], 'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
             'completed_runs': 0, 'total_runs': 8}
    report = {'status': 'running', **state, 'runs': []}
    results_root.mkdir(parents=True, exist_ok=True)
    save_json(results_root / 'state.json', state)
    save_json(results_root / 'benchmark.json', report)
    try:
        for pairing in (False, True):
            run_case(results_root, title, files, pairing, f'warmup-{"on" if pairing else "off"}', report, state)
        for round_number in range(1, 4):
            order = (False, True) if round_number % 2 else (True, False)
            for pairing in order:
                mode = 'on' if pairing else 'off'
                run_case(results_root, title, files, pairing,
                         f'round-{round_number}-{mode}', report, state)
        if {path.name: digest(path) for path in files} != original_hashes:
            raise ValueError('Benchmark 結束後來源 CBZ SHA256 不符')
        report.update(status='completed', finished_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
        state.update(status='completed', completed_runs=len(report['runs']), current=None)
    except BaseException as exc:
        report.update(status='failed', error=str(exc), finished_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
        state.update(status='failed', completed_runs=len(report['runs']), error=str(exc))
        raise
    finally:
        save_json(results_root / 'benchmark.json', report)
        save_json(results_root / 'state.json', state)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
