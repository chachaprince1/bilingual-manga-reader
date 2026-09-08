"""Safe manga discovery, archive extraction, metadata and image features."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
import uuid
import zipfile
from pathlib import Path

IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".svg"}
ARCHIVES = {".zip", ".cbz", ".tar"}
PDFS = {".pdf"}


def archive_is_sparse_partial(path: Path) -> bool:
    """Detect preallocated torrent files whose unwritten ranges are filesystem holes."""
    try:
        stat = path.stat()
        allocated = int(getattr(stat, "st_blocks", 0)) * 512
    except OSError:
        return False
    if not allocated or stat.st_size < 1024 * 1024:
        return False
    missing = stat.st_size - allocated
    return missing > max(512 * 1024, int(stat.st_size * 0.05))


def _natural_key(path: Path) -> list[tuple[int, object]]:
    return [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", path.as_posix())
    ]


def images(folder: Path) -> list[Path]:
    return sorted(
        (path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() in IMAGES),
        key=lambda path: _natural_key(path.relative_to(folder)),
    )


def _safe_archive_destination(target: Path, name: str) -> Path:
    clean = Path(name.replace("\\", "/"))
    if clean.is_absolute() or ".." in clean.parts:
        raise ValueError("Archive contains an unsafe path")
    destination = (target / clean).resolve()
    if target.resolve() not in destination.parents:
        raise ValueError("Archive contains an unsafe path")
    return destination


def _check_archive_limits(file_count: int, total: int, stored: int) -> None:
    if file_count > 10000 or total > 6 * 1024**3 or total / max(1, stored) > 250:
        raise ValueError("Archive expands to an unreasonable size")


def _extract_zip(archive: Path, target: Path, allowed: set[str]) -> None:
    try:
        bundle = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"Damaged or unsupported archive: {archive.name}") from exc
    with bundle:
        files = [item for item in bundle.infolist() if not item.is_dir()]
        total = sum(item.file_size for item in files)
        _check_archive_limits(len(files), total, sum(item.compress_size for item in files))
        for info in files:
            destination = _safe_archive_destination(target, info.filename)
            if destination.suffix.lower() not in allowed:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def _extract_tar(archive: Path, target: Path, allowed: set[str]) -> None:
    if archive_is_sparse_partial(archive):
        raise ValueError(
            f"TAR archive is only partially downloaded: {archive.name}. Download it again."
        )
    try:
        # .tar means a plain TAR here. Raw mode makes sparse/zero-filled
        # incomplete downloads fail promptly instead of triggering expensive
        # compressed-format probes.
        bundle = tarfile.open(archive, mode="r:")
    except (OSError, tarfile.TarError) as exc:
        raise ValueError(f"Damaged or unsupported archive: {archive.name}") from exc
    try:
        with bundle:
            files = []
            for member in bundle.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError("Archive contains an unsafe entry")
                files.append(member)
            if not files:
                raise ValueError(
                    f"TAR archive contains no readable files: {archive.name}. "
                    "The download may be incomplete; download it again."
                )
            _check_archive_limits(
                len(files), sum(member.size for member in files), archive.stat().st_size
            )
            for member in files:
                destination = _safe_archive_destination(target, member.name)
                if destination.suffix.lower() not in allowed:
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError("Archive contains an unreadable file")
                with source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except tarfile.TarError as exc:
        raise ValueError(f"Damaged or unsupported archive: {archive.name}") from exc


def safe_extract(archive: Path, target: Path) -> None:
    """Extract reader assets from ZIP, CBZ, or TAR with strict safety limits."""
    target.mkdir(parents=True, exist_ok=True)
    allowed = IMAGES | {".mokuro"}
    if archive.suffix.lower() == ".tar":
        _extract_tar(archive, target, allowed)
    else:
        _extract_zip(archive, target, allowed)


def page_id(volume_id: str, path: Path, root: Path | None = None) -> str:
    """Stable identity based on a volume UUID and normalized relative filename."""
    try:
        name = path.resolve().relative_to(root.resolve()).as_posix() if root else path.name
    except ValueError:
        name = path.name
    return str(uuid.uuid5(uuid.UUID(volume_id), name.casefold()))


def _binary_hash(image) -> str:
    from PIL import ImageOps

    small = ImageOps.autocontrast(ImageOps.grayscale(image)).resize((16, 16))
    pixels = list(small.get_flattened_data())
    average = sum(pixels) / max(1, len(pixels))
    return "".join("1" if value >= average else "0" for value in pixels)


def image_features(path: Path) -> dict[str, str | int | None]:
    """Compute cached full-page and half-page perceptual features."""
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")
            width, height = image.size
            midpoint = max(1, width // 2)
            return {
                "fingerprint": _binary_hash(image),
                "fingerprint_left": _binary_hash(image.crop((0, 0, midpoint, height))),
                "fingerprint_right": _binary_hash(image.crop((midpoint, 0, width, height))),
                "width": width,
                "height": height,
            }
    except Exception:
        digest = hashlib.sha256(path.read_bytes()[:1024 * 1024]).digest()
        bits = "".join(f"{byte:08b}" for byte in digest)
        return {
            "fingerprint": bits,
            "fingerprint_left": None,
            "fingerprint_right": None,
            "width": None,
            "height": None,
        }


def fingerprint(path: Path) -> str:
    return str(image_features(path)["fingerprint"])


def make_thumbnail(source: Path, destination: Path) -> Path | None:
    try:
        from PIL import Image, ImageOps

        destination.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((360, 520))
            image.save(destination, format="JPEG", quality=82, optimize=True)
        return destination
    except Exception:
        return None


def infer_language(name: str, default: str = "jp") -> str:
    """Infer a raw edition language from common filename markers.

    A missing marker defaults to Japanese because an unlabelled raw manga is
    overwhelmingly the input that benefits from this reader's Mokuro path.
    The catalogue and a real .mokuro sidecar always take precedence elsewhere.
    """
    normalized = f" {re.sub(r'[_\.\-]+', ' ', name).casefold()} "
    if re.search(r"(?:^|[\s\[(])(?:english|eng|en)(?:$|[\s\])])", normalized):
        return "en"
    if re.search(r"(?:^|[\s\[(])(?:japanese|jpn|jp|raw)(?:$|[\s\])])", normalized):
        return "jp"
    return "en" if default == "en" else "jp"


def infer_metadata(name: str) -> dict[str, str]:
    """Infer series, title, volume, and chapter from common manga names."""
    cleaned = re.sub(r"[_]+", " ", str(name)).strip()
    cleaned = re.sub(r"\.(?:cbz|zip|tar|pdf|mokuro)$", "", cleaned, flags=re.IGNORECASE)
    # Remove common release/format suffixes while preserving bracketed title text.
    cleaned = re.sub(
        r"\s*[\[(](?:digital|scan|scans|raw|japanese|jpn|jp|english|eng|en|complete|retail)[^\])]?[\])]\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    title = re.sub(r"\s+", " ", cleaned)
    patterns = (
        ("volume", re.compile(r"(?:\s*[-–—]?\s*)(?:vol(?:ume)?|v)\.?\s*(\d+(?:\.\d+)?)\s*$", re.I)),
        ("volume", re.compile(r"(?:\s*[-–—]?\s*)第\s*(\d+(?:\.\d+)?)\s*巻\s*$")),
        ("chapter", re.compile(r"(?:\s*[-–—]?\s*)(?:ch(?:apter)?|c)\.?\s*(\d+(?:\.\d+)?)\s*$", re.I)),
        ("chapter", re.compile(r"(?:\s*[-–—]?\s*)第\s*(\d+(?:\.\d+)?)\s*話\s*$")),
    )
    series = title
    volume = chapter = ""
    # Parse up to one chapter and one volume suffix (e.g. "Series v03 ch12").
    for _ in range(2):
        matched = False
        for kind, pattern in patterns:
            match = pattern.search(series)
            if not match:
                continue
            number = match.group(1)
            if kind == "volume" and not volume:
                volume = f"Volume {number}"
            elif kind == "chapter" and not chapter:
                chapter = f"Chapter {number}"
            else:
                continue
            series = series[: match.start()].strip(" .-–—")
            matched = True
            break
        if not matched:
            break
    series = series or title or "Untitled Manga"
    return {"series": series, "title": title or series, "volume": volume, "chapter": chapter}


def mokuro_data(path: Path | None) -> dict:
    if not path:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def mokuro_images(path: Path) -> list[Path]:
    """Return existing image files in the exact order declared by one Mokuro file."""
    root = path.parent.resolve()
    found: list[Path] = []
    seen: set[Path] = set()
    for page in mokuro_data(path).get("pages", []):
        if not isinstance(page, dict):
            continue
        clean = Path(str(page.get("img_path", "")).replace("\\", "/"))
        if clean.is_absolute() or not clean.parts or ".." in clean.parts:
            continue
        candidate = (root / clean).resolve()
        if root not in candidate.parents or candidate.suffix.lower() not in IMAGES:
            continue
        if candidate.is_file() and candidate not in seen:
            found.append(candidate)
            seen.add(candidate)
    return found


def mokuro_page(path: Path, image_path: Path, volume_root: Path) -> dict:
    """Read current Mokuro page data without following paths from its JSON."""
    data = mokuro_data(path)
    try:
        relative = image_path.resolve().relative_to(volume_root.resolve()).as_posix()
    except ValueError:
        relative = image_path.name
    for page in data.get("pages", []):
        if not isinstance(page, dict):
            continue
        candidate = str(page.get("img_path", "")).replace("\\", "/")
        if candidate == relative or Path(candidate).name == image_path.name:
            blocks = page.get("blocks", [])
            return {
                "img_width": page.get("img_width", 0),
                "img_height": page.get("img_height", 0),
                "blocks": blocks if isinstance(blocks, list) else [],
            }
    return {"img_width": 0, "img_height": 0, "blocks": []}
