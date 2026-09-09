# Bilingual Manga Reader and Mokuro Converter

A private, offline manga library for **any Mokuro manga**, raw Japanese manga, and matching Japanese/English editions. It opens in your ordinary Chrome profile so Yomitan, your dictionaries, audio, and Anki integration work normally.

## Windows

1. Extract `BilingualMangaOffline-Windows.zip`.
2. Double-click **Bilingual Manga.exe**.
3. Add manga. Chrome opens automatically.
4. Read.

## macOS (Apple Silicon)

1. Extract `BilingualMangaOffline-macOS.zip`.
2. On an M-series Mac, open **Bilingual Manga Reader and Mokuro Converter.app**.
3. If macOS blocks the unsigned app, right-click it and choose **Open**, or use **Open Anyway** once.
4. Add manga. Chrome opens automatically.
5. Read.

Install and configure Yomitan in Chrome normally for Japanese dictionary lookup.

The reader uses `127.0.0.1:48765` by default. It deliberately avoids port
`8765`, which belongs to AnkiConnect, so the manga reader, Yomitan, ImmersionKit,
and Anki can run at the same time regardless of launch order.

## What it reads

- Existing Mokuro folders with images and a `.mokuro` file.
- Raw image folders, PDF, CBZ, ZIP, and TAR manga.
- Japanese and English sets from the [Bilingual Manga archive](https://github.com/B-M-dev/Bilingual-Manga-archive).

**Scan for manga** safely bulk-discovers separate editions in a parent folder. Type, language, series, title, volume, and chapter are inferred automatically; uncertain Japanese/English matches ask for one confirmation. Recognized editions are page-aligned, including front-matter offsets and differing spreads. Press `L` to switch to the confirmed corresponding page and back.

On first use, the app caches about 14 MB of recognition metadata directly from the linked Bilingual Manga archive so CID-named downloads work on a fresh installation. This metadata contains naming/matching information, not manga pages; ordinary Mokuro and raw imports remain available without it.

Raw Japanese manga can be converted locally with Mokuro. The first conversion installs a private pinned OCR engine and model cache; the app includes its own bootstrap runtime, so Python or command-line tools are not required. Hardware acceleration is used automatically when supported. Manga pages are never sent to an OCR service.

The library includes search, Bilingual/Japanese/Mokuro-Ready/Favorites/Bookmarked discovery, Continue Reading, favorites, page bookmarks, progress, removal, relinking, backup, and automatic stale-tab reconnection. Reader controls include right-to-left or left-to-right navigation, click-to-turn, previous/next buttons, fit modes, zoom, fullscreen, and OCR hover/show/hide. OCR defaults to hover, reading defaults to right-to-left, and click navigation defaults on.

Shortcuts: `L` language, `A` alignment editor, `B` bookmark, `F` favorite, arrow keys navigate, `+`/`−` zoom, and `0` resets zoom.

## Privacy and storage

The server binds only to `127.0.0.1`, rejects cross-site/foreign-host requests, and sends browser hardening headers without blocking Yomitan. There is no account, telemetry, LAN exposure, cloud database, or manga upload. Reading works without Internet after import; only first-use Bilingual recognition metadata and converter packages/models require downloads.

- macOS: `~/Library/Application Support/Bilingual Manga Reader and Mokuro Converter`
- Windows: `%APPDATA%\Bilingual Manga Reader and Mokuro Converter`

Folders may stay referenced in place. Archives and conversions use a managed library. Optional checkboxes can move originals to Trash/Recycle Bin only after the managed copy or conversion output is verified.

## Development

Run `python3 -m app.server --no-browser` for local development and `python3 -W error::ResourceWarning -m unittest discover -s tests -v` for the test suite.

- `scripts/build_macos.sh` produces the macOS ZIP.
- `scripts/build_windows_portable.sh` produces the zero-runtime Windows x64 ZIP.

See [development and release instructions](docs/DEVELOPMENT.md), [architecture](docs/ARCHITECTURE.md), and the [macOS acceptance matrix](docs/MAC_ACCEPTANCE.md).
