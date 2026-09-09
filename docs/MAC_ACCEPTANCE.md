# Release acceptance matrix

Validated on the native Apple Silicon macOS host on 2026-09-08. Synthetic pages are generated during tests; no manga is included in either release archive.

| Area | Result | Evidence |
| --- | --- | --- |
| macOS packaging | PASS | `scripts/build_macos.sh` produced the standalone arm64 `Bilingual Manga Reader and Mokuro Converter.app` with reader runtime, Pillow, Certifi CA data, and bundled `uv` bootstrap |
| Clean extracted launch | PASS | the executable extracted from the final ZIP started with isolated app data, reported version 1.4.2, served an empty library on loopback, and exposed no remote frontend dependency |
| Stable launch / stale-tab recovery | PASS | an immediate stop/replace/reopen reproduced the former restart race; 1.4.2 waited for and reclaimed `127.0.0.1:48765` without competing with AnkiConnect on `8765`. Static HTML/JS/CSS use `no-store` plus versioned asset URLs |
| Single instance | PASS | opening the installed app again retained the same PID and port and opened the existing healthy library |
| Local server security | PASS | the packaged app bound only to `127.0.0.1`; foreign Host, Origin, and cross-site requests returned 403; framing, unsafe base/object content, camera, microphone, and geolocation are disabled without blocking Yomitan |
| Installed upgrade | PASS | the current app retained 220/220 available editions across Yotsuba&! and Hayate the Combat Butler. Three stale UUID-labelled duplicate records from the retired scan path were removed without deleting shared manga files |
| Library experience | PASS | welcome message, search, six exact discovery filters, sorting, Continue Reading, favorites, bookmarks, removal, local-storage explanation, upper-right Bilingual marker, and fully spelled-out Bilingual/Mokuro/Raw edition labels were exercised in the installed UI |
| Reader | PASS | selectable Japanese Mokuro DOM text, bookmark add/remove, hidden meaningless chapter selector, RTL default, LTR toggle, correct bottom Previous/Next sides, click navigation, OCR controls, zoom/fit/fullscreen controls, and return navigation were exercised |
| Yomitan compatibility | PASS | the same positioned DOM OCR path was exercised in the user's ordinary Chrome profile with text selection and Yomitan; the final reader keeps that DOM-based rendering and its geometry regression tests |
| Import formats | PASS | folders, browser folder uploads, PDF, CBZ, ZIP, and TAR; safe extraction, Unicode paths, automatic metadata/type/language inference, user-friendly managed names, and optional verified-original Trash behavior |
| Bilingual recognition | PASS | the final extracted 1.4.2 package securely fetched and validated the linked upstream `json.zip`, cached 20,851 archive identifiers, and left no temporary download behind. Manga pages are never fetched or uploaded |
| Automatic pairing | PASS | exact and high-confidence opposite-language editions pair automatically; ambiguous candidates require confirmation; different volume labels cannot pair accidentally |
| Bulk reliability | PASS | failure isolation, duplicate signatures, already-added handling, incomplete/sparse torrent diagnostics, and the legacy managed-root duplicate regression are automated. The real 219-archive Yotsuba batch previously scanned as 105 Japanese + 114 English with zero unsupported items |
| Embedded Mokuro conversion | PASS | on-demand private conversion engine, PDFs/images to Mokuro, automatic hardware acceleration, output verification, stable page IDs, safe source handling, and serialized conversion jobs are tested. A 10-page real conversion benchmark improved from 28.16 seconds CPU to 15.89 seconds with Apple acceleration |
| Security/privacy | PASS | loopback-only host/origin checks, no account/telemetry/LAN exposure, safe archive limits, verified TLS for metadata, no manga in release archives, and no bundled multi-gigabyte OCR cache |
| Automated suite | PASS | 59 tests pass with `ResourceWarning` promoted to an error; Python, JavaScript, shell, plist, ZIP, Mach-O, and Windows PE checks also pass |
| Windows portable structure | PASS | zero-install ZIP contains x64 GUI launchers, official x64 Python 3.12 runtime, Pillow, SQLite, Certifi CA data, and x64 `uv`; all Python sources compile and the archive contains no manga |
| Native Windows launch | NOT EXECUTED | this Apple Silicon Mac cannot execute a Windows PE application; clean Windows launch, Windows Chrome/Yomitan, and Recycle Bin integration remain the only host-specific acceptance items |
| macOS Gatekeeper | NOT EXECUTED | the shareable app is unsigned; a fresh recipient may need right-click Open or Open Anyway once |

Final SHA-256 checksums:

```text
cdcbc5fea879751de3ae9397b6eb376606827276945dc42dcfe209e99ac4ec42  BilingualMangaOffline-macOS.zip
9e09f1f41fed4403d3ffd8b6a4e297712c47e16e7732cf5648ad74588c83c715  BilingualMangaOffline-Windows.zip
```
