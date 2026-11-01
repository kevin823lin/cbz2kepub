"""Per-volume disk budgets, also loaded directly by the KCC subprocess."""

from io import BytesIO
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import zipfile


MARGIN = 32 * 1024 * 1024
IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.jp2', '.avif'}


def extracted_entry(item):
    # Canonical exclusions shared by KCC's 7z and tar commands.
    parts = PurePosixPath(item.filename.replace('\\', '/')).parts
    return not item.is_dir() and not any(part in ('__MACOSX', '.DS_Store', 'thumbs.db', 'Thumbs.db') for part in parts)


def existing_directory(path):
    path = Path(path).resolve()
    while not path.exists():
        if path.parent == path:
            raise FileNotFoundError(f'找不到可用磁碟區：{path}')
        path = path.parent
    return path if path.is_dir() else path.parent


def volume(path):
    # stat follows mount points/junctions; drive letters alone do not identify a volume.
    return existing_directory(path).stat().st_dev


def same_volume(first, second):
    return volume(first) == volume(second)


def require_space(path, needed, label, credit=0, margin=MARGIN):
    free = shutil.disk_usage(existing_directory(path)).free
    if free + credit < max(0, needed) + margin:
        raise OSError(f'{label}：空間不足；{path} 可用 {free:,} bytes，'
                      f'需新增 {max(0, needed):,} + 操作餘裕 {margin:,} bytes'
                      + (f'；預計刪舊檔釋放 {credit:,} bytes' if credit else ''))
    return free


def image_peak(originals, predictions, workers):
    overlap = sorted((min(u, p) for u, p in zip(originals, predictions)), reverse=True)
    return (sum(originals) + sum(max(p - u, 0) for u, p in zip(originals, predictions))
            + sum(overlap[:workers]))


def page_count(size, target):
    width, height = size
    return 2 if (width > height) != (target[0] > target[1]) and 1.16 < width / height < 1.8 else 1


def calibrate_images(sources, target, quality=85, *, upscale=True, forcecolor=True, eraserainbow=False):
    """Weight at most 24 RAM samples by their size stratum and split-page group."""
    from PIL import Image, ImageOps
    if eraserainbow:
        from kindlecomicconverter.image import colorCheck, erase_rainbow_artifacts
        from types import SimpleNamespace

    images = []
    for source in sources:
        with zipfile.ZipFile(source) as archive:
            for item in archive.infolist():
                relative = Path(item.filename)
                if (not extracted_entry(item) or relative.suffix.lower() not in IMAGE_TYPES
                        or relative.name.startswith('._')):
                    continue
                with archive.open(item) as stream, Image.open(stream) as image:
                    images.append((Path(source), item.filename, item.file_size, image.size))
    if not images:
        raise ValueError('無法校準圖片容量：沒有圖片')
    groups = {}
    for index, item in enumerate(images):
        groups.setdefault(page_count(item[3], target), []).append(index)
    predictions = [0] * len(images)
    maximum = samples = 0
    for pages, indices in groups.items():
        indices.sort(key=lambda index: images[index][2])
        count = min(len(indices), 24 // len(groups))
        for group in range(count):
            members = indices[len(indices) * group // count:len(indices) * (group + 1) // count]
            source, name, _, _ = images[members[len(members) // 2]]
            encoded = 0
            with zipfile.ZipFile(source) as archive, archive.open(name) as stream, Image.open(stream) as image:
                is_color = image.mode not in ('1', 'L')
                if eraserainbow:
                    is_color = colorCheck(image, image.mode, SimpleNamespace(forcecolor=forcecolor, webtoon=False))
                image = image.convert('RGB' if forcecolor and is_color else 'L')
                if pages == 2:
                    width, height = image.size
                    candidates = [image.crop(box) for box in ((0, 0, width // 2, height), (width // 2, 0, width, height))]
                else:
                    candidates = [image]
                    if (image.width > image.height) != (target[0] > target[1]) and image.width / image.height >= 1.8:
                        candidates = [image.rotate(90, expand=True)]
                for candidate in candidates:
                    resized = candidate
                    if upscale or candidate.width > target[0] or candidate.height > target[1]:
                        method = Image.Resampling.BICUBIC if candidate.width <= target[0] and candidate.height <= target[1] else Image.Resampling.LANCZOS
                        if abs(candidate.height / candidate.width - target[1] / target[0]) < 0.015:
                            resized = ImageOps.fit(candidate, target, method=method)
                        else:
                            resized = ImageOps.contain(candidate, target, method=method)
                    if eraserainbow and min(resized.size) > 1:
                        resized = erase_rainbow_artifacts(resized, forcecolor and is_color)
                    buffer = BytesIO()
                    resized.save(buffer, 'JPEG', optimize=True, quality=quality)
                    encoded += buffer.tell()
                    maximum = max(maximum, buffer.tell())
            samples += 1
            # shortcut: cropping/gamma and unseen content remain approximate; recheck actual bytes after processing.
            predicted = math.ceil(encoded * 1.25)
            for index in members:
                predictions[index] = predicted
    return dict(page_bytes=math.ceil(maximum * 1.25), sample_bytes=maximum, samples=samples,
                originals=[item[2] for item in images], predictions=predictions)


class SpacePlan:
    def __init__(self, data):
        self.data = data
        self.work = Path(data['work']).resolve()
        self.target = tuple(data['target'])
        self.page_bytes = data['images']['page_bytes']
        self.workers = max(1, (getattr(os, 'process_cpu_count', os.cpu_count)() or 1))

    @classmethod
    def from_file(cls, path):
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('version') != 1 or not Path(data['work']).is_absolute():
            raise ValueError('Invalid space plan')
        return cls(data)

    def _inside_work(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.work) or path == self.work:
            raise ValueError(f'Space check outside wrapper workspace: {path}')
        if not same_volume(path, self.work):
            raise ValueError(f'Workspace crosses volumes: {path}')
        return path

    def packaging_peak(self, predicted):
        target_bytes = self.data['target_bytes']
        # Next-fit chapter packing can leave a volume partly empty; adjacent volumes exceed target together.
        volumes = min(2 * len(self.data['images']['originals']), max(1, 2 * math.ceil(predicted / target_bytes) - 1))
        # Cover, XHTML and ZIP entries grow with volumes/pages, not CBZ compression ratio.
        overhead = (volumes * (self.page_bytes + self.data.get('metadata_bytes', 0))
                    + len(self.data['images']['originals']) * 4096)
        total = predicted + overhead
        largest = min(predicted, target_bytes) + overhead
        return total + largest, total

    def initial_budget(self, copied_bytes):
        images = self.data['images']
        processed = sum(images['predictions'])
        packing, output = self.packaging_peak(processed)
        processing = image_peak(images['originals'], images['predictions'], self.workers)
        sidecar = self.data.get('sidecar_bytes', 0)
        peak = copied_bytes + self.data['cover_bytes'] + sidecar + max(self.data['expanded'] + sidecar, processing, packing)
        return peak, output

    def before_extract(self, source, destination):
        destination = self._inside_work(destination)
        registered = {str(Path(item['path']).resolve()): item for item in self.data['sources']}
        item = registered.get(str(Path(source).resolve()))
        if item is None:
            raise ValueError(f'Archive absent from wrapper space plan: {source}')
        with zipfile.ZipFile(source) as archive:
            expanded = sum(entry.file_size for entry in archive.infolist() if extracted_entry(entry))
        require_space(destination, expanded, 'CBZ 解壓預檢')

    def before_images(self, images_dir):
        images_dir = self._inside_work(images_dir)
        current = sum(path.stat().st_size for path in images_dir.rglob('*')
                      if path.is_file() and path.suffix.lower() in IMAGE_TYPES)
        originals, predictions = self.data['images']['originals'], self.data['images']['predictions']
        packing, _ = self.packaging_peak(sum(predictions))
        peak = max(image_peak(originals, predictions, self.workers), packing)
        require_space(images_dir, peak - current, '圖片處理及剩餘封裝預檢（沿用分層圖片估計）')

    def before_zip(self, tome, zipfilename):
        tome, output = self._inside_work(tome), self._inside_work(zipfilename)
        files = [path for path in tome.rglob('*') if path.is_file()]
        size = sum(path.stat().st_size + 256 + 2 * len(path.relative_to(tome).as_posix().encode('utf-8'))
                   for path in files) + 4096
        print(f'封裝預檢：需新增 ZIP 約 {size:,} bytes（含 ZIP 結構；直接封裝一次）', flush=True)
        require_space(output, size, 'EPUB 封裝預檢')

    def after_images(self, tomes, cover=None):
        sizes, overheads = [], []
        for index, tome in enumerate(tomes, 1):
            tome = self._inside_work(tome)
            files = [path for path in tome.rglob('*') if path.is_file()]
            sizes.append(sum(path.stat().st_size for path in files))
            cover_bytes = 0
            if cover is not None:
                buffer = BytesIO()
                cover.save_to_folder(buffer, index if len(tomes) > 1 else 0, len(tomes))
                cover_bytes = buffer.tell()
            overheads.append(cover_bytes + self.data.get('metadata_bytes', 0) + len(files) * 4096)
        present = sum(sizes)
        occupancy = peak = present
        output_bytes = 0
        for size, overhead in zip(sizes, overheads):
            archive = size + overhead
            peak = max(peak, occupancy + overhead + archive)
            occupancy += archive - size
            output_bytes += archive
        print(f'圖片處理完成預檢：實際分卷工作資料 {present:,} bytes，{len(sizes)} 卷；'
              f'work 後續最大新增約 {peak - present:,} bytes（直接封裝一次）', flush=True)
        require_space(self.work, peak - present, '實際圖片完成後的剩餘封裝預檢')
        output = Path(self.data['output_dir'])
        if not same_volume(self.work, output):
            require_space(output, output_bytes, '實際圖片完成後的 output 容量預檢')


def batch_requirements(items, margin=0):
    """Return per-volume additional peaks; old files already consume initial free."""
    current, peaks = {}, {}
    for work_volume, output_volume, work_peak, new_output, old_output, keep_backup in items:
        for key in (work_volume, output_volume):
            current.setdefault(key, 0)
            peaks.setdefault(key, 0)
        if not keep_backup:
            current[output_volume] -= old_output
        peaks[work_volume] = max(peaks[work_volume], current[work_volume] + work_peak + margin)
        if work_volume != output_volume:
            peaks[output_volume] = max(peaks[output_volume], current[output_volume] + new_output + margin)
        current[output_volume] += new_output - (old_output if keep_backup else 0)
        peaks[output_volume] = max(peaks[output_volume], current[output_volume] + margin)
    return peaks
