"""Optional recognition for user-supplied Bilingual Manga archive metadata.

The application never bundles the upstream catalogue. A user may import the
archive project's ``json.zip`` file, which is reduced to the small amount of
metadata needed to recognize CID-named JP/EN TAR files.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import threading
import urllib.request
import uuid
import zipfile
from pathlib import Path

import certifi

CATALOG_FILENAME = "bilingual-manga-catalog.json"
CATALOG_SOURCE_PAGE = "https://github.com/B-M-dev/Bilingual-Manga-archive"
CATALOG_DOWNLOAD_URL = "https://raw.githubusercontent.com/B-M-dev/Bilingual-Manga-archive/main/json.zip"
MAX_CATALOG_DOWNLOAD = 64 * 1024**2
DOWNLOAD_LOCK = threading.Lock()
_CID = re.compile(r"^(?:bafy[a-z0-9]{20,}|Qm[1-9A-HJ-NP-Za-km-z]{30,}|[a-f0-9]{32,})$")


def _archive_alias(value: object) -> str | None:
    first = str(value or "").replace("\\", "/").split("/", 1)[0].strip()
    return first if _CID.fullmatch(first) else None


def build_catalog(manga_data: object, manga_metadata: object) -> dict:
    if not isinstance(manga_data, list):
        raise ValueError("Bilingual Manga catalogue data must be a JSON list")
    if isinstance(manga_metadata, list) and manga_metadata:
        metadata_root = manga_metadata[0]
    else:
        metadata_root = manga_metadata
    titles = metadata_root.get("manga_titles", []) if isinstance(metadata_root, dict) else []
    by_record_id = {
        str(item.get("enid")): item
        for item in titles
        if isinstance(item, dict) and item.get("enid")
    }
    archives: dict[str, dict] = {}
    ambiguous: set[str] = set()
    for index, record in enumerate(manga_data):
        if not isinstance(record, dict):
            continue
        record_id = str((record.get("_id") or {}).get("$oid", ""))
        metadata = by_record_id.get(record_id)
        if metadata is None and index < len(titles) and isinstance(titles[index], dict):
            metadata = titles[index]
        metadata = metadata or {}
        series = str(
            metadata.get("entit")
            or metadata.get("jptit")
            or record.get("title")
            or record_id
        ).strip()
        if not series:
            continue
        for language, data_key, hashes_key, labels_key in (
            ("jp", "jp_data", "ch_jph", "ch_najp"),
            ("en", "en_data", "ch_enh", "ch_naen"),
        ):
            language_data = record.get(data_key) or {}
            if not isinstance(language_data, dict):
                continue
            hashes = language_data.get(hashes_key) or []
            labels = language_data.get(labels_key) or []
            if not isinstance(hashes, list):
                continue
            for position, path in enumerate(hashes):
                alias = _archive_alias(path)
                if not alias:
                    continue
                volume_label = (
                    str(labels[position]).strip()
                    if isinstance(labels, list) and position < len(labels)
                    else f"Volume {position + 1:02d}"
                )
                candidate = {
                    "series": series,
                    "language": language,
                    "volume_label": volume_label,
                    "title": " · ".join(value for value in (series, volume_label) if value),
                }
                previous = archives.get(alias)
                if previous and previous != candidate:
                    ambiguous.add(alias)
                else:
                    archives[alias] = candidate
    for alias in ambiguous:
        archives.pop(alias, None)
    return {"version": 1, "archives": archives}


def catalog_from_zip(source: Path) -> dict:
    try:
        with zipfile.ZipFile(source) as bundle:
            names = bundle.namelist()
            data_name = next(
                (name for name in names if name.endswith("BM_data.manga_data.json")), None
            )
            metadata_name = next(
                (name for name in names if name.endswith("BM_data.manga_metadata.json")), None
            )
            if not data_name or not metadata_name:
                raise ValueError(
                    "Choose the Bilingual Manga json.zip containing manga_data and manga_metadata"
                )
            if (
                bundle.getinfo(data_name).file_size > 512 * 1024**2
                or bundle.getinfo(metadata_name).file_size > 32 * 1024**2
            ):
                raise ValueError("Bilingual Manga catalogue data is unexpectedly large")
            with bundle.open(data_name) as stream:
                data = json.load(stream)
            with bundle.open(metadata_name) as stream:
                metadata = json.load(stream)
    except (OSError, zipfile.BadZipFile, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("The Bilingual Manga catalogue ZIP is damaged or unsupported") from exc
    return build_catalog(data, metadata)


def install_catalog(source: Path, destination: Path) -> dict:
    catalog = catalog_from_zip(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(destination)
    return {"archives": len(catalog["archives"])}


def ensure_catalog(destination: Path, progress=None) -> dict:
    """Cache recognition metadata from the linked upstream archive when absent.

    The release does not redistribute the upstream data. This downloads only
    its public JSON metadata, never manga pages, and validates it through the
    same strict parser used by manual catalogue imports.
    """
    existing = load_catalog(destination)
    if existing:
        return {"archives": len(existing), "downloaded": False}
    if os.getenv("BMO_DISABLE_CATALOG_DOWNLOAD"):
        return {"archives": 0, "downloaded": False, "disabled": True}
    with DOWNLOAD_LOCK:
        existing = load_catalog(destination)
        if existing:
            return {"archives": len(existing), "downloaded": False}
        incoming = destination.parent / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        temporary = incoming / f"bilingual-catalog-{uuid.uuid4().hex}.zip"
        request = urllib.request.Request(
            CATALOG_DOWNLOAD_URL,
            headers={"User-Agent": "Bilingual-Manga-Reader/1.4.2"},
        )
        tls = ssl.create_default_context(cafile=certifi.where())
        total = 0
        try:
            if progress:
                progress("Downloading Bilingual Manga recognition metadata…")
            with urllib.request.urlopen(request, timeout=120, context=tls) as response, temporary.open("wb") as output:
                declared = int(response.headers.get("Content-Length", "0") or 0)
                if declared > MAX_CATALOG_DOWNLOAD:
                    raise ValueError("Bilingual Manga recognition metadata is unexpectedly large")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_CATALOG_DOWNLOAD:
                        raise ValueError("Bilingual Manga recognition metadata is unexpectedly large")
                    output.write(chunk)
            if not total:
                raise ValueError("Bilingual Manga recognition metadata download was empty")
            result = install_catalog(temporary, destination)
            return {**result, "downloaded": True, "source": CATALOG_SOURCE_PAGE}
        finally:
            temporary.unlink(missing_ok=True)


def load_catalog(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        archives = data.get("archives", {})
        return archives if isinstance(archives, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return {}


def identify_archive(archive: Path, catalog_path: Path) -> dict | None:
    alias = archive.stem
    match = load_catalog(catalog_path).get(alias)
    return dict(match) if isinstance(match, dict) else None


def looks_like_archive_id(path: Path) -> bool:
    return bool(_CID.fullmatch(path.stem))
