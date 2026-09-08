"""Normalize user-supplied archived Bilingual Manga mapping exports.

No upstream data is bundled. The archived reader stores two inverse ``img_data``
dictionaries whose keys and values look like ``chapter_page.ext``. Generic
grouped exports are accepted as well.
"""

from __future__ import annotations

import json
from pathlib import Path


def _list(value) -> list[str]:
    if value in (None, ""):
        return []
    values = value if isinstance(value, list) else [value]
    return [str(item).split(".")[0] for item in values if item not in (None, "")]


def normalize_record(record: dict) -> list[dict]:
    title = (
        record.get("title")
        or record.get("name")
        or record.get("slug")
        or record.get("manga_title")
        or ""
    )
    normalized: list[dict] = []
    img_data = record.get("img_data") or {}
    jp_index = img_data.get("jp") if isinstance(img_data, dict) else None
    if isinstance(jp_index, dict):
        for jp_ref, en_ref in jp_index.items():
            normalized.append(
                {
                    "jp_pages": _list(jp_ref),
                    "en_pages": _list(en_ref),
                    "source": "bilingual-manga",
                    "confidence": 1.0,
                    "title": title,
                }
            )
        return normalized

    pairs = record.get("mappings") or record.get("mapping") or record.get("page_mapping") or []
    if isinstance(pairs, dict):
        pairs = [{"jp_pages": key, "en_pages": value} for key, value in pairs.items()]
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        jp = _list(pair.get("jp_pages", pair.get("jp", pair.get("japanese"))))
        en = _list(pair.get("en_pages", pair.get("en", pair.get("english"))))
        if jp or en:
            normalized.append(
                {
                    "jp_pages": jp,
                    "en_pages": en,
                    "source": "bilingual-manga",
                    "confidence": float(pair.get("confidence", 1.0)),
                    "title": title,
                }
            )
    return normalized


def normalize_data(data) -> list[dict]:
    if isinstance(data, dict) and any(key in data for key in ("img_data", "mappings", "mapping")):
        records = [data]
    elif isinstance(data, dict):
        records = data.values()
    elif isinstance(data, list):
        records = data
    else:
        raise ValueError("Mapping export must contain a JSON object or list")
    return [
        mapping
        for record in records
        if isinstance(record, dict)
        for mapping in normalize_record(record)
    ]


def normalize_file(path: Path) -> list[dict]:
    return normalize_data(json.loads(path.read_text(encoding="utf-8")))
