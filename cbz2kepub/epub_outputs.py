"""Validate KCC EPUBs, then replace only outputs owned by this tool."""

import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import tempfile
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
from zipfile import ZIP_STORED, ZipFile

from PIL import Image

from .space_preflight import require_space, same_volume


OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
XHTML = "http://www.w3.org/1999/xhtml"
EPUB = "http://www.idpf.org/2007/ops"
NCX = "http://www.daisy.org/z3986/2005/ncx/"
NS = {"opf": OPF, "dc": DC, "html": XHTML, "ncx": NCX}
COVER_ID = "cbz2kepub-cover"
ET.register_namespace("", OPF)
ET.register_namespace("dc", DC)
ET.register_namespace("epub", EPUB)


def _resolve(base, href):
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(parts.path))) if parts.path else base
    if path == ".." or path.startswith(("../", "/")) or "\\" in path:
        raise ValueError(f"Unsafe EPUB reference: {base}: {href}")
    return path


def _package(archive):
    container = ET.fromstring(archive.read("META-INF/container.xml"))
    rootfiles = container.findall("{*}rootfiles/{*}rootfile")
    if len(rootfiles) != 1:
        raise ValueError("EPUB must contain exactly one package document")
    opf_path = rootfiles[0].get("full-path", "")
    if _resolve("container.xml", opf_path) != opf_path or not opf_path:
        raise ValueError("Invalid EPUB package path")
    package = ET.fromstring(archive.read(opf_path))
    if package.tag != f"{{{OPF}}}package":
        raise ValueError("Invalid OPF package document")
    for name in ("metadata", "manifest", "spine"):
        if package.find(f"opf:{name}", NS) is None:
            raise ValueError(f"OPF is missing {name}")
    return opf_path, package


def inspect_epub(path, max_bytes=2_000_000_000):
    """Check ZIP/XML/resource integrity and return ordered body/navigation data."""
    path = Path(path)
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"EPUB exceeds {max_bytes} bytes: {path.name} ({size})")
    with ZipFile(path) as archive:
        entries = archive.infolist()
        names = set(archive.namelist())
        if len(entries) != len(names):
            raise ValueError("EPUB contains duplicate ZIP entries")
        if not entries or entries[0].filename != "mimetype" or entries[0].compress_type != ZIP_STORED or archive.read("mimetype") != b"application/epub+zip":
            raise ValueError("EPUB mimetype must be the first, uncompressed entry")
        for name in names:
            if "\\" in name or posixpath.normpath(name.rstrip("/")) != name.rstrip("/") or name.startswith("/"):
                raise ValueError(f"Unsafe EPUB ZIP path: {name}")
        bad = archive.testzip()
        if bad:
            raise ValueError(f"EPUB CRC failed: {bad}")
        opf_path, package = _package(archive)
        manifest = package.find("opf:manifest", NS)
        spine = package.find("opf:spine", NS)
        metadata = package.find("opf:metadata", NS)
        items = {}
        documents = {}
        for item in manifest:
            item_id = item.get("id")
            if not item_id or item_id in items:
                raise ValueError("Missing or duplicate manifest ID")
            resource = _resolve(opf_path, item.get("href", ""))
            if resource is None or resource not in names or resource.endswith("/"):
                raise ValueError(f"Missing or nonlocal manifest resource: {item.get('href')}")
            items[item_id] = (item, resource)
            if item.get("media-type") in ("application/xhtml+xml", "application/x-dtbncx+xml", "image/svg+xml"):
                documents[resource] = ET.fromstring(archive.read(resource))
        documents[opf_path] = package
        ids_by_path = {name: {node.get("id") for node in doc.iter() if node.get("id")} for name, doc in documents.items()}
        for name, document in documents.items():
            for node in document.iter():
                for attribute, href in node.attrib.items():
                    if attribute.rsplit("}", 1)[-1] not in ("href", "src"):
                        continue
                    target = _resolve(name, href)
                    if target is None:
                        continue
                    if target not in names:
                        raise ValueError(f"Missing EPUB reference: {name}: {href}")
                    fragment = unquote(urlsplit(href).fragment)
                    if fragment and target in ids_by_path and fragment not in ids_by_path[target]:
                        raise ValueError(f"Missing EPUB fragment: {name}: {href}")

        body_paths, body_images, spreads = [], [], []
        body_layout = []
        has_cover = False
        for index, itemref in enumerate(spine):
            if itemref.get("idref") not in items:
                raise ValueError("Spine refers to a missing manifest item")
            item, resource = items[itemref.get("idref")]
            if item.get("media-type") != "application/xhtml+xml":
                raise ValueError("Spine item must be XHTML")
            document = documents[resource]
            is_cover = itemref.get("idref") == COVER_ID or any("cover" in node.get(f"{{{EPUB}}}type", "").split() for node in document.iter())
            if is_cover:
                if has_cover or index != 0:
                    raise ValueError("Cover page must occur once at the start of the spine")
                has_cover = True
                continue
            page_images = []
            image_data = []
            viewport = None
            for node in document.iter():
                if node.tag.rsplit("}", 1)[-1] == "meta" and node.get("name", "").lower() == "viewport":
                    match = re.fullmatch(r"\s*width\s*=\s*(\d+)\s*,\s*height\s*=\s*(\d+)\s*", node.get("content", ""))
                    if match:
                        viewport = (int(match[1]), int(match[2]))
                if node.tag.rsplit("}", 1)[-1] in ("img", "image"):
                    href = node.get("src") or node.get("href") or node.get("{http://www.w3.org/1999/xlink}href")
                    if href:
                        image = _resolve(resource, href)
                        if image and image not in page_images:
                            page_images.append(image)
                            with archive.open(image) as stream, Image.open(stream) as bitmap:
                                image_data.append({"path": image, "size": bitmap.size})
            if not page_images:
                raise ValueError(f"Body page has no local image: {resource}")
            body_paths.append(resource)
            body_images.extend(page_images)
            spread = itemref.get("properties", "")
            spreads.append(spread)
            spread_tokens = {token.rsplit(":", 1)[-1] for token in spread.split()}
            body_layout.append({"xhtml": resource, "images": image_data, "viewport": viewport,
                                "side": next((side for side in ("left", "right", "center")
                                              if f"page-spread-{side}" in spread_tokens), None)})
        if not body_paths:
            raise ValueError("EPUB has no body pages")
        if len(body_paths) != len(set(body_paths)):
            raise ValueError("EPUB repeats a body page in the spine")
        navigation = next(((item, name) for item, name in items.values() if "nav" in item.get("properties", "").split()), None)
        ncx = next(((item, name) for item, name in items.values() if item.get("media-type") == "application/x-dtbncx+xml"), None)
        if navigation is None or ncx is None:
            raise ValueError("EPUB requires NAV and NCX chapter navigation")
        nav_path = navigation[1]
        nav_toc = next((node for node in documents[nav_path].iter(f"{{{XHTML}}}nav") if "toc" in node.get(f"{{{EPUB}}}type", "").split()), None)
        if nav_toc is None:
            raise ValueError("NAV has no table of contents")
        nav_entries = [("".join(node.itertext()).strip(), _resolve(nav_path, node.get("href", ""))) for node in nav_toc.iter(f"{{{XHTML}}}a")]
        ncx_entries = []
        for node in documents[ncx[1]].iter(f"{{{NCX}}}navPoint"):
            label = node.find("ncx:navLabel/ncx:text", NS)
            content = node.find("ncx:content", NS)
            if label is None or content is None:
                raise ValueError("NCX entry is incomplete")
            ncx_entries.append(("".join(label.itertext()).strip(), _resolve(ncx[1], content.get("src", ""))))
        if not nav_entries or nav_entries != ncx_entries:
            raise ValueError("NAV and NCX chapter entries disagree")
        if any(not label or target not in body_paths for label, target in nav_entries):
            raise ValueError("Chapter navigation must point to body pages")
        chapter_positions = [body_paths.index(target) for _, target in nav_entries]
        if chapter_positions[0] != 0 or chapter_positions != sorted(set(chapter_positions)):
            raise ValueError("Chapter navigation must follow body order without duplicate targets")
        collection = metadata.find("opf:meta[@property='belongs-to-collection']", NS)
        position = None
        if collection is not None:
            position = next((node.text for node in metadata if node.get("property") == "group-position" and node.get("refines") == "#" + collection.get("id", "")), None)
        return {
            "size": size,
            "body_pages": len(body_paths),
            "body_images": body_images,
            "chapter_titles": [label for label, _ in nav_entries],
            "title": metadata.findtext("dc:title", default="", namespaces=NS),
            "authors": [node.text or "" for node in metadata.findall("dc:creator", NS)],
            "description": metadata.findtext("dc:description", default="", namespaces=NS),
            "series": collection.text if collection is not None else None,
            "series_index": position,
            "has_cover_page": has_cover,
            "page_spreads": spreads,
            "body_layout": body_layout,
            "direction": spine.get("page-progression-direction"),
        }


def inspect_owned_outputs(output_dir, title, work_title=None):
    """Validate ownership without changing files; the caller must hold the output lock."""
    output_dir = Path(output_dir).resolve()
    work_title = title if work_title is None else work_title
    if not title or title in (".", "..") or any(char in title for char in "/\\\0"):
        raise ValueError("Output title must be a filename stem")
    pattern = re.compile(re.escape(title) + r"(?: [1-9][0-9]*)?\.kepub\.epub", re.IGNORECASE)
    token = hashlib.sha256(title.casefold().encode("utf-8")).hexdigest()[:16]
    manifest_path = output_dir / f".cbz2kepub-{token}.json"
    for pending in output_dir.glob(".cbz2kepub-publish-*/transaction.json"):
        if str(json.loads(pending.read_text(encoding="utf-8")).get("title", "")).casefold() == title.casefold():
            raise ValueError(f"Unfinished publication requires recovery: {pending.parent}")
    old_names = []
    if manifest_path.is_symlink():
        raise ValueError("Output ownership manifest is a symbolic link")
    if manifest_path.exists():
        owned = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (owned.get("version") != 1 or owned.get("title") != title
                or owned.get("work_title", title) != work_title or not isinstance(owned.get("files"), list)):
            raise ValueError("Invalid output ownership manifest")
        old_names = owned["files"]
        if (any(not isinstance(name, str) or not pattern.fullmatch(name) for name in old_names)
                or len({name.casefold() for name in old_names}) != len(old_names)):
            raise ValueError("Invalid filenames in output ownership manifest")
    existing = {path.name for path in output_dir.iterdir() if pattern.fullmatch(path.name)} if output_dir.exists() else set()
    if existing != set(old_names):
        raise ValueError("Existing output ownership is unknown or incomplete; move legacy/colliding outputs aside before publishing")
    if any(not (output_dir / name).is_file() or (output_dir / name).is_symlink() for name in old_names):
        raise ValueError("Owned output is not a regular file")
    return manifest_path, [output_dir / name for name in old_names]


def discard_owned_outputs(output_dir, title, work_title=None):
    """Discard only owned old outputs; an interrupted deletion requires manual recovery."""
    manifest_path, old_paths = inspect_owned_outputs(output_dir, title, work_title)
    if not old_paths and not manifest_path.exists():
        return
    output_dir = manifest_path.parent
    require_space(output_dir, 0, "publication deletion journal", margin=4096)
    transaction = Path(tempfile.mkdtemp(prefix=".cbz2kepub-publish-", dir=output_dir))
    # Keep this journal on any deletion failure: the old set can no longer be rolled back.
    (transaction / "transaction.json").write_text(json.dumps({
        "title": title, "work_title": title if work_title is None else work_title,
        "operation": "discard", "old_files": [path.name for path in old_paths], "new_files": [],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    for path in old_paths:
        path.unlink()
    manifest_path.unlink(missing_ok=True)
    shutil.rmtree(transaction)


def publish_outputs(staged_files, output_dir, title, work_title=None, keep_backup=True):
    """Replace a manifest-owned set; the caller must lock the output directory."""
    if type(keep_backup) is not bool:
        raise ValueError("keep_backup must be a boolean")
    output_dir = Path(output_dir).resolve()
    staged_files = [Path(path) for path in staged_files]
    work_title = title if work_title is None else work_title
    manifest_path, old_paths = inspect_owned_outputs(output_dir, title, work_title)
    pattern = re.compile(re.escape(title) + r"(?: [1-9][0-9]*)?\.kepub\.epub", re.IGNORECASE)
    names = [path.name for path in staged_files]
    if not names or len({name.casefold() for name in names}) != len(names) or any(not pattern.fullmatch(name) for name in names):
        raise ValueError("Staged output names do not uniquely match this work")
    if any(not path.is_file() or path.is_symlink() for path in staged_files):
        raise ValueError("Staged output is missing or is a symbolic link")
    if any(path.resolve().parent == output_dir or any(path.samefile(old) for old in old_paths) for path in staged_files):
        raise ValueError("Staged output aliases an existing output")
    staged_files = [path.resolve() for path in staged_files]
    output_dir.mkdir(parents=True, exist_ok=True)
    transfers = [(path, same_volume(path, output_dir)) for path in staged_files]
    copy_bytes = sum(path.stat().st_size for path, moved in transfers if not moved)
    reclaimable = sum(path.stat().st_size for path in old_paths) if not keep_backup else 0
    require_space(output_dir, copy_bytes, "publication", credit=reclaimable)
    if not keep_backup and (old_paths or manifest_path.exists()):
        discard_owned_outputs(output_dir, title, work_title)
        old_paths = []
        require_space(output_dir, copy_bytes, "publication after old-output deletion")
    old_names = [path.name for path in old_paths]
    transaction = Path(tempfile.mkdtemp(prefix=".cbz2kepub-publish-", dir=output_dir))
    incoming, backup = transaction / "incoming", transaction / "backup"
    incoming.mkdir()
    backup.mkdir()
    backed_up, installed = [], []
    try:
        (transaction / "transaction.json").write_text(json.dumps({
            "title": title, "work_title": work_title, "old_files": old_names, "new_files": names,
            "sources": [{"path": str(path), "move": moved} for path, moved in transfers],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        for source, moved in transfers:
            if moved:
                os.replace(source, incoming / source.name)
            else:
                shutil.copyfile(source, incoming / source.name)
                if (incoming / source.name).stat().st_size != source.stat().st_size:
                    raise OSError(f"Published copy size differs from source: {source.name}")
        (incoming / manifest_path.name).write_text(json.dumps({"version": 1, "title": title, "work_title": work_title, "files": names}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for name in old_names + ([manifest_path.name] if manifest_path.exists() else []):
            os.replace(output_dir / name, backup / name)
            backed_up.append(name)
        for name in names + [manifest_path.name]:
            if (output_dir / name).exists():
                raise FileExistsError(f"Output appeared during publication: {name}")
            os.replace(incoming / name, output_dir / name)
            installed.append(name)
    except BaseException as error:
        # An interrupt may arrive after rename but before the completion list is updated.
        if not isinstance(error, Exception):
            raise RuntimeError(f"Publication interrupted; recovery files retained at {transaction}") from error
        try:
            for source, moved in transfers:
                location = output_dir / source.name if source.name in installed else incoming / source.name
                if location.exists():
                    if moved:
                        if source.exists():
                            raise FileExistsError(f"Staged source appeared during rollback: {source}")
                        os.replace(location, source)
                    else:
                        location.unlink()
            if manifest_path.name in installed:
                manifest_path.unlink()
            for name in backed_up:
                os.replace(backup / name, output_dir / name)
        except BaseException as rollback_error:
            raise RuntimeError(f"Publication rollback failed; backups retained at {transaction}") from rollback_error
        shutil.rmtree(transaction)
        raise
    shutil.rmtree(transaction)
    for source, moved in transfers:
        if not moved:
            source.unlink()
    return [output_dir / name for name in names]
