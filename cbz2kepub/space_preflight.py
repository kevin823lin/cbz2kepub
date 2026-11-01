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
    return 2 if (width > height) != (target[0] > target[1]) and max(width, height) / min(width, height) > 1.05 else 1


def calibrate_images(sources, target, quality=85):
    """Read image headers, then trial-encode at most three source images in RAM."""
    from PIL import Image, ImageOps
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
    ordered = sorted(images, key=lambda item: item[2])
    samples = [ordered[index] for index in sorted({0, len(ordered) // 2, len(ordered) - 1})]
    maximum = 0
    for source, name, _, _ in samples:
        with zipfile.ZipFile(source) as archive, archive.open(name) as stream, Image.open(stream) as image:
            # Native KCC can retain a color first page even when forcecolor is false.
            image = image.convert('RGB')
            candidates = [image]
            if page_count(image.size, target) == 2:
                width, height = image.size
                boxes = ((0, 0, width // 2, height), (width // 2, 0, width, height)) if width > height else (
                    (0, 0, width, height // 2), (0, height // 2, width, height))
                candidates.extend(image.crop(box) for box in boxes)
            for candidate in candidates:
                resized = ImageOps.fit(candidate, target, method=Image.Resampling.LANCZOS)
                buffer = BytesIO()
                resized.save(buffer, 'JPEG', optimize=True, quality=quality)
                maximum = max(maximum, buffer.tell())
    # shortcut: samples cannot bound unseen JPEG content; recheck actual bytes after processing.
    page_bytes = math.ceil(maximum * 1.25)
    return dict(page_bytes=page_bytes, sample_bytes=maximum, samples=len(samples),
                originals=[item[2] for item in images],
                predictions=[page_bytes * page_count(item[3], target) for item in images])


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
        # 7z rename may use an archive temporary file; keep this explicit allowance.
        return total + 2 * largest, total

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
        from PIL import Image
        images_dir = self._inside_work(images_dir)
        originals, predictions = [], []
        for path in images_dir.rglob('*'):
            if path.is_file() and path.suffix.lower() in IMAGE_TYPES:
                with Image.open(path) as image:
                    predictions.append(self.page_bytes * page_count(image.size, self.target))
                originals.append(path.stat().st_size)
        packing, _ = self.packaging_peak(sum(predictions))
        peak = max(image_peak(originals, predictions, self.workers), packing)
        require_space(images_dir, peak - sum(originals), '圖片處理及剩餘封裝預檢（圖片大小為估計）')

    def before_zip(self, tome, zipfilename, sevenzip=False):
        tome, output = self._inside_work(tome), self._inside_work(zipfilename)
        files = [path for path in tome.rglob('*') if path.is_file()]
        size = sum(path.stat().st_size + 256 + 2 * len(path.relative_to(tome).as_posix().encode('utf-8'))
                   for path in files) + 4096
        temporary = size if sevenzip else 0
        print(f'封裝預檢：工作檔及 ZIP 結構約 {size:,} bytes；7z 暫存預留 {temporary:,} bytes', flush=True)
        require_space(output, size + temporary, 'EPUB 封裝預檢')

    def after_images(self, tomes):
        sizes, overheads = [], []
        for tome in tomes:
            tome = self._inside_work(tome)
            files = [path for path in tome.rglob('*') if path.is_file()]
            sizes.append(sum(path.stat().st_size for path in files))
            overheads.append(self.page_bytes + self.data.get('metadata_bytes', 0) + len(files) * 4096)
        present = sum(sizes)
        occupancy = peak = present
        output_bytes = 0
        for size, overhead in zip(sizes, overheads):
            archive = size + overhead
            peak = max(peak, occupancy + overhead + 2 * archive)
            occupancy += archive - size
            output_bytes += archive
        print(f'圖片處理完成預檢：實際分卷工作資料 {present:,} bytes，{len(sizes)} 卷；'
              f'work 後續最大新增約 {peak - present:,} bytes（含 7z 暫存預留）', flush=True)
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
