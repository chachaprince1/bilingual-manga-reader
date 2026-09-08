"""Loopback-only API for Bilingual Manga Reader and Mokuro Converter."""

from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .align import align, detect_offset, similarity
from .bilingual_catalog import (
    CATALOG_FILENAME,
    ensure_catalog,
    identify_archive,
    install_catalog,
    load_catalog,
    looks_like_archive_id,
)
from .bulk_import import scan_folder
from .converter import bootstrap_available, engine_ready, ensure_engine, render_pdf, run_mokuro
from .db import connect, delete_mappings_for_pages, insert_mapping
from .importer import (
    ARCHIVES,
    IMAGES,
    PDFS,
    image_features,
    images,
    infer_language,
    infer_metadata,
    make_thumbnail,
    mokuro_data,
    mokuro_images,
    mokuro_page,
    page_id,
    safe_extract,
)
from .mapping_import import normalize_data

VERSION = "1.4.1"
PRODUCT_NAME = "Bilingual Manga Reader and Mokuro Converter"
PREVIOUS_PRODUCT_NAMES = ("Mokuro & Bilingual Manga Reader", "Bilingual Manga Offline")
DEFAULT_PORT = 8765
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
DB: sqlite3.Connection | None = None
DB_LOCK = threading.RLock()
BULK_SCANS: dict[str, dict] = {}
BULK_LOCK = threading.RLock()
CONVERSION_JOBS: dict[str, dict] = {}
CONVERSION_LOCK = threading.RLock()
CONVERTER_RUN_LOCK = threading.Lock()
LOGGER = logging.getLogger("bilingual_manga")


def data_dir() -> Path:
    if os.name == "nt":
        return Path(os.getenv("BMO_DATA_DIR", Path(os.getenv("APPDATA", Path.home())) / PRODUCT_NAME))
    return Path(
        os.getenv(
            "BMO_DATA_DIR",
            Path.home() / "Library" / "Application Support" / PRODUCT_NAME,
        )
    )


def legacy_data_dir() -> Path:
    """Return the immediately previous data folder for compatibility helpers."""
    if os.name == "nt":
        return Path(os.getenv("APPDATA", Path.home())) / PREVIOUS_PRODUCT_NAMES[0]
    return Path.home() / "Library" / "Application Support" / PREVIOUS_PRODUCT_NAMES[0]


def previous_data_dirs() -> list[Path]:
    if os.name == "nt":
        base = Path(os.getenv("APPDATA", Path.home()))
    else:
        base = Path.home() / "Library" / "Application Support"
    return [base / name for name in PREVIOUS_PRODUCT_NAMES]


def initialize_data_dirs() -> Path:
    root = data_dir()
    if "BMO_DATA_DIR" not in os.environ:
        for legacy in previous_data_dirs():
            if not root.exists() and legacy.is_dir():
                legacy.rename(root)
                break
    for name in ("library", "incoming", "thumbnails", "backups", "logs", "converter"):
        (root / name).mkdir(parents=True, exist_ok=True)
    if not LOGGER.handlers:
        handler = RotatingFileHandler(root / "logs" / "app.log", maxBytes=1_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
    bundled_catalog = ROOT / "assets" / CATALOG_FILENAME
    local_catalog = root / CATALOG_FILENAME
    if not local_catalog.is_file() and bundled_catalog.is_file():
        shutil.copy2(bundled_catalog, local_catalog)
    return root


def _migrate_legacy_paths(root: Path, legacy: Path | None = None) -> int:
    """Rewrite managed absolute paths after the application-support folder rename."""
    root = root.resolve()
    legacy = (legacy or legacy_data_dir()).resolve()
    changed = 0
    for table, key, columns in (
        ("volumes", "id", ("source_path", "cover_path", "mokuro_path", "thumbnail_path", "origin_path")),
        ("pages", "id", ("path",)),
    ):
        for row in _db().execute(f"SELECT {key},{','.join(columns)} FROM {table}"):
            updates = {}
            for column in columns:
                value = row[column]
                if not value:
                    continue
                try:
                    relative = Path(value).relative_to(legacy)
                except ValueError:
                    continue
                updates[column] = str(root / relative)
            if updates:
                assignments = ",".join(f"{column}=?" for column in updates)
                _db().execute(
                    f"UPDATE {table} SET {assignments} WHERE {key}=?",
                    (*updates.values(), row[key]),
                )
                changed += 1
    if changed:
        _db().commit()
        LOGGER.info("Migrated %s stored paths to the rebranded data folder", changed)
    return changed


def open_reader(url: str) -> bool:
    """Open the ordinary installed Chrome profile without a temporary profile."""
    try:
        if platform.system() == "Darwin":
            chrome = Path("/Applications/Google Chrome.app")
            if chrome.exists():
                # Ask LaunchServices first. Executing Chrome's internal binary
                # can silently fail to hand the URL to an already-running
                # profile even though the new process exits successfully.
                result = subprocess.run(
                    ["open", "-a", "Google Chrome", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if result.returncode == 0:
                    return True
                # Keep a direct-binary fallback for unusual/headless macOS
                # environments where LaunchServices is unavailable.
                binary = chrome / "Contents" / "MacOS" / "Google Chrome"
                if binary.is_file():
                    subprocess.Popen([str(binary), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
        elif platform.system() == "Windows":
            candidates = [
                shutil.which("chrome"),
                os.path.join(
                    os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"
                ),
                os.path.join(
                    os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"
                ),
                os.path.join(
                    os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"
                ),
            ]
            chrome = next((candidate for candidate in candidates if candidate and os.path.exists(candidate)), None)
            if chrome:
                subprocess.Popen([chrome, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
    except OSError:
        LOGGER.exception("Could not start Chrome")
    return bool(webbrowser.open(url))


def choose_bulk_folder() -> Path | None:
    """Show the platform's native folder picker for a collection scan."""
    system = platform.system()
    if system == "Darwin":
        script = """
ObjC.import("AppKit");
var app = $.NSApplication.sharedApplication;
app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
app.activateIgnoringOtherApps(true);
var panel = $.NSOpenPanel.openPanel;
panel.setCanChooseFiles(false);
panel.setCanChooseDirectories(true);
panel.setAllowsMultipleSelection(false);
panel.setCanCreateDirectories(false);
panel.setMessage("Choose a folder containing manga editions");
panel.setPrompt("Scan This Folder");
var response = panel.runModal;
response === $.NSModalResponseOK ? ObjC.unwrap(panel.URL.path) : "";
""".strip()
        command = ["osascript", "-l", "JavaScript", "-e", script]
    elif system == "Windows":
        script = r"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Choose a folder containing manga editions'
$dialog.ShowNewFolderButton = $false
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.SelectedPath }
""".strip()
        command = ["powershell.exe", "-NoProfile", "-STA", "-Command", script]
    else:
        raise ValueError("Native folder selection is available on macOS and Windows")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        error = result.stderr.strip()
        if "User canceled" in error or "(-128)" in error:
            return None
        raise ValueError("The folder picker could not be opened")
    selected = result.stdout.strip()
    return Path(selected).expanduser().resolve() if selected else None


def choose_manga_source() -> Path | None:
    """Choose one manga directory, archive, or PDF with a native picker."""
    system = platform.system()
    if system == "Darwin":
        script = """
ObjC.import("AppKit");
var app = $.NSApplication.sharedApplication;
app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
app.activateIgnoringOtherApps(true);
var panel = $.NSOpenPanel.openPanel;
panel.setCanChooseFiles(true);
panel.setCanChooseDirectories(true);
panel.setAllowsMultipleSelection(false);
panel.setCanCreateDirectories(false);
panel.setMessage("Choose one manga folder, PDF, CBZ, ZIP, or TAR");
panel.setPrompt("Choose Manga");
var response = panel.runModal;
response === $.NSModalResponseOK ? ObjC.unwrap(panel.URL.path) : "";
""".strip()
        command = ["osascript", "-l", "JavaScript", "-e", script]
    elif system == "Windows":
        script = r"""
Add-Type -AssemblyName System.Windows.Forms
$answer = [System.Windows.Forms.MessageBox]::Show('Choose Yes for a manga folder, or No for a PDF/CBZ/ZIP/TAR file.', 'Choose one manga', [System.Windows.Forms.MessageBoxButtons]::YesNoCancel)
if ($answer -eq [System.Windows.Forms.DialogResult]::Yes) {
  $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
  $dialog.Description = 'Choose one manga folder'
  $dialog.ShowNewFolderButton = $false
  if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.SelectedPath }
} elseif ($answer -eq [System.Windows.Forms.DialogResult]::No) {
  $dialog = New-Object System.Windows.Forms.OpenFileDialog
  $dialog.Title = 'Choose one manga archive or PDF'
  $dialog.Filter = 'Manga files (*.pdf;*.cbz;*.zip;*.tar)|*.pdf;*.cbz;*.zip;*.tar|All files (*.*)|*.*'
  if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.FileName }
}
""".strip()
        command = ["powershell.exe", "-NoProfile", "-STA", "-Command", script]
    else:
        raise ValueError("Native manga selection is available on macOS and Windows")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        error = result.stderr.strip()
        if "User canceled" in error or "(-128)" in error:
            return None
        raise ValueError("The manga picker could not be opened")
    selected = result.stdout.strip()
    return Path(selected).expanduser().resolve() if selected else None


def describe_manga_source(path: Path) -> dict:
    path = path.expanduser().resolve()
    if not path.exists():
        raise ValueError("The selected manga is unavailable")
    if path.is_file() and path.suffix.lower() not in ARCHIVES | PDFS:
        raise ValueError("Choose an image folder, PDF, CBZ, ZIP, or TAR")
    has_mokuro = False
    if path.is_dir():
        has_mokuro = next(path.rglob("*.mokuro"), None) is not None
    kind = "folder" if path.is_dir() else "pdf" if path.suffix.lower() in PDFS else "archive"
    return {
        "cancelled": False,
        "path": str(path),
        "name": path.name,
        "kind": kind,
        "has_mokuro": has_mokuro,
    }


def _bulk_scan_snapshot(scan_id: str) -> dict:
    with BULK_LOCK:
        job = BULK_SCANS.get(scan_id)
        if not job:
            raise ValueError("Bulk scan was not found or has expired")
        # Jobs contain only JSON-compatible fields; round-tripping gives each
        # request an immutable snapshot while the scanner updates in parallel.
        return json.loads(json.dumps(job, ensure_ascii=False))


def _conversion_snapshot(job_id: str) -> dict:
    with CONVERSION_LOCK:
        job = CONVERSION_JOBS.get(job_id)
        if not job:
            raise ValueError("Conversion was not found or has expired")
        return json.loads(json.dumps(job, ensure_ascii=False))


def _conversion_update(job_id: str, status: str, message: str, **values) -> None:
    with CONVERSION_LOCK:
        job = CONVERSION_JOBS.get(job_id)
        if job:
            job.update(status=status, message=message, updated=time.time(), **values)


def _new_conversion_job(label: str, volume_id: str | None = None) -> dict:
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "status": "queued",
        "message": "Waiting for the local converter…",
        "label": label,
        "volume_id": volume_id,
        "error": "",
        "created": time.time(),
        "updated": time.time(),
    }
    with CONVERSION_LOCK:
        finished = sorted(
            (entry for entry in CONVERSION_JOBS.values() if entry["status"] in ("complete", "failed")),
            key=lambda entry: entry["created"],
        )
        while len(CONVERSION_JOBS) >= 12 and finished:
            CONVERSION_JOBS.pop(finished.pop(0)["id"], None)
        CONVERSION_JOBS[job_id] = job
    return job


def _mark_bulk_duplicates(items: list[dict]) -> None:
    with DB_LOCK:
        existing = [
            dict(row)
            for row in _db().execute(
                "SELECT id,source_path,origin_path,import_signature,language FROM volumes"
            )
        ]
    signatures = {
        row["import_signature"]: row["id"]
        for row in existing
        if row.get("import_signature")
    }
    origins = {
        row["origin_path"]: row["id"]
        for row in existing
        if row.get("origin_path")
    }
    source_languages = {
        (row["source_path"], row["language"]): row["id"] for row in existing
    }
    for item in items:
        if item.get("status") == "error":
            continue
        library_id = signatures.get(item.get("signature")) or origins.get(item["path"])
        if not library_id and item.get("language"):
            library_id = source_languages.get((item["path"], item["language"]))
        if library_id:
            item.update(
                {
                    "status": "existing",
                    "selected": False,
                    "library_id": library_id,
                    "error": "",
                    "note": "Already in your library; it will not be imported twice.",
                }
            )


def start_bulk_scan(folder: Path) -> dict:
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("The selected bulk-import folder is unavailable")
    scan_id = str(uuid.uuid4())
    job = {
        "id": scan_id,
        "root": str(folder),
        "status": "scanning",
        "processed": 0,
        "total": 0,
        "current": "Looking for manga…",
        "items": [],
        "error": "",
        "created": time.time(),
    }
    with BULK_LOCK:
        completed = sorted(
            (entry for entry in BULK_SCANS.values() if entry["status"] != "scanning"),
            key=lambda entry: entry["created"],
        )
        while len(BULK_SCANS) >= 8 and completed:
            BULK_SCANS.pop(completed.pop(0)["id"], None)
        BULK_SCANS[scan_id] = job

    def progress(processed: int, total: int, current: str) -> None:
        with BULK_LOCK:
            active = BULK_SCANS.get(scan_id)
            if active:
                active.update(processed=processed, total=total, current=current)

    def run_scan() -> None:
        try:
            catalog_path = initialize_data_dirs() / CATALOG_FILENAME
            if not load_catalog(catalog_path):
                try:
                    ensure_catalog(
                        catalog_path,
                        lambda message: progress(0, 0, message),
                    )
                except Exception as exc:
                    LOGGER.warning("Bilingual Manga recognition metadata is unavailable: %s", exc)
            items = scan_folder(
                folder,
                catalog_path,
                progress,
            )
            _mark_bulk_duplicates(items)
            with BULK_LOCK:
                active = BULK_SCANS.get(scan_id)
                if active:
                    active.update(
                        status="complete",
                        processed=active["total"],
                        current="Scan complete",
                        items=items,
                    )
        except Exception as exc:
            LOGGER.exception("Bulk scan failed for %s", folder)
            with BULK_LOCK:
                active = BULK_SCANS.get(scan_id)
                if active:
                    active.update(status="failed", error=str(exc), current="Scan failed")

    threading.Thread(target=run_scan, name=f"bulk-scan-{scan_id[:8]}", daemon=True).start()
    return _bulk_scan_snapshot(scan_id)


def _row(value):
    return dict(value) if value else None


def _db() -> sqlite3.Connection:
    if DB is None:
        raise RuntimeError("Database is not initialized")
    return DB


def _json_response(handler, payload, status: int = 200) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.end_headers()
    handler.wfile.write(raw)


def _read_json(handler, maximum: int = 20 * 1024 * 1024):
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0 or length > maximum:
        raise ValueError("Request size is invalid")
    try:
        return json.loads(handler.rfile.read(length))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Request contains invalid JSON") from exc


def _safe_relative(value: str) -> Path:
    clean = Path(unquote(value).replace("\\", "/"))
    if clean.is_absolute() or not clean.parts or ".." in clean.parts:
        raise ValueError("Unsafe filename")
    return clean


def _volume_available(volume: dict) -> tuple[bool, str]:
    source = Path(volume["source_path"])
    available = source.exists()
    if available and volume.get("cover_path"):
        available = Path(volume["cover_path"]).is_file()
    if not available:
        return False, "Manga files are missing or were moved. Relink this volume to keep its progress and mappings."
    if volume["language"] == "jp" and not volume.get("mokuro_path"):
        return True, "No .mokuro file was found. Pages work, but selectable Japanese OCR is unavailable."
    if volume["language"] == "jp" and not Path(volume["mokuro_path"]).is_file():
        return True, "The .mokuro file is missing. Relink the volume to restore selectable Japanese OCR."
    return True, ""


def library_rows() -> list[dict]:
    catalog = load_catalog(initialize_data_dirs() / CATALOG_FILENAME)
    with DB_LOCK:
        rows = [
            _row(item)
            for item in _db().execute(
                """SELECT v.*,p.page_id AS progress_page,p.updated_at AS last_read,
                (SELECT id FROM pages WHERE volume_id=v.id ORDER BY ordinal LIMIT 1) AS cover_page,
                (SELECT ordinal FROM pages WHERE id=p.page_id) AS progress_ordinal,
                (SELECT COUNT(*) FROM bookmarks b WHERE b.volume_id=v.id) AS bookmark_count,
                EXISTS(SELECT 1 FROM favorites f WHERE f.series=v.series) AS favorite
                FROM volumes v LEFT JOIN progress p ON p.volume_id=v.id
                ORDER BY CASE WHEN p.updated_at IS NULL THEN 1 ELSE 0 END,
                         p.updated_at DESC,v.created_at DESC"""
            )
        ]
        for volume in rows:
            content_type = volume.get("content_type") or ""
            origin_alias = Path(volume.get("origin_path") or "").stem
            if origin_alias and origin_alias in catalog:
                content_type = "bilingual"
            elif not content_type:
                if volume.get("mokuro_path"):
                    content_type = "mokuro"
                else:
                    content_type = "raw"
            if content_type != volume.get("content_type"):
                volume["content_type"] = content_type
                _db().execute(
                    "UPDATE volumes SET content_type=? WHERE id=?",
                    (content_type, volume["id"]),
                )
            available, error = _volume_available(volume)
            volume["available"] = int(available)
            volume["error"] = error
            volume["original_available"] = int(_external_original_path(volume) is not None)
            _db().execute(
                "UPDATE volumes SET available=?,error=? WHERE id=?",
                (int(available), error, volume["id"]),
            )
        _db().commit()
    return rows


def _serve_path(
    handler,
    path: Path,
    content_type: str | None = None,
    download: str | None = None,
    cache_control: str = "private, max-age=3600",
) -> None:
    if not path.is_file():
        handler.send_error(404)
        return
    handler.send_response(200)
    handler.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    handler.send_header("Content-Length", str(path.stat().st_size))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Cache-Control", cache_control)
    if download:
        handler.send_header("Content-Disposition", f'attachment; filename="{download}"')
    handler.end_headers()
    with path.open("rb") as source:
        shutil.copyfileobj(source, handler.wfile, length=1024 * 1024)


def _insert_group_mapping(
    series: str,
    jp_pages: list[str],
    en_pages: list[str],
    source: str,
    confidence: float,
    manual: bool = False,
) -> str:
    mapping_id = str(uuid.uuid4())
    insert_mapping(_db(), mapping_id, series, jp_pages, en_pages, source, confidence, manual)
    return mapping_id


def _page_rows(volume_id: str) -> list[dict]:
    return [
        dict(item)
        for item in _db().execute(
            "SELECT * FROM pages WHERE volume_id=? ORDER BY ordinal", (volume_id,)
        )
    ]


def _pair_volumes(volume_id: str, peer_id: str) -> dict:
    volume = _row(_db().execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone())
    peer = _row(_db().execute("SELECT * FROM volumes WHERE id=?", (peer_id,)).fetchone())
    if not volume or not peer:
        raise ValueError("One of the editions is no longer in the library")
    if volume["language"] == peer["language"]:
        raise ValueError("Pairing requires one Japanese and one English edition")
    ours = _page_rows(volume_id)
    theirs = _page_rows(peer_id)
    jp, en = (ours, theirs) if volume["language"] == "jp" else (theirs, ours)
    offset = detect_offset(jp, en)
    matches = []
    if offset:
        delta, confidence = offset
        used_en: set[int] = set()
        for j in range(len(jp)):
            e = j + delta
            if 0 <= e < len(en):
                score = similarity(jp[j]["fingerprint"], en[e]["fingerprint"])
                matches.append(([j], [e], min(confidence, score), "structural-offset"))
                used_en.add(e)
            else:
                matches.append(([j], [], 0.0, "structural-unmatched"))
        for e in range(len(en)):
            if e not in used_en:
                matches.append(([], [e], 0.0, "structural-unmatched"))
    else:
        matches = [
            (item.jp, item.en, item.confidence, "visual-sequence")
            for item in align(jp, en)
        ]
    page_ids = [page["id"] for page in ours]
    delete_mappings_for_pages(_db(), page_ids)
    mapped = 0
    for jp_indexes, en_indexes, score, source in matches:
        _insert_group_mapping(
            peer["series"],
            [jp[index]["id"] for index in jp_indexes],
            [en[index]["id"] for index in en_indexes],
            source,
            score,
        )
        if jp_indexes and en_indexes:
            mapped += 1
    return {"peer_id": peer_id, "pages_mapped": mapped}


def _pair_imported_volume(volume_id: str) -> dict | None:
    """Preserve exact historical pairing behavior and report the chosen peer."""
    volume = _row(_db().execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone())
    if not volume:
        return None
    peer = _db().execute(
        """SELECT id FROM volumes WHERE series=? AND language<>? AND id<>?
        AND volume_label=? ORDER BY created_at DESC LIMIT 1""",
        (volume["series"], volume["language"], volume_id, volume["volume_label"]),
    ).fetchone()
    return _pair_volumes(volume_id, peer["id"]) if peer else None


def _normalized_pair_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = re.sub(r"\b(?:volume|vol|chapter|ch|english|eng|japanese|jpn|raw|digital)\b", " ", value)
    value = re.sub(r"\d+(?:\.\d+)?", " ", value)
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip()


def _label_number(value: str) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(match.group()) if match else None


def pairing_candidates(volume_id: str, limit: int = 5) -> list[dict]:
    volume = _row(_db().execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone())
    if not volume:
        return []
    ours_name = _normalized_pair_name(volume["series"] or volume["title"])
    ours_number = _label_number(volume["volume_label"] or volume["chapter_label"])
    candidates: list[dict] = []
    for row in _db().execute(
        "SELECT * FROM volumes WHERE language<>? AND id<>? AND available=1",
        (volume["language"], volume_id),
    ):
        peer = dict(row)
        peer_number = _label_number(peer["volume_label"] or peer["chapter_label"])
        if ours_number is not None and peer_number is not None and ours_number != peer_number:
            continue
        peer_name = _normalized_pair_name(peer["series"] or peer["title"])
        name_score = SequenceMatcher(None, ours_name, peer_name).ratio() if ours_name and peer_name else 0.0
        ours_tokens = set(ours_name.split())
        peer_tokens = set(peer_name.split())
        token_score = len(ours_tokens & peer_tokens) / max(1, len(ours_tokens | peer_tokens))
        page_score = min(volume["page_count"], peer["page_count"]) / max(1, volume["page_count"], peer["page_count"])
        label_score = 1.0 if ours_number is not None and ours_number == peer_number else 0.35
        exact_name = bool(ours_name and ours_name == peer_name)
        score = max(0.0, min(1.0, 0.58 * max(name_score, token_score) + 0.24 * page_score + 0.18 * label_score))
        if exact_name:
            score = max(score, 0.97)
        if score < 0.55:
            continue
        candidates.append(
            {
                "id": peer["id"],
                "series": peer["series"],
                "title": peer["title"],
                "language": peer["language"],
                "volume_label": peer["volume_label"],
                "chapter_label": peer["chapter_label"],
                "page_count": peer["page_count"],
                "score": round(score, 4),
            }
        )
    return sorted(candidates, key=lambda item: (-item["score"], item["series"].casefold()))[:limit]


def auto_pair_imported_volume(volume_id: str) -> dict:
    exact = _pair_imported_volume(volume_id)
    if exact:
        return {"paired": True, **exact}
    candidates = pairing_candidates(volume_id)
    if not candidates:
        return {"paired": False}
    top = candidates[0]
    gap = top["score"] - (candidates[1]["score"] if len(candidates) > 1 else 0.0)
    if top["score"] >= 0.90 and gap >= 0.08:
        return {"paired": True, **confirm_pairing(volume_id, top["id"], commit=False)}
    volume = _row(_db().execute("SELECT title FROM volumes WHERE id=?", (volume_id,)).fetchone())
    return {
        "paired": False,
        "review": {
            "volume_id": volume_id,
            "title": volume["title"] if volume else "New edition",
            "candidates": candidates,
        },
    }


def confirm_pairing(volume_id: str, peer_id: str, *, commit: bool = True) -> dict:
    volume = _row(_db().execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone())
    peer = _row(_db().execute("SELECT * FROM volumes WHERE id=?", (peer_id,)).fetchone())
    if not volume or not peer:
        raise ValueError("One of the editions is no longer in the library")
    if volume["language"] == peer["language"]:
        raise ValueError("Choose an edition in the opposite language")
    ours_number = _label_number(volume["volume_label"] or volume["chapter_label"])
    peer_number = _label_number(peer["volume_label"] or peer["chapter_label"])
    if ours_number is not None and peer_number is not None and ours_number != peer_number:
        raise ValueError("These editions have different volume or chapter numbers")
    if volume["series"] != peer["series"]:
        _db().execute("UPDATE volumes SET series=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (peer["series"], volume_id))
    result = _pair_volumes(volume_id, peer_id)
    if commit:
        _db().commit()
    return {"ok": True, "series": peer["series"], **result}


def import_volume(
    source: Path,
    language: str = "auto",
    series: str | None = None,
    title: str | None = None,
    volume_label: str | None = None,
    chapter_label: str | None = None,
    source_kind: str = "referenced",
    *,
    mokuro_override: Path | None = None,
    pictures_override: list[Path] | None = None,
    origin_path: str | None = None,
    import_signature: str = "",
    content_type: str | None = None,
    friendly_names: bool = True,
    delete_original: bool = False,
) -> dict:
    source = source.expanduser().resolve()
    if not source.exists():
        raise ValueError("Selected manga does not exist")
    original_source = source
    requested_language = language if language in ("jp", "en") else "auto"
    origin = Path(origin_path).expanduser().resolve() if origin_path else original_source
    managed = initialize_data_dirs() / "library"
    managed_destination: Path | None = None
    bilingual_match = None
    if import_signature and _db().execute(
        "SELECT id FROM volumes WHERE import_signature=?", (import_signature,)
    ).fetchone():
        raise ValueError("This manga edition is already in the library")
    if source.suffix.lower() in ARCHIVES:
        bilingual_match = identify_archive(
            source, initialize_data_dirs() / CATALOG_FILENAME
        )
        if bilingual_match:
            language = bilingual_match["language"]
            series = series or bilingual_match["series"]
            title = title or bilingual_match["title"]
            volume_label = volume_label or bilingual_match["volume_label"]
        destination = managed / str(uuid.uuid4())
        managed_destination = destination
        try:
            safe_extract(source, destination)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        source = destination
        source_kind = "managed"
    elif delete_original and source_kind == "referenced" and source.is_dir():
        # A referenced directory cannot be removed safely because the library
        # would lose its pages. Make and verify an app-managed copy first.
        destination = managed / str(uuid.uuid4())
        try:
            shutil.copytree(source, destination)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        managed_destination = destination
        source = destination
        source_kind = "managed"
    if not source.is_dir():
        if managed_destination:
            shutil.rmtree(managed_destination, ignore_errors=True)
        raise ValueError("Choose an image folder, CBZ, ZIP, or TAR")
    if pictures_override is None:
        pictures = images(source)
    else:
        pictures = []
        for picture in pictures_override:
            candidate = picture.expanduser().resolve()
            if source not in candidate.parents or not candidate.is_file() or candidate.suffix.lower() not in IMAGES:
                if managed_destination:
                    shutil.rmtree(managed_destination, ignore_errors=True)
                raise ValueError("A scanned Mokuro page is no longer available")
            pictures.append(candidate)
    if not pictures:
        if managed_destination:
            shutil.rmtree(managed_destination, ignore_errors=True)
        archives = [
            path
            for path in source.rglob("*")
            if path.is_file() and path.suffix.lower() in ARCHIVES
        ]
        if archives:
            noun = "archive" if len(archives) == 1 else "archives"
            raise ValueError(
                f"This folder contains {len(archives)} manga {noun}. "
                "Use Scan folder so each edition is recognized and imported separately."
            )
        raise ValueError("No supported manga images were found")
    metadata_name = original_source.stem if original_source.is_file() else original_source.name
    metadata = infer_metadata(metadata_name)
    if mokuro_override:
        candidate = mokuro_override.expanduser().resolve()
        beside_source = candidate.parent == source.parent and candidate.stem.startswith(source.name)
        if (
            (source not in candidate.parents and not beside_source)
            or not candidate.is_file()
            or candidate.suffix.lower() != ".mokuro"
        ):
            if managed_destination:
                shutil.rmtree(managed_destination, ignore_errors=True)
            raise ValueError("The scanned Mokuro file is no longer available")
        mokuro_candidates = [candidate]
    else:
        mokuro_candidates = sorted(source.rglob("*.mokuro"))
    if not mokuro_candidates:
        mokuro_candidates = sorted(source.parent.glob(f"{source.name}*.mokuro"))
    if bilingual_match:
        language = bilingual_match["language"]
    elif mokuro_candidates:
        language = "jp"
    elif requested_language in ("jp", "en"):
        language = requested_language
    else:
        language = infer_language(metadata_name)
    if language == "en" and not mokuro_override:
        mokuro_candidates = []
    mokuro = mokuro_candidates[0] if mokuro_candidates else None
    mokuro_metadata = mokuro_data(mokuro) if mokuro else {}
    mokuro_title = mokuro_metadata.get("title")
    title = (title or mokuro_title or metadata["title"]).strip()
    inferred_series = metadata["series"]
    if mokuro_title:
        inferred_series = str(mokuro_title).strip()
    series = (series or inferred_series or mokuro_title or title).strip()
    mokuro_volume = str(mokuro_metadata.get("volume", "")).strip()
    volume_label = (volume_label or mokuro_volume or metadata["volume"]).strip()
    if volume_label and not re.search(r"(?:volume|vol\.?|巻)", volume_label, re.I):
        volume_label = f"Volume {volume_label}"
    chapter_label = (chapter_label or metadata["chapter"]).strip()
    if not title or not series:
        if managed_destination:
            shutil.rmtree(managed_destination, ignore_errors=True)
        raise ValueError("Series and title cannot be empty")
    if friendly_names and source_kind == "managed":
        current_root = _managed_root(str(source))
        if current_root and _is_opaque_managed_name(current_root.name):
            renamed_root = _available_managed_root(series, volume_label, chapter_label, language)
            current_root.rename(renamed_root)
            source = _rebase_path(source, current_root, renamed_root)
            pictures = [_rebase_path(path, current_root, renamed_root) for path in pictures]
            mokuro = _rebase_path(mokuro, current_root, renamed_root) if mokuro else None
            if managed_destination and (managed_destination == current_root or current_root in managed_destination.parents):
                managed_destination = renamed_root
    duplicate = _db().execute(
        """SELECT id FROM volumes WHERE language=? AND (
        source_path=? OR (origin_path<>'' AND origin_path=?) OR
        (?<>'' AND import_signature=?)) LIMIT 1""",
        (language, str(source), str(origin), import_signature, import_signature),
    ).fetchone()
    if duplicate:
        if managed_destination:
            shutil.rmtree(managed_destination, ignore_errors=True)
        raise ValueError("This manga edition is already in the library")

    volume_id = str(uuid.uuid4())
    content_type = (
        "bilingual"
        if bilingual_match
        else "mokuro"
        if mokuro
        else "raw"
    ) if content_type not in ("bilingual", "mokuro", "raw") else content_type
    try:
        thumb = make_thumbnail(
            pictures[0], initialize_data_dirs() / "thumbnails" / f"{volume_id}.jpg"
        )
        _db().execute(
            """INSERT INTO volumes(
            id,title,series,language,source_path,page_count,cover_path,mokuro_path,
            volume_label,chapter_label,source_kind,thumbnail_path,origin_path,import_signature,
            content_type)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                volume_id,
                title,
                series,
                language,
                str(source),
                len(pictures),
                str(pictures[0]),
                str(mokuro) if mokuro else None,
                volume_label,
                chapter_label,
                source_kind,
                str(thumb) if thumb else None,
                str(origin),
                import_signature,
                content_type,
            ),
        )
        for ordinal, picture in enumerate(pictures):
            features = image_features(picture)
            relative_parent = picture.relative_to(source).parent.as_posix()
            page_chapter = chapter_label or ("" if relative_parent == "." else relative_parent)
            _db().execute(
                """INSERT INTO pages(
                id,volume_id,ordinal,path,width,height,fingerprint,fingerprint_left,
                fingerprint_right,chapter_label) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    page_id(volume_id, picture, source),
                    volume_id,
                    ordinal,
                    str(picture),
                    features["width"],
                    features["height"],
                    features["fingerprint"],
                    features["fingerprint_left"],
                    features["fingerprint_right"],
                    page_chapter,
                ),
            )
        pairing = auto_pair_imported_volume(volume_id)
        _db().commit()
    except Exception:
        _db().rollback()
        (initialize_data_dirs() / "thumbnails" / f"{volume_id}.jpg").unlink(missing_ok=True)
        if managed_destination:
            shutil.rmtree(managed_destination, ignore_errors=True)
        raise
    warning = "" if language == "en" or mokuro else "No .mokuro file was found; OCR overlay is unavailable."
    originals_trashed = 0
    deletion_warning = ""
    if delete_original:
        if source_kind != "managed":
            deletion_warning = "The original was kept because this edition is referenced in place."
        else:
            candidate = _external_original_path({"origin_path": str(origin), "source_path": str(source), "source_kind": source_kind})
            if candidate:
                try:
                    _move_external_paths_to_trash([candidate])
                    originals_trashed = 1
                    _db().execute("UPDATE volumes SET origin_path='' WHERE id=?", (volume_id,))
                    _db().commit()
                except Exception as exc:
                    deletion_warning = f"The manga was imported, but the original could not be moved to Trash: {exc}"
    LOGGER.info("Imported %s (%s, %s pages)", title, language, len(pictures))
    return {
        "id": volume_id,
        "pages": len(pictures),
        "mokuro": bool(mokuro),
        "warning": warning,
        "bilingual_archive": bool(bilingual_match),
        "content_type": content_type,
        "language": language,
        "detected": bilingual_match,
        "pairing_review": pairing.get("review"),
        "paired": bool(pairing.get("paired")),
        "originals_trashed": originals_trashed,
        "deletion_warning": deletion_warning,
    }


def import_bulk_item(
    scan_id: str,
    item_id: str,
    metadata: dict,
    *,
    friendly_names: bool = True,
    delete_original: bool = False,
) -> dict:
    with BULK_LOCK:
        job = BULK_SCANS.get(scan_id)
        if not job or job["status"] not in ("complete", "importing"):
            raise ValueError("Bulk scan is not ready")
        item = next((entry for entry in job["items"] if entry["id"] == item_id), None)
        if not item:
            raise ValueError("Scanned manga item was not found")
        if item["status"] in ("existing", "imported"):
            raise ValueError("This manga edition is already in the library")
        if item["status"] == "error":
            raise ValueError(item.get("error") or "This scanned manga cannot be imported")
        if item["status"] == "importing":
            raise ValueError("This manga edition is already being imported")
        language = metadata.get("language") if metadata.get("language") in ("jp", "en") else item.get("language")
        if language not in ("jp", "en"):
            raise ValueError("Choose Japanese or English for this item")
        item["status"] = "importing"
        item["selected"] = False
        job["status"] = "importing"

    def field(name: str, limit: int) -> str | None:
        value = str(metadata.get(name, item.get(name, "")))[:limit].strip()
        return value or None

    try:
        path = Path(item["path"])
        kwargs = {
            "series": field("series", 300),
            "title": field("title", 300),
            "volume_label": field("volume_label", 100),
            "chapter_label": field("chapter_label", 100),
            "origin_path": item["path"],
            "import_signature": item.get("signature", ""),
            "content_type": metadata.get("content_type"),
            "friendly_names": friendly_names,
            "delete_original": delete_original and item["kind"] == "archive",
        }
        if item["kind"] == "mokuro":
            page_files = mokuro_images(path)
            with DB_LOCK:
                result = import_volume(
                    path.parent,
                    language,
                    source_kind="referenced",
                    mokuro_override=path,
                    pictures_override=page_files,
                    **kwargs,
                )
        else:
            with DB_LOCK:
                result = import_volume(path, language, source_kind="referenced", **kwargs)
    except Exception as exc:
        with BULK_LOCK:
            active = BULK_SCANS.get(scan_id)
            if active:
                active["status"] = "complete"
                current = next((entry for entry in active["items"] if entry["id"] == item_id), None)
                if current:
                    current.update(status="import-error", error=str(exc), selected=False)
        raise

    with BULK_LOCK:
        active = BULK_SCANS.get(scan_id)
        if active:
            active["status"] = "complete"
            current = next((entry for entry in active["items"] if entry["id"] == item_id), None)
            if current:
                current.update(
                    status="imported",
                    error="",
                    selected=False,
                    library_id=result["id"],
                    result=result,
                    note=f"Imported {result['pages']} pages.",
                )
    return result


def relink_volume(volume_id: str, source: Path, source_kind: str = "referenced") -> dict:
    volume = _row(_db().execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone())
    if not volume:
        raise ValueError("Volume was not found")
    source = source.expanduser().resolve()
    if source.suffix.lower() in ARCHIVES:
        destination = initialize_data_dirs() / "library" / str(uuid.uuid4())
        try:
            safe_extract(source, destination)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        source = destination
        source_kind = "managed"
    pictures = images(source) if source.is_dir() else []
    old_pages = _page_rows(volume_id)
    if len(pictures) != len(old_pages):
        raise ValueError(
            f"Relink requires the same {len(old_pages)} pages to preserve mappings; the selected source has {len(pictures)}."
        )
    for old, picture in zip(old_pages, pictures):
        features = image_features(picture)
        relative_parent = picture.relative_to(source).parent.as_posix()
        chapter = volume["chapter_label"] or ("" if relative_parent == "." else relative_parent)
        _db().execute(
            """UPDATE pages SET path=?,width=?,height=?,fingerprint=?,fingerprint_left=?,
            fingerprint_right=?,chapter_label=? WHERE id=?""",
            (
                str(picture),
                features["width"],
                features["height"],
                features["fingerprint"],
                features["fingerprint_left"],
                features["fingerprint_right"],
                chapter,
                old["id"],
            ),
        )
    mokuro_candidates = sorted(source.rglob("*.mokuro")) if volume["language"] == "jp" else []
    if volume["language"] == "jp" and not mokuro_candidates:
        mokuro_candidates = sorted(source.parent.glob(f"{source.name}*.mokuro"))
    mokuro = mokuro_candidates[0] if mokuro_candidates else None
    thumb = make_thumbnail(
        pictures[0], initialize_data_dirs() / "thumbnails" / f"{volume_id}.jpg"
    )
    _db().execute(
        """UPDATE volumes SET source_path=?,cover_path=?,mokuro_path=?,source_kind=?,
        thumbnail_path=?,available=1,error='',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (
            str(source),
            str(pictures[0]),
            str(mokuro) if mokuro else None,
            source_kind,
            str(thumb) if thumb else None,
            volume_id,
        ),
    )
    _db().commit()
    return {"ok": True, "id": volume_id, "pages": len(pictures)}


def _managed_root(path_value: str) -> Path | None:
    """Return the per-import managed root, never the library directory itself."""
    library = (initialize_data_dirs() / "library").resolve()
    try:
        relative = Path(path_value).expanduser().resolve().relative_to(library)
    except (OSError, ValueError):
        return None
    if not relative.parts:
        return None
    return library / relative.parts[0]


def _is_opaque_managed_name(name: str) -> bool:
    return bool(
        re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27}", name, re.I)
        or re.fullmatch(r"(?:pdf-|upload-)?[0-9a-f-]{24,}", name, re.I)
    )


def _friendly_component(value: str, fallback: str = "Manga") -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or fallback)[:90].rstrip(" .")


def _available_managed_root(
    series: str,
    volume_label: str = "",
    chapter_label: str = "",
    language: str = "jp",
) -> Path:
    library = initialize_data_dirs() / "library"
    language_label = "Japanese" if language == "jp" else "English"
    parts = [_friendly_component(series), _friendly_component(volume_label or chapter_label, "")]
    base = " - ".join(part for part in parts if part)
    base = _friendly_component(f"{base} - {language_label}")
    candidate = library / base
    suffix = 2
    while candidate.exists():
        candidate = library / f"{base} ({suffix})"
        suffix += 1
    return candidate


def _rebase_path(path: Path, old_root: Path, new_root: Path) -> Path:
    try:
        return new_root / path.resolve().relative_to(old_root.resolve())
    except ValueError:
        return path


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _external_original_path(row: dict) -> Path | None:
    root = initialize_data_dirs().resolve()
    for value in (row.get("origin_path"), row.get("source_path") if row.get("source_kind") == "referenced" else None):
        if not value:
            continue
        candidate = Path(value).expanduser().resolve()
        if candidate.exists() and not _path_inside(candidate, root):
            return candidate
    return None


def _unique_trash_target(trash: Path, name: str) -> Path:
    target = trash / name
    stem = Path(name).stem
    suffix = Path(name).suffix
    index = 2
    while target.exists():
        target = trash / f"{stem} ({index}){suffix}"
        index += 1
    return target


def _move_external_paths_to_trash(paths: list[Path]) -> list[str]:
    """Move exact user-selected sources to Trash/Recycle Bin after safety checks."""
    unique = list(dict.fromkeys(path.expanduser().resolve() for path in paths if path.exists()))
    home = Path.home().resolve()
    protected = {Path(path).resolve() for path in (Path(path.anchor) for path in unique)}
    protected.update({home, home / "Desktop", home / "Downloads", home / "Documents"})
    data_root = initialize_data_dirs().resolve()
    for path in unique:
        if path in protected or _path_inside(path, data_root):
            raise ValueError(f"Refusing to remove a broad or app-managed path: {path}")
    if not unique:
        return []
    if platform.system() == "Windows" and "BMO_TRASH_DIR" not in os.environ:
        for path in unique:
            quoted = str(path).replace("'", "''")
            method = "DeleteDirectory" if path.is_dir() else "DeleteFile"
            script = (
                "Add-Type -AssemblyName Microsoft.VisualBasic; "
                f"[Microsoft.VisualBasic.FileIO.FileSystem]::{method}('{quoted}', "
                "[Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, "
                "[Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise OSError(result.stderr.strip() or f"Could not move {path.name} to the Recycle Bin")
        return [path.name for path in unique]
    trash = Path(os.getenv("BMO_TRASH_DIR", str(home / ".Trash"))).expanduser()
    trash.mkdir(parents=True, exist_ok=True)
    moved = []
    for path in unique:
        target = _unique_trash_target(trash, path.name)
        shutil.move(str(path), str(target))
        moved.append(target.name)
    return moved


def organize_managed_library_names() -> int:
    """Replace old opaque managed-folder IDs with stable human-readable names."""
    library = (initialize_data_dirs() / "library").resolve()
    changed = 0
    roots: dict[Path, list[dict]] = {}
    for row in _db().execute("SELECT * FROM volumes WHERE source_kind='managed'"):
        item = dict(row)
        root = _managed_root(item["source_path"])
        if root and root.exists() and _is_opaque_managed_name(root.name):
            roots.setdefault(root, []).append(item)
    for old_root, volumes in roots.items():
        first = volumes[0]
        new_root = _available_managed_root(
            first["series"], first["volume_label"], first["chapter_label"], first["language"]
        )
        try:
            old_root.rename(new_root)
            for volume in volumes:
                updates = {}
                for column in ("source_path", "cover_path", "mokuro_path", "origin_path"):
                    value = volume.get(column)
                    if value and _path_inside(Path(value), old_root):
                        updates[column] = str(_rebase_path(Path(value), old_root, new_root))
                if updates:
                    _db().execute(
                        f"UPDATE volumes SET {','.join(f'{key}=?' for key in updates)} WHERE id=?",
                        (*updates.values(), volume["id"]),
                    )
                for page in _db().execute("SELECT id,path FROM pages WHERE volume_id=?", (volume["id"],)):
                    if _path_inside(Path(page["path"]), old_root):
                        _db().execute(
                            "UPDATE pages SET path=? WHERE id=?",
                            (str(_rebase_path(Path(page["path"]), old_root, new_root)), page["id"]),
                        )
            _db().commit()
            changed += 1
        except Exception:
            _db().rollback()
            if new_root.exists() and not old_root.exists():
                new_root.rename(old_root)
            LOGGER.exception("Could not organize managed folder %s", old_root)
    return changed


def remove_from_library(
    *,
    volume_id: str | None = None,
    series: str | None = None,
    delete_originals: bool = False,
) -> dict:
    """Remove editions and, only on request, move their external originals to Trash."""
    if bool(volume_id) == bool(series):
        raise ValueError("Choose one edition or one series to remove")
    with DB_LOCK:
        if volume_id:
            rows = [
                dict(row)
                for row in _db().execute("SELECT * FROM volumes WHERE id=?", (volume_id,))
            ]
        else:
            rows = [
                dict(row)
                for row in _db().execute("SELECT * FROM volumes WHERE series=?", (series,))
            ]
        if not rows:
            raise ValueError("The selected manga is no longer in the library")
        ids = [row["id"] for row in rows]
        page_ids: list[str] = []
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            page_ids.extend(
                row[0]
                for row in _db().execute(
                    f"SELECT id FROM pages WHERE volume_id IN ({','.join('?' for _ in chunk)})",
                    chunk,
                )
            )
        candidates = {
            root
            for row in rows
            if row["source_kind"] == "managed"
            for root in [_managed_root(row["source_path"])]
            if root is not None
        }
        # Never delete a managed directory still referenced by a retained edition.
        managed_roots: list[Path] = []
        for candidate in candidates:
            shared = False
            for other in _db().execute(
                f"SELECT id,source_path FROM volumes WHERE id NOT IN ({','.join('?' for _ in ids)})",
                ids,
            ):
                try:
                    Path(other["source_path"]).resolve().relative_to(candidate.resolve())
                    shared = True
                    break
                except (OSError, ValueError):
                    continue
            if not shared:
                managed_roots.append(candidate)

        originals: list[Path] = []
        if delete_originals:
            retained = [dict(row) for row in _db().execute(
                f"SELECT source_path,source_kind,origin_path FROM volumes WHERE id NOT IN ({','.join('?' for _ in ids)})",
                ids,
            )]
            for row in rows:
                candidate = _external_original_path(row)
                if not candidate:
                    continue
                shared = any(
                    candidate == _external_original_path(other)
                    or any(
                        value and Path(value).expanduser().resolve() == candidate
                        for value in (other.get("source_path"), other.get("origin_path"))
                    )
                    for other in retained
                )
                if not shared and candidate not in originals:
                    originals.append(candidate)

        staged: list[tuple[Path, Path]] = []
        staging_root = initialize_data_dirs() / "incoming"
        try:
            for candidate in managed_roots:
                if not candidate.exists():
                    continue
                staged_path = staging_root / f"removing-{uuid.uuid4()}"
                candidate.rename(staged_path)
                staged.append((candidate, staged_path))
            delete_mappings_for_pages(_db(), page_ids)
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                _db().execute(
                    f"DELETE FROM volumes WHERE id IN ({','.join('?' for _ in chunk)})", chunk
                )
            affected_series = {row["series"] for row in rows}
            for name in affected_series:
                if not _db().execute("SELECT 1 FROM volumes WHERE series=? LIMIT 1", (name,)).fetchone():
                    _db().execute("DELETE FROM favorites WHERE series=?", (name,))
            _db().commit()
        except Exception:
            _db().rollback()
            for original, staged_path in reversed(staged):
                if staged_path.exists() and not original.exists():
                    staged_path.rename(original)
            raise
    for row in rows:
        if row.get("thumbnail_path"):
            Path(row["thumbnail_path"]).unlink(missing_ok=True)
    if staged:
        threading.Thread(
            target=lambda: [shutil.rmtree(path, ignore_errors=True) for _, path in staged],
            name="library-removal-cleanup",
            daemon=True,
        ).start()
    originals_trashed: list[str] = []
    deletion_warning = ""
    if originals:
        try:
            originals_trashed = _move_external_paths_to_trash(originals)
        except Exception as exc:
            deletion_warning = f"Library entries were removed, but the originals were kept: {exc}"
    LOGGER.info(
        "Removed %s editions from the library; %s external originals moved to Trash",
        len(rows),
        len(originals_trashed),
    )
    return {
        "ok": True,
        "removed": len(rows),
        "series": sorted({row["series"] for row in rows}),
        "managed_copies_deleted": len(staged),
        "originals_preserved": not bool(originals_trashed),
        "originals_trashed": len(originals_trashed),
        "deletion_warning": deletion_warning,
    }


def _copy_volume_pages(volume: dict, page_rows: list[dict], target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=False)
    source_root = Path(volume["source_path"]).resolve()
    copied: list[Path] = []
    used: set[Path] = set()
    for index, page in enumerate(page_rows):
        source = Path(page["path"]).resolve()
        if not source.is_file():
            raise ValueError(f"Page {index + 1} is missing; relink this edition before converting it")
        try:
            relative = source.relative_to(source_root)
        except ValueError:
            relative = Path(f"{index + 1:05d}{source.suffix.lower()}")
        if relative.is_absolute() or ".." in relative.parts or relative in used:
            relative = Path(f"{index + 1:05d}{source.suffix.lower()}")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(destination)
        used.add(relative)
    return copied


def _run_existing_volume_conversion(
    job_id: str, volume_id: str, delete_original: bool = False
) -> None:
    root = initialize_data_dirs()
    work_root: Path | None = None
    created_work_root = False
    try:
        with CONVERTER_RUN_LOCK:
            with DB_LOCK:
                volume = _row(_db().execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone())
                pages_before = _page_rows(volume_id) if volume else []
            if not volume:
                raise ValueError("The edition is no longer in the library")
            if volume["language"] != "jp":
                raise ValueError("Mokuro conversion is for Japanese manga editions")
            if volume.get("mokuro_path") and Path(volume["mokuro_path"]).is_file():
                _conversion_update(job_id, "complete", "This edition already has selectable Mokuro text.")
                return
            original = _external_original_path(volume) if delete_original else None
            old_managed = _managed_root(volume["source_path"]) if volume["source_kind"] == "managed" else None
            free = shutil.disk_usage(root).free
            source_bytes = sum(Path(page["path"]).stat().st_size for page in pages_before if Path(page["path"]).is_file())
            required_copy = 0 if old_managed else source_bytes
            if free < required_copy + 3 * 1024**3:
                raise RuntimeError("Not enough free disk space for the local conversion and OCR models")

            def progress(status: str, message: str) -> None:
                _conversion_update(job_id, status, message)

            executable = ensure_engine(root, progress)
            if old_managed:
                work_root = old_managed
                pages_root = Path(volume["source_path"])
                copied = [Path(page["path"]) for page in pages_before]
                _conversion_update(job_id, "preparing", "Verifying the app-managed pages…")
            else:
                work_root = _available_managed_root(
                    volume["series"], volume["volume_label"], volume["chapter_label"], volume["language"]
                )
                created_work_root = True
                pages_root = work_root / "pages"
                _conversion_update(job_id, "preparing", "Copying pages into the app's private library…")
                copied = _copy_volume_pages(volume, pages_before, pages_root)
            mokuro = run_mokuro(executable, pages_root, root, progress)
            if old_managed and not _path_inside(mokuro, old_managed):
                destination = old_managed / "OCR.mokuro"
                if destination.exists():
                    destination = old_managed / f"OCR-{uuid.uuid4().hex[:8]}.mokuro"
                shutil.move(str(mokuro), str(destination))
                mokuro = destination
            data = mokuro_data(mokuro)
            if len(data.get("pages", [])) != len(copied):
                raise RuntimeError("Mokuro output did not contain every page; the original library entry was left unchanged")
            with DB_LOCK:
                for before, destination in zip(pages_before, copied):
                    _db().execute("UPDATE pages SET path=? WHERE id=?", (str(destination), before["id"]))
                _db().execute(
                    """UPDATE volumes SET source_path=?,cover_path=?,mokuro_path=?,source_kind='managed',
                    content_type=?,available=1,error='',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (
                        str(pages_root),
                        str(copied[0]),
                        str(mokuro),
                        "bilingual" if volume.get("content_type") == "bilingual" else "mokuro",
                        volume_id,
                    ),
                )
                _db().commit()
            originals_trashed = []
            deletion_warning = ""
            if original:
                try:
                    originals_trashed = _move_external_paths_to_trash([original])
                    with DB_LOCK:
                        _db().execute("UPDATE volumes SET origin_path='' WHERE id=?", (volume_id,))
                        _db().commit()
                except Exception as exc:
                    deletion_warning = f" The original was kept: {exc}"
            _conversion_update(
                job_id,
                "complete",
                f"Converted {len(copied)} pages. Selectable Japanese text is ready.{deletion_warning}",
                volume_id=volume_id,
                originals_trashed=len(originals_trashed),
            )
    except Exception as exc:
        LOGGER.exception("Mokuro conversion failed for %s", volume_id)
        if created_work_root and work_root:
            shutil.rmtree(work_root, ignore_errors=True)
        _conversion_update(job_id, "failed", "Conversion stopped", error=str(exc))


def start_volume_conversion(volume_id: str, delete_original: bool = False) -> dict:
    with CONVERSION_LOCK:
        existing = next(
            (
                job
                for job in CONVERSION_JOBS.values()
                if job.get("volume_id") == volume_id and job["status"] not in ("complete", "failed")
            ),
            None,
        )
        if existing:
            if delete_original:
                existing["delete_original"] = True
            return json.loads(json.dumps(existing))
    with DB_LOCK:
        volume = _row(_db().execute("SELECT id,title,series,language,mokuro_path FROM volumes WHERE id=?", (volume_id,)).fetchone())
    if not volume:
        raise ValueError("The edition is no longer in the library")
    if volume["language"] != "jp":
        raise ValueError("Mokuro conversion is only needed for Japanese manga")
    job = _new_conversion_job(volume["title"], volume_id)
    job["delete_original"] = delete_original
    threading.Thread(
        target=_run_existing_volume_conversion,
        args=(job["id"], volume_id, delete_original),
        name=f"mokuro-{job['id'][:8]}",
        daemon=True,
    ).start()
    return _conversion_snapshot(job["id"])


def _run_pdf_conversion(
    job_id: str,
    pdf: Path,
    metadata: dict,
    delete_original: bool = False,
    friendly_names: bool = True,
) -> None:
    root = initialize_data_dirs()
    original_pdf = pdf.expanduser().resolve()
    work_root: Path | None = None
    copied_external = False
    try:
        with CONVERTER_RUN_LOCK:
            def progress(status: str, message: str) -> None:
                _conversion_update(job_id, status, message)

            inferred = infer_metadata(original_pdf.stem)
            series = metadata.get("series") or inferred["series"]
            volume_label = metadata.get("volume_label") or inferred["volume"]
            chapter_label = metadata.get("chapter_label") or inferred["chapter"]
            if _path_inside(original_pdf, root / "library"):
                current_root = _managed_root(str(original_pdf))
                assert current_root is not None
                work_root = current_root
                if friendly_names and _is_opaque_managed_name(current_root.name):
                    renamed = _available_managed_root(series, volume_label, chapter_label, "jp")
                    current_root.rename(renamed)
                    original_pdf = _rebase_path(original_pdf, current_root, renamed)
                    work_root = renamed
                pdf = original_pdf
            else:
                work_root = _available_managed_root(series, volume_label, chapter_label, "jp")
                work_root.mkdir(parents=True, exist_ok=False)
                pdf = work_root / f"source{original_pdf.suffix.lower()}"
                shutil.copy2(original_pdf, pdf)
                copied_external = True
            pages = work_root / "pages"
            executable = ensure_engine(root, progress)
            render_pdf(executable, pdf, pages, root, progress)
            mokuro = run_mokuro(executable, pages, root, progress)
            with DB_LOCK:
                result = import_volume(
                    pages,
                    "jp",
                    series,
                    metadata.get("title") or inferred["title"],
                    volume_label,
                    chapter_label,
                    "managed",
                    mokuro_override=mokuro,
                    origin_path=str(original_pdf),
                    content_type=metadata.get("content_type"),
                    friendly_names=False,
                )
            originals_trashed = []
            deletion_warning = ""
            if delete_original and not _path_inside(original_pdf, root):
                try:
                    originals_trashed = _move_external_paths_to_trash([original_pdf])
                    with DB_LOCK:
                        _db().execute("UPDATE volumes SET origin_path='' WHERE id=?", (result["id"],))
                        _db().commit()
                except Exception as exc:
                    deletion_warning = f" The original PDF was kept: {exc}"
            _conversion_update(
                job_id,
                "complete",
                f"Converted and added {result['pages']} PDF pages with selectable Japanese text.{deletion_warning}",
                volume_id=result["id"],
                pairing_review=result.get("pairing_review"),
                originals_trashed=len(originals_trashed),
            )
    except Exception as exc:
        LOGGER.exception("PDF Mokuro conversion failed for %s", pdf)
        if copied_external and work_root:
            shutil.rmtree(work_root, ignore_errors=True)
        _conversion_update(job_id, "failed", "Conversion stopped", error=str(exc))


def start_pdf_conversion(
    pdf: Path,
    metadata: dict,
    delete_original: bool = False,
    friendly_names: bool = True,
) -> dict:
    job = _new_conversion_job(pdf.stem)
    threading.Thread(
        target=_run_pdf_conversion,
        args=(job["id"], pdf, metadata, delete_original, friendly_names),
        name=f"mokuro-pdf-{job['id'][:8]}",
        daemon=True,
    ).start()
    return _conversion_snapshot(job["id"])


def _upload_metadata(upload_id: str) -> tuple[Path, dict]:
    if not re.fullmatch(r"[0-9a-f-]{36}", upload_id):
        raise ValueError("Invalid upload identifier")
    folder = initialize_data_dirs() / "incoming" / upload_id
    metadata_path = folder / ".upload.json"
    if not metadata_path.is_file():
        raise ValueError("Upload was not found or expired")
    return folder, json.loads(metadata_path.read_text(encoding="utf-8"))


def _finish_upload(upload_id: str) -> dict:
    folder, metadata = _upload_metadata(upload_id)
    destination = initialize_data_dirs() / "library" / upload_id
    metadata_path = folder / ".upload.json"
    metadata_path.unlink(missing_ok=True)
    if destination.exists():
        raise ValueError("Import destination already exists")
    folder.rename(destination)
    archive_entries = [item for item in destination.iterdir() if item.is_file() and item.suffix.lower() in ARCHIVES]
    pdf_entries = [item for item in destination.iterdir() if item.is_file() and item.suffix.lower() in PDFS]
    if len(pdf_entries) == 1 and not images(destination) and not archive_entries:
        return {
            "pending": True,
            "conversion_job": start_pdf_conversion(
                pdf_entries[0],
                metadata,
                friendly_names=bool(metadata.get("friendly_names", True)),
            ),
        }
    entries = archive_entries
    archive_upload = len(entries) == 1 and not images(destination)
    visible_children = [item for item in destination.iterdir() if not item.name.startswith(".")]
    single_folder = (
        visible_children[0]
        if len(visible_children) == 1 and visible_children[0].is_dir()
        else None
    )
    source = entries[0] if archive_upload else single_folder or destination
    try:
        if metadata.get("relink_volume_id"):
            result = relink_volume(metadata["relink_volume_id"], source, "managed")
        else:
            result = import_volume(
                source,
                metadata.get("language", "jp"),
                metadata.get("series"),
                metadata.get("title"),
                metadata.get("volume_label"),
                metadata.get("chapter_label"),
                "managed",
                content_type=metadata.get("content_type"),
                friendly_names=bool(metadata.get("friendly_names", True)),
            )
    except Exception:
        # Keep uploaded files so a recoverable metadata/import error never deletes user input.
        LOGGER.exception("Upload finalization failed; files retained at %s", destination)
        raise
    if archive_upload:
        # The uploaded archive is a managed working copy; import_volume has
        # already extracted a durable managed folder, so avoid a duplicate
        # library candidate on the next scan. The user's original file is
        # untouched because this folder was created by the browser upload.
        shutil.rmtree(destination, ignore_errors=True)
    if metadata.get("convert") and result.get("content_type") == "raw" and result.get("language") == "jp":
        result["conversion_job"] = start_volume_conversion(result["id"])
    return result


def _ensure_catalog_for_source(source: Path) -> None:
    """Guarantee CID archives can be named without blocking normal raw imports."""
    if not source.exists():
        return
    candidates = [source] if source.is_file() else [
        item for item in source.iterdir() if item.is_file() and item.suffix.lower() in ARCHIVES
    ]
    if not any(item.suffix.lower() in ARCHIVES and looks_like_archive_id(item) for item in candidates):
        return
    catalog_path = initialize_data_dirs() / CATALOG_FILENAME
    if load_catalog(catalog_path):
        return
    try:
        ensure_catalog(catalog_path)
    except Exception as exc:
        LOGGER.warning("Could not fetch Bilingual Manga recognition metadata: %s", exc)


def _page_groups(series: str, side: str) -> list[list[dict]]:
    groups: list[list[dict]] = []
    volumes = [
        row[0]
        for row in _db().execute(
            "SELECT id FROM volumes WHERE series=? AND language=? ORDER BY created_at,id",
            (series, side),
        )
    ]
    for volume_id in volumes:
        pages = _page_rows(volume_id)
        labels: list[str] = []
        for page in pages:
            label = page["chapter_label"] or "__volume__"
            if label not in labels:
                labels.append(label)
        for label in labels:
            groups.append([page for page in pages if (page["chapter_label"] or "__volume__") == label])
    return groups


def _resolve_mapping_ref(series: str, side: str, reference: str) -> str | None:
    reference = str(reference).split(".")[0]
    exact = _db().execute(
        """SELECT p.id FROM pages p JOIN volumes v ON v.id=p.volume_id
        WHERE v.series=? AND v.language=? AND p.id=?""",
        (series, side, reference),
    ).fetchone()
    if exact:
        return exact["id"]
    for candidate in _db().execute(
        """SELECT p.id,p.path FROM pages p JOIN volumes v ON v.id=p.volume_id
        WHERE v.series=? AND v.language=?""",
        (series, side),
    ):
        if Path(candidate["path"]).stem.casefold() == reference.casefold():
            return candidate["id"]
    match = re.search(r"(\d+)_(\d+)$", reference)
    if match:
        chapter, page = map(int, match.groups())
        groups = _page_groups(series, side)
        if 0 <= chapter < len(groups) and 0 <= page < len(groups[chapter]):
            return groups[chapter][page]["id"]
    return None


def import_archived_mappings(series: str, data) -> dict:
    normalized = normalize_data(data)
    inserted = unresolved = review = 0
    for mapping in normalized:
        jp = [resolved for ref in mapping["jp_pages"] if (resolved := _resolve_mapping_ref(series, "jp", ref))]
        en = [resolved for ref in mapping["en_pages"] if (resolved := _resolve_mapping_ref(series, "en", ref))]
        if len(jp) != len(mapping["jp_pages"]) or len(en) != len(mapping["en_pages"]):
            unresolved += 1
            continue
        scores = []
        for jp_id, en_id in zip(jp, en):
            jp_hash = _db().execute("SELECT fingerprint FROM pages WHERE id=?", (jp_id,)).fetchone()[0]
            en_hash = _db().execute("SELECT fingerprint FROM pages WHERE id=?", (en_id,)).fetchone()[0]
            scores.append(similarity(jp_hash, en_hash))
        confidence = 1.0 if all(ref in jp + en for ref in mapping["jp_pages"] + mapping["en_pages"]) else 0.0
        if scores:
            confidence = min(scores)
        confidence = 0.92 if confidence >= 0.78 else 0.55
        if confidence < 0.80:
            review += 1
        delete_mappings_for_pages(_db(), jp + en)
        _insert_group_mapping(series, jp, en, "bilingual-manga-validated", confidence)
        inserted += 1
    _db().commit()
    return {"inserted": inserted, "unresolved": unresolved, "review": review}


def _infer_manual_neighbors(jp_id: str, en_id: str, series: str) -> int:
    jp = _db().execute("SELECT volume_id,ordinal FROM pages WHERE id=?", (jp_id,)).fetchone()
    en = _db().execute("SELECT volume_id,ordinal FROM pages WHERE id=?", (en_id,)).fetchone()
    if not jp or not en:
        return 0
    added = 0
    for delta in (-1, 1):
        jp_neighbor = _db().execute(
            "SELECT id,fingerprint FROM pages WHERE volume_id=? AND ordinal=?",
            (jp["volume_id"], jp["ordinal"] + delta),
        ).fetchone()
        en_neighbor = _db().execute(
            "SELECT id,fingerprint FROM pages WHERE volume_id=? AND ordinal=?",
            (en["volume_id"], en["ordinal"] + delta),
        ).fetchone()
        if not jp_neighbor or not en_neighbor:
            continue
        occupied = _db().execute(
            "SELECT 1 FROM mapping_pages WHERE page_id IN (?,?) LIMIT 1",
            (jp_neighbor["id"], en_neighbor["id"]),
        ).fetchone()
        score = similarity(jp_neighbor["fingerprint"], en_neighbor["fingerprint"])
        if not occupied and score >= 0.88:
            _insert_group_mapping(
                series,
                [jp_neighbor["id"]],
                [en_neighbor["id"]],
                "manual-anchor-neighbor",
                score,
            )
            added += 1
    return added


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # TCPServer normally calls socket.getfqdn(), which can block on a
        # reverse-DNS query. Offline startup must never depend on DNS.
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


class Handler(SimpleHTTPRequestHandler):
    server_version = "BilingualMangaReader/1.4.1"

    def log_message(self, fmt, *args):
        LOGGER.info("%s - %s", self.client_address[0], fmt % args)

    def _trusted(self) -> bool:
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        host = self.headers.get("Host", "").split(":", 1)[0]
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            return False
        return self.headers.get("Sec-Fetch-Site", "same-origin") not in ("cross-site",)

    def _require_trusted(self) -> bool:
        if self._trusted():
            return True
        _json_response(self, {"error": "Local same-origin access is required"}, 403)
        return False

    def do_GET(self):
        if not self._require_trusted():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/health":
                return _json_response(self, {"ok": True, "version": VERSION, "loopback": True})
            if path == "/api/status":
                root = initialize_data_dirs()
                catalog = load_catalog(root / CATALOG_FILENAME)
                return _json_response(
                    self,
                    {
                        "version": VERSION,
                        "data_dir": str(root),
                        "library_dir": str(root / "library"),
                        "log_file": str(root / "logs" / "app.log"),
                        "chrome": Path("/Applications/Google Chrome.app").exists()
                        if platform.system() == "Darwin"
                        else None,
                        "bilingual_catalog_archives": len(catalog),
                        "converter_installed": engine_ready(root),
                        "converter_python_available": engine_ready(root) or bootstrap_available(),
                        "converter_dir": str(root / "converter"),
                        "default_port": DEFAULT_PORT,
                        "port": self.server.server_address[1],
                    },
                )
            if path == "/api/conversion/status":
                job_id = parse_qs(parsed.query).get("job_id", [""])[0]
                return _json_response(self, _conversion_snapshot(job_id))
            if path == "/api/bulk/status":
                scan_id = parse_qs(parsed.query).get("scan_id", [""])[0]
                return _json_response(self, _bulk_scan_snapshot(scan_id))
            if path == "/api/settings":
                with DB_LOCK:
                    settings = {
                        item["key"]: json.loads(item["value"])
                        for item in _db().execute("SELECT key,value FROM settings")
                    }
                return _json_response(self, settings)
            if path == "/api/library":
                return _json_response(self, library_rows())
            if path.startswith("/api/volume/"):
                volume_id = path.rsplit("/", 1)[-1]
                with DB_LOCK:
                    volume = _row(
                        _db().execute(
                            """SELECT v.*,p.page_id AS progress_page,p.updated_at AS last_read
                            FROM volumes v LEFT JOIN progress p ON p.volume_id=v.id WHERE v.id=?""",
                            (volume_id,),
                        ).fetchone()
                    )
                    if not volume:
                        return _json_response(self, {"error": "Volume was not found"}, 404)
                    volume["pages"] = _page_rows(volume_id)
                    volume["bookmark_page_ids"] = [
                        item[0]
                        for item in _db().execute(
                            "SELECT page_id FROM bookmarks WHERE volume_id=? ORDER BY created_at",
                            (volume_id,),
                        )
                    ]
                    available, error = _volume_available(volume)
                    volume["available"], volume["error"] = int(available), error
                return _json_response(self, volume)
            if path.startswith("/api/corresponding/"):
                page = path.rsplit("/", 1)[-1]
                side = parse_qs(parsed.query).get("side", ["jp"])[0]
                if side not in ("jp", "en"):
                    raise ValueError("Language side is invalid")
                with DB_LOCK:
                    mapping = _db().execute(
                        """SELECT m.* FROM mappings m JOIN mapping_pages mp ON mp.mapping_id=m.id
                        WHERE mp.page_id=? AND mp.side=?
                        ORDER BY m.manual DESC,m.confidence DESC,m.created_at DESC LIMIT 1""",
                        (page, side),
                    ).fetchone()
                    if not mapping or (not mapping["manual"] and mapping["confidence"] < 0.80):
                        return _json_response(self, {"page_ids": [], "review": bool(mapping)})
                    other = "en" if side == "jp" else "jp"
                    pages = [
                        dict(item)
                        for item in _db().execute(
                            """SELECT p.id,p.volume_id,mp.position FROM mapping_pages mp
                            JOIN pages p ON p.id=mp.page_id
                            WHERE mp.mapping_id=? AND mp.side=? ORDER BY mp.position""",
                            (mapping["id"], other),
                        )
                    ]
                return _json_response(
                    self,
                    {
                        "page_ids": [item["id"] for item in pages],
                        "volume_id": pages[0]["volume_id"] if pages else None,
                        "confidence": mapping["confidence"],
                        "source": mapping["source"],
                    },
                )
            if path.startswith("/api/mokuro/"):
                page = path.rsplit("/", 1)[-1]
                with DB_LOCK:
                    record = _db().execute(
                        """SELECT p.path,v.mokuro_path,v.source_path FROM pages p
                        JOIN volumes v ON v.id=p.volume_id WHERE p.id=?""",
                        (page,),
                    ).fetchone()
                if not record or not record["mokuro_path"]:
                    return _json_response(self, {"blocks": []})
                return _json_response(
                    self,
                    mokuro_page(
                        Path(record["mokuro_path"]),
                        Path(record["path"]),
                        Path(record["source_path"]),
                    ),
                )
            if path.startswith("/api/review/"):
                series = unquote(path.rsplit("/", 1)[-1])
                with DB_LOCK:
                    rows = [
                        dict(item)
                        for item in _db().execute(
                            "SELECT * FROM mappings WHERE series=? AND manual=0 AND confidence<.80 ORDER BY created_at",
                            (series,),
                        )
                    ]
                return _json_response(self, rows)
            if path == "/api/backup":
                backup_dir = initialize_data_dirs() / "backups"
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                target = backup_dir / f"Bilingual-Manga-Reader-Backup-{stamp}.sqlite3"
                with DB_LOCK:
                    destination = sqlite3.connect(target)
                    try:
                        _db().backup(destination)
                    finally:
                        destination.close()
                return _serve_path(self, target, "application/vnd.sqlite3", target.name)
            if path.startswith("/media/"):
                page = path.rsplit("/", 1)[-1]
                with DB_LOCK:
                    record = _db().execute("SELECT path FROM pages WHERE id=?", (page,)).fetchone()
                return _serve_path(self, Path(record["path"])) if record else self.send_error(404)
            if path.startswith("/thumb/"):
                volume = path.rsplit("/", 1)[-1]
                with DB_LOCK:
                    record = _db().execute(
                        "SELECT thumbnail_path,cover_path FROM volumes WHERE id=?", (volume,)
                    ).fetchone()
                if not record:
                    return self.send_error(404)
                candidate = Path(record["thumbnail_path"]) if record["thumbnail_path"] else Path(record["cover_path"])
                return _serve_path(self, candidate)
            if path in ("/", "/index.html"):
                return _serve_path(
                    self,
                    ROOT / "static" / "index.html",
                    "text/html; charset=utf-8",
                    cache_control="no-store",
                )
            if path.startswith("/static/"):
                name = _safe_relative(path.removeprefix("/static/"))
                if len(name.parts) != 1:
                    return self.send_error(404)
                return _serve_path(self, ROOT / "static" / name, cache_control="no-store")
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            LOGGER.exception("GET %s failed", self.path)
            _json_response(self, {"error": str(exc)}, 400)

    def do_POST(self):
        if not self._require_trusted():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/source/pick":
                _read_json(self)
                selected = choose_manga_source()
                if selected is None:
                    return _json_response(self, {"cancelled": True})
                return _json_response(self, describe_manga_source(selected))
            if path == "/api/bulk/pick":
                selected = choose_bulk_folder()
                return _json_response(
                    self,
                    {"cancelled": selected is None, "path": str(selected) if selected else ""},
                )
            if path == "/api/bulk/scan/start":
                data = _read_json(self)
                return _json_response(self, start_bulk_scan(Path(str(data.get("path", "")))), 202)
            if path == "/api/bulk/import-one":
                data = _read_json(self)
                result = import_bulk_item(
                    str(data.get("scan_id", "")),
                    str(data.get("item_id", "")),
                    data.get("metadata", {}) if isinstance(data.get("metadata", {}), dict) else {},
                    friendly_names=bool(data.get("friendly_names", True)),
                    delete_original=bool(data.get("delete_original")),
                )
                return _json_response(self, result, 201)
            if path == "/api/upload/start":
                metadata = _read_json(self)
                upload_id = str(uuid.uuid4())
                folder = initialize_data_dirs() / "incoming" / upload_id
                folder.mkdir()
                safe_metadata = {
                    "language": metadata.get("language") if metadata.get("language") in ("jp", "en") else "auto",
                    "series": str(metadata.get("series", ""))[:300].strip() or None,
                    "title": str(metadata.get("title", ""))[:300].strip() or None,
                    "volume_label": str(metadata.get("volume_label", ""))[:100].strip() or None,
                    "chapter_label": str(metadata.get("chapter_label", ""))[:100].strip() or None,
                    "relink_volume_id": metadata.get("relink_volume_id"),
                    "convert": bool(metadata.get("convert")),
                    "content_type": metadata.get("content_type")
                    if metadata.get("content_type") in ("bilingual", "mokuro", "raw")
                    else None,
                    "friendly_names": bool(metadata.get("friendly_names", True)),
                    "created": time.time(),
                }
                (folder / ".upload.json").write_text(
                    json.dumps(safe_metadata, ensure_ascii=False), encoding="utf-8"
                )
                return _json_response(self, {"upload_id": upload_id}, 201)
            if path == "/api/upload/file":
                upload_id = parse_qs(parsed.query).get("upload_id", [""])[0]
                folder, _ = _upload_metadata(upload_id)
                relative = _safe_relative(self.headers.get("X-Relative-Path", ""))
                if relative.suffix.lower() not in IMAGES | ARCHIVES | PDFS | {".mokuro"}:
                    raise ValueError(f"Unsupported import file: {relative.name}")
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 2 * 1024**3:
                    raise ValueError("An uploaded file has an invalid size")
                current_total = sum(item.stat().st_size for item in folder.rglob("*") if item.is_file())
                if current_total + length > 6 * 1024**3:
                    raise ValueError("Upload exceeds the 6 GB per-volume safety limit")
                destination = (folder / relative).resolve()
                if folder.resolve() not in destination.parents:
                    raise ValueError("Unsafe uploaded filename")
                destination.parent.mkdir(parents=True, exist_ok=True)
                remaining = length
                with destination.open("wb") as output:
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("Upload ended before the declared size")
                        output.write(chunk)
                        remaining -= len(chunk)
                return _json_response(self, {"ok": True}, 201)
            if path == "/api/upload/finish":
                data = _read_json(self)
                upload_folder, _ = _upload_metadata(data["upload_id"])
                _ensure_catalog_for_source(upload_folder)
                with DB_LOCK:
                    result = _finish_upload(data["upload_id"])
                return _json_response(self, result, 201)
            if path == "/api/upload/cancel":
                data = _read_json(self)
                folder, _ = _upload_metadata(data["upload_id"])
                shutil.rmtree(folder)
                return _json_response(self, {"ok": True})
            if path == "/api/import":
                data = _read_json(self)
                source = Path(data["path"]).expanduser().resolve()
                _ensure_catalog_for_source(source)
                metadata = {
                    "series": str(data.get("series", ""))[:300].strip() or None,
                    "title": str(data.get("title", ""))[:300].strip() or None,
                    "volume_label": str(data.get("volume_label", ""))[:100].strip() or None,
                    "chapter_label": str(data.get("chapter_label", ""))[:100].strip() or None,
                    "content_type": data.get("content_type")
                    if data.get("content_type") in ("bilingual", "mokuro", "raw")
                    else None,
                }
                delete_original = bool(data.get("delete_original"))
                friendly_names = bool(data.get("friendly_names", True))
                if source.suffix.lower() in PDFS:
                    result = {
                        "pending": True,
                        "conversion_job": start_pdf_conversion(
                            source,
                            metadata,
                            delete_original=delete_original,
                            friendly_names=friendly_names,
                        ),
                    }
                else:
                    convert = bool(data.get("convert"))
                    with DB_LOCK:
                        result = import_volume(
                            source,
                            data.get("language", "auto"),
                            metadata["series"],
                            metadata["title"],
                            metadata["volume_label"],
                            metadata["chapter_label"],
                            "referenced",
                            content_type=metadata["content_type"],
                            friendly_names=friendly_names,
                            delete_original=delete_original and not convert,
                        )
                    if convert and result.get("language") == "jp" and not result.get("mokuro"):
                        result["conversion_job"] = start_volume_conversion(
                            result["id"], delete_original=delete_original
                        )
                return _json_response(self, result, 201)
            if path == "/api/library/remove":
                data = _read_json(self)
                result = remove_from_library(
                    volume_id=str(data.get("volume_id", "")).strip() or None,
                    series=str(data.get("series", "")).strip() or None,
                    delete_originals=bool(data.get("delete_originals")),
                )
                return _json_response(self, result)
            if path == "/api/conversion/start":
                data = _read_json(self)
                result = start_volume_conversion(
                    str(data.get("volume_id", "")), bool(data.get("delete_original"))
                )
                return _json_response(self, result, 202)
            if path == "/api/relink":
                data = _read_json(self)
                with DB_LOCK:
                    result = relink_volume(data["volume_id"], Path(data["path"]))
                return _json_response(self, result)
            if path == "/api/pairing/confirm":
                data = _read_json(self)
                with DB_LOCK:
                    result = confirm_pairing(
                        str(data.get("volume_id", "")), str(data.get("peer_id", ""))
                    )
                return _json_response(self, result)
            if path == "/api/progress":
                data = _read_json(self)
                with DB_LOCK:
                    valid = _db().execute(
                        "SELECT 1 FROM pages WHERE id=? AND volume_id=?",
                        (data.get("page_id"), data.get("volume_id")),
                    ).fetchone()
                    if not valid:
                        raise ValueError("Progress page does not belong to this volume")
                    _db().execute(
                        """INSERT INTO progress(volume_id,page_id,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
                        ON CONFLICT(volume_id) DO UPDATE SET page_id=excluded.page_id,updated_at=CURRENT_TIMESTAMP""",
                        (data["volume_id"], data["page_id"]),
                    )
                    _db().commit()
                return _json_response(self, {"ok": True})
            if path == "/api/favorite":
                data = _read_json(self)
                series = str(data.get("series", "")).strip()
                if not series:
                    raise ValueError("Series is required")
                with DB_LOCK:
                    if data.get("favorite"):
                        _db().execute("INSERT OR IGNORE INTO favorites(series) VALUES(?)", (series,))
                    else:
                        _db().execute("DELETE FROM favorites WHERE series=?", (series,))
                    _db().commit()
                return _json_response(self, {"ok": True})
            if path == "/api/settings":
                data = _read_json(self)
                allowed = {
                    "reader_fit",
                    "reader_zoom",
                    "reader_direction",
                    "reader_ocr_display",
                    "reader_click_navigation",
                }
                with DB_LOCK:
                    for key, value in data.items():
                        if key not in allowed:
                            continue
                        _db().execute(
                            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value)),
                        )
                    _db().commit()
                return _json_response(self, {"ok": True})
            if path == "/api/bookmark":
                data = _read_json(self)
                volume_id = str(data.get("volume_id", ""))
                page_id_value = str(data.get("page_id", ""))
                with DB_LOCK:
                    valid = _db().execute(
                        "SELECT 1 FROM pages WHERE id=? AND volume_id=?",
                        (page_id_value, volume_id),
                    ).fetchone()
                    if not valid:
                        raise ValueError("Bookmark page does not belong to this edition")
                    if data.get("bookmarked"):
                        _db().execute(
                            "INSERT OR IGNORE INTO bookmarks(volume_id,page_id) VALUES(?,?)",
                            (volume_id, page_id_value),
                        )
                    else:
                        _db().execute(
                            "DELETE FROM bookmarks WHERE volume_id=? AND page_id=?",
                            (volume_id, page_id_value),
                        )
                    count = _db().execute(
                        "SELECT COUNT(*) FROM bookmarks WHERE volume_id=?", (volume_id,)
                    ).fetchone()[0]
                    _db().commit()
                return _json_response(self, {"ok": True, "bookmark_count": count})
            if path == "/api/catalog/import":
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 256 * 1024**2:
                    raise ValueError("Bilingual Manga catalogue ZIP has an invalid size")
                root = initialize_data_dirs()
                incoming = root / "incoming" / f"catalog-{uuid.uuid4()}.zip"
                remaining = length
                try:
                    with incoming.open("wb") as output:
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("Catalogue upload ended before the declared size")
                            output.write(chunk)
                            remaining -= len(chunk)
                    result = install_catalog(incoming, root / CATALOG_FILENAME)
                finally:
                    incoming.unlink(missing_ok=True)
                return _json_response(self, result, 201)
            if path == "/api/map":
                data = _read_json(self)
                jp_pages = list(dict.fromkeys(data.get("jp_pages", [])))[:2]
                en_pages = list(dict.fromkeys(data.get("en_pages", [])))[:2]
                if not jp_pages and not en_pages:
                    raise ValueError("Select at least one Japanese or English page")
                series = str(data.get("series", "")).strip()
                with DB_LOCK:
                    for side, ids in (("jp", jp_pages), ("en", en_pages)):
                        for page_id_value in ids:
                            record = _db().execute(
                                """SELECT v.series,v.language FROM pages p JOIN volumes v ON v.id=p.volume_id
                                WHERE p.id=?""",
                                (page_id_value,),
                            ).fetchone()
                            if not record or record["series"] != series or record["language"] != side:
                                raise ValueError("Selected mapping pages do not belong to this series/language")
                    delete_mappings_for_pages(_db(), jp_pages + en_pages)
                    _insert_group_mapping(series, jp_pages, en_pages, "manual", 1.0, True)
                    inferred = _infer_manual_neighbors(jp_pages[0], en_pages[0], series) if len(jp_pages) == len(en_pages) == 1 else 0
                    _db().commit()
                return _json_response(self, {"ok": True, "inferred_neighbors": inferred})
            if path == "/api/mappings/import":
                data = _read_json(self)
                with DB_LOCK:
                    result = import_archived_mappings(str(data["series"]), data["data"])
                return _json_response(self, result, 201)
            if path == "/api/scan":
                imported, errors = [], []
                root = initialize_data_dirs() / "library"
                candidates = [item for item in root.iterdir() if item.name != "incoming"]
                with DB_LOCK:
                    known = {row[0] for row in _db().execute("SELECT source_path FROM volumes")}
                    known_managed_roots = {
                        managed.resolve()
                        for source_path in known
                        if (managed := _managed_root(source_path)) is not None
                    }
                    for candidate in candidates:
                        if str(candidate.resolve()) in known:
                            continue
                        if candidate.resolve() in known_managed_roots:
                            continue
                        if candidate.is_dir() and not images(candidate):
                            continue
                        if not candidate.is_dir() and candidate.suffix.lower() not in ARCHIVES:
                            continue
                        try:
                            imported.append(import_volume(candidate, "jp", source_kind="managed"))
                        except Exception as exc:
                            errors.append({"path": str(candidate), "error": str(exc)})
                return _json_response(self, {"imported": imported, "errors": errors})
            if path == "/api/open-folder":
                data = _read_json(self)
                root = initialize_data_dirs()
                targets = {"data": root, "library": root / "library", "backups": root / "backups"}
                target = targets.get(data.get("target"))
                if not target:
                    raise ValueError("Folder target is invalid")
                if platform.system() == "Darwin":
                    subprocess.Popen(["open", str(target)])
                elif platform.system() == "Windows":
                    os.startfile(target)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", str(target)])
                return _json_response(self, {"ok": True})
            if path == "/api/shutdown":
                _json_response(self, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            _json_response(self, {"error": "Endpoint was not found"}, 404)
        except Exception as exc:
            with DB_LOCK:
                if DB is not None:
                    _db().rollback()
            LOGGER.exception("POST %s failed", self.path)
            _json_response(self, {"error": str(exc)}, 400)


def _create_server(port: int) -> Server:
    """Prefer the stable port across a rapid restart before using a fallback."""
    deadline = time.monotonic() + 3.0
    while True:
        try:
            return Server(("127.0.0.1", port), Handler)
        except OSError:
            if port == 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    return Server(("127.0.0.1", 0), Handler)


def run(
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
    instance_file: Path | None = None,
) -> None:
    global DB
    root = initialize_data_dirs()
    stale_removals = list((root / "incoming").glob("removing-*"))
    if stale_removals:
        threading.Thread(
            target=lambda: [shutil.rmtree(path, ignore_errors=True) for path in stale_removals],
            name="stale-removal-cleanup",
            daemon=True,
        ).start()
    DB = connect(root / "bilingual-manga.sqlite3")
    for legacy in previous_data_dirs():
        _migrate_legacy_paths(root, legacy)
    organize_managed_library_names()
    if not load_catalog(root / CATALOG_FILENAME):
        def refresh_catalog() -> None:
            try:
                result = ensure_catalog(root / CATALOG_FILENAME)
                if result.get("downloaded"):
                    LOGGER.info("Cached %s Bilingual Manga archive identifiers", result["archives"])
            except Exception as exc:
                LOGGER.warning("Bilingual Manga recognition metadata is unavailable: %s", exc)

        threading.Thread(
            target=refresh_catalog,
            name="bilingual-catalog-metadata",
            daemon=True,
        ).start()
    server = _create_server(port)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}"
    state = instance_file or (Path(os.getenv("BMO_PORT_FILE")) if os.getenv("BMO_PORT_FILE") else None)
    if state:
        try:
            state.parent.mkdir(parents=True, exist_ok=True)
            if state.suffix == ".json":
                state.write_text(json.dumps({"port": actual_port, "pid": os.getpid(), "url": url}), encoding="utf-8")
            else:
                state.write_text(str(actual_port), encoding="utf-8")
        except OSError:
            LOGGER.exception("Could not write server state")
    LOGGER.info("Started version %s on %s", VERSION, url)
    if open_browser:
        threading.Timer(0.35, lambda: open_reader(url)).start()

    def stop_server(*_args):
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, stop_server)
        signal.signal(signal.SIGINT, stop_server)
    except ValueError:
        pass
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        if state and state.suffix == ".json":
            try:
                current = json.loads(state.read_text(encoding="utf-8"))
                if current.get("pid") == os.getpid():
                    state.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                pass
        with DB_LOCK:
            DB.close()
        LOGGER.info("Stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args()
    run(arguments.port, not arguments.no_browser)
