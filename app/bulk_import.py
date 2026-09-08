"""Conservative recursive discovery for reliable multi-edition imports."""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Callable

from .bilingual_catalog import load_catalog
from .importer import (
    ARCHIVES,
    IMAGES,
    archive_is_sparse_partial,
    infer_language,
    infer_metadata,
    mokuro_data,
    mokuro_images,
)

Progress = Callable[[int, int, str], None]


def content_signature(path: Path, related: list[Path] | None = None) -> str:
    """Cheap, stable duplicate signature without hashing entire multi-gigabyte manga."""
    digest = hashlib.sha256()
    files = related or [path]
    content_indexes = set(range(len(files))) if len(files) <= 4 else {0, 1, len(files) - 2, len(files) - 1}
    for index, item in enumerate(files):
        try:
            stat = item.stat()
        except OSError:
            continue
        if related is not None:
            digest.update(item.name.encode("utf-8", "surrogatepass"))
        digest.update(str(stat.st_size).encode())
        if item.is_file() and index in content_indexes:
            try:
                with item.open("rb") as stream:
                    offsets = {0, max(0, stat.st_size - 65536)}
                    if related is None and stat.st_size > 3 * 65536:
                        offsets.update({stat.st_size // 3, stat.st_size * 2 // 3})
                    for offset in sorted(offsets):
                        stream.seek(offset)
                        digest.update(stream.read(65536))
            except OSError:
                continue
    return digest.hexdigest()


def _mokuro_title(raw: bytes) -> str:
    try:
        data = json.loads(raw.decode("utf-8"))
        return str(data.get("title", "")).strip() if isinstance(data, dict) else ""
    except (UnicodeError, json.JSONDecodeError):
        return ""


def _archive_preview(path: Path, recognized: bool) -> dict:
    image_count = 0
    image_count_exact = True
    mokuro_title = ""
    has_mokuro = False
    if path.suffix.lower() in {".zip", ".cbz"}:
        try:
            with zipfile.ZipFile(path) as bundle:
                entries = [item for item in bundle.infolist() if not item.is_dir()]
                total = sum(item.file_size for item in entries)
                stored = sum(item.compress_size for item in entries)
                if len(entries) > 10_000 or total > 6 * 1024**3 or total / max(1, stored) > 250:
                    raise ValueError("Archive expands to an unreasonable size")
                for entry in entries:
                    clean = Path(entry.filename.replace("\\", "/"))
                    if clean.is_absolute() or ".." in clean.parts:
                        raise ValueError("Archive contains an unsafe path")
                image_count = sum(Path(item.filename).suffix.lower() in IMAGES for item in entries)
                sidecar = next(
                    (item for item in entries if Path(item.filename).suffix.lower() == ".mokuro"),
                    None,
                )
                has_mokuro = sidecar is not None
                if sidecar and sidecar.file_size <= 32 * 1024**2:
                    mokuro_title = _mokuro_title(bundle.read(sidecar))
        except (OSError, zipfile.BadZipFile) as exc:
            raise ValueError("Damaged or unsupported ZIP/CBZ") from exc
    else:
        if archive_is_sparse_partial(path):
            raise ValueError("Only partially downloaded; download this TAR again")
        try:
            # The supported extension is a plain TAR. Raw mode avoids spending
            # minutes probing sparse/zero-filled partial downloads as several
            # different compressed formats before reporting them as incomplete.
            with tarfile.open(path, mode="r:") as bundle:
                file_count = 0
                total = 0
                for member in bundle:
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise ValueError("Archive contains an unsafe link or special entry")
                    clean = Path(member.name.replace("\\", "/"))
                    if clean.is_absolute() or ".." in clean.parts:
                        raise ValueError("Archive contains an unsafe path")
                    file_count += 1
                    total += member.size
                    if file_count > 10_000 or total > 6 * 1024**3:
                        raise ValueError("Archive expands to an unreasonable size")
                    suffix = Path(member.name).suffix.lower()
                    if suffix in IMAGES:
                        image_count += 1
                    elif suffix == ".mokuro":
                        has_mokuro = True
                        if member.size <= 32 * 1024**2:
                            stream = bundle.extractfile(member)
                            if stream:
                                mokuro_title = _mokuro_title(stream.read())
        except (OSError, tarfile.TarError) as exc:
            raise ValueError("Damaged or unsupported TAR") from exc
    if not image_count:
        raise ValueError("No readable manga pages; the download may be incomplete")
    return {
        "image_count": image_count,
        "image_count_exact": image_count_exact,
        "has_mokuro": has_mokuro,
        "mokuro_title": mokuro_title,
    }


def _base_item(path: Path, kind: str, metadata: dict, signature: str) -> dict:
    try:
        size = path.stat().st_size if path.is_file() else 0
    except OSError:
        size = 0
    return {
        "id": hashlib.sha256(str(path.resolve()).encode("utf-8", "surrogatepass")).hexdigest()[:24],
        "path": str(path.resolve()),
        "name": path.name,
        "kind": kind,
        "size": size,
        "series": metadata.get("series", ""),
        "title": metadata.get("title", ""),
        "volume_label": metadata.get("volume", ""),
        "chapter_label": metadata.get("chapter", ""),
        "signature": signature,
        "recognized": False,
        "content_type": "raw",
        "language": None,
        "status": "review",
        "selected": False,
        "error": "",
        "note": "Choose Japanese or English before importing.",
    }


def scan_folder(root: Path, catalog_path: Path, progress: Progress | None = None) -> list[dict]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("The selected bulk-import folder is unavailable")
    archives: list[Path] = []
    sidecars: list[Path] = []
    image_directories: dict[Path, list[Path]] = {}
    visited = 0
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = [name for name in names if not name.startswith(".")]
        folder = Path(directory)
        direct_images = []
        for name in filenames:
            if name.startswith("."):
                continue
            visited += 1
            if visited > 200_000:
                raise ValueError("The selected folder contains too many files to scan safely")
            path = folder / name
            suffix = path.suffix.lower()
            if suffix in ARCHIVES:
                archives.append(path)
            elif suffix == ".mokuro":
                sidecars.append(path)
            elif suffix in IMAGES:
                direct_images.append(path)
        if direct_images:
            image_directories[folder] = direct_images
    if len(archives) + len(sidecars) + len(image_directories) > 5000:
        raise ValueError("More than 5,000 possible editions were found; choose a smaller folder")

    work_total = len(archives) + len(sidecars) + len(image_directories)
    processed = 0
    items: list[dict] = []
    covered_image_directories: set[Path] = set()
    catalog = load_catalog(catalog_path)

    for sidecar in sorted(sidecars):
        page_files = mokuro_images(sidecar)
        data = mokuro_data(sidecar)
        metadata = infer_metadata(sidecar.stem)
        title = str(data.get("title", "")).strip()
        if title:
            metadata["title"] = title
            metadata["series"] = title
        signature = content_signature(sidecar, [sidecar, *page_files])
        item = _base_item(sidecar, "mokuro", metadata, signature)
        item.update(
            {
                "language": "jp",
                "content_type": "mokuro",
                "status": "ready" if page_files else "error",
                "selected": bool(page_files),
                "error": "" if page_files else "Mokuro file does not point to readable images",
                "note": "Mokuro detected; Japanese OCR will be available." if page_files else "",
                "image_count": len(page_files),
                "image_count_exact": True,
                "mokuro_path": str(sidecar.resolve()),
            }
        )
        items.append(item)
        covered_image_directories.add(sidecar.parent.resolve())
        for page in page_files:
            covered_image_directories.add(page.parent.resolve())
        processed += 1
        if progress:
            progress(processed, work_total, sidecar.name)

    for archive in sorted(archives):
        catalog_entry = catalog.get(archive.stem)
        match = dict(catalog_entry) if isinstance(catalog_entry, dict) else None
        metadata = match or infer_metadata(archive.stem)
        item = _base_item(archive, "archive", metadata, content_signature(archive))
        try:
            preview = _archive_preview(archive, bool(match))
            if match:
                item.update(
                    {
                        "recognized": True,
                        "content_type": "bilingual",
                        "language": match["language"],
                        "series": match["series"],
                        "title": match["title"],
                        "volume_label": match["volume_label"],
                        "status": "ready",
                        "selected": True,
                        "note": "Recognized from the cached archive index.",
                    }
                )
            elif preview["has_mokuro"]:
                title = preview["mokuro_title"]
                item.update(
                    {
                        "language": "jp",
                        "content_type": "mokuro",
                        "series": title or item["series"],
                        "title": title or item["title"],
                        "status": "ready",
                        "selected": True,
                        "note": "Mokuro detected inside this archive.",
                    }
                )
            else:
                item.update(
                    {
                        "language": infer_language(archive.stem),
                        "content_type": "raw",
                        "status": "ready",
                        "selected": True,
                        "note": "Raw manga detected. Metadata and language were inferred; you can correct them under Details.",
                    }
                )
            item.update(preview)
        except ValueError as exc:
            # CID-named files are retained in the review so incomplete torrent
            # pieces are visible instead of disappearing from the result count.
            if match or archive.stem.startswith(("bafy", "Qm")):
                item.update(
                    {
                        "recognized": bool(match),
                        "language": match.get("language") if match else None,
                        "series": match.get("series", item["series"]) if match else item["series"],
                        "title": match.get("title", item["title"]) if match else item["title"],
                        "volume_label": match.get("volume_label", item["volume_label"]) if match else item["volume_label"],
                        "status": "error",
                        "selected": False,
                        "error": str(exc),
                        "note": "",
                        "image_count": 0,
                        "image_count_exact": True,
                    }
                )
            else:
                processed += 1
                if progress:
                    progress(processed, work_total, archive.name)
                continue
        items.append(item)
        processed += 1
        if progress:
            progress(processed, work_total, archive.name)

    for folder, direct_images in sorted(image_directories.items()):
        if folder.resolve() in covered_image_directories or len(direct_images) < 2:
            processed += 1
            if progress:
                progress(processed, work_total, folder.name)
            continue
        ordered = sorted(direct_images)
        metadata = infer_metadata(folder.name)
        item = _base_item(folder, "image-folder", metadata, content_signature(folder, ordered))
        item.update(
            {
                "language": infer_language(folder.name),
                "content_type": "raw",
                "status": "ready",
                "selected": True,
                "image_count": len(ordered),
                "image_count_exact": True,
                "note": "Raw image manga detected. It can be converted with Mokuro after import.",
            }
        )
        items.append(item)
        processed += 1
        if progress:
            progress(processed, work_total, folder.name)

    return sorted(
        items,
        key=lambda item: (
            {"ready": 0, "review": 1, "error": 2}.get(item["status"], 3),
            item["series"].casefold(),
            item["volume_label"].casefold(),
            item["name"].casefold(),
        ),
    )
