# Development and release

## Local development

Run `python3 -m app.server --no-browser`. The server binds only to loopback on port 48765, falling back to a free port if necessary. Port 8765 remains reserved for AnkiConnect.

Run the strict suite before packaging:

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

It covers schema migration, Unicode import, safe ZIP/TAR handling, bulk failure isolation and duplicate detection, native picker commands, removal safety, automatic/confirmed pairing, structural and visual alignment, spreads, Mokuro DOM data, progress, bookmarks, relinking, conversion jobs, HTTP security, offline frontend assets, and a 300-volume query.

## macOS release

Run `scripts/build_macos.sh`. It creates `outputs/BilingualMangaOffline-macOS.zip`. The archive contains a standalone Apple Silicon `.app`, bundled Python reader runtime, and bundled `uv` converter bootstrap. Smoke-test the executable extracted from the ZIP with a temporary `BMO_DATA_DIR`, then test the Finder launcher and normal Chrome profile.

## Windows release

Run `scripts/build_windows_portable.sh` on the Apple Silicon build host. It downloads the official Windows x64 Python 3.12 embeddable runtime, a matching Pillow wheel, and the Windows `uv` bootstrapper. A tiny GUI-subsystem x64 launcher is cross-compiled with a checksum-verified Zig toolchain. The result is `outputs/BilingualMangaOffline-Windows.zip`, containing `Bilingual Manga.exe` and all required runtime files.

The Windows archive can be structurally verified on macOS (ZIP integrity, PE x64/GUI headers, required DLL/PYD/runtime files, Python source compilation), but clean-machine launch acceptance must run on native Windows. The GitHub Actions matrix is the native-build route when the project is placed in a repository.

## Conversion engine

The release packages do not bundle PyTorch or OCR models. On first conversion, bundled `uv` creates a private Python 3.12 environment and installs the exact Mokuro/PyTorch/PDF support versions in `app/converter.py`. Published model weights are cached in user data. Manga pages remain local. Subsequent conversions work from the cache and hardware acceleration is selected automatically when supported.

## Content and licensing

No manga is bundled. The Bilingual Manga archive currently has no explicit redistribution license, so release packages link to the [upstream archive](https://github.com/B-M-dev/Bilingual-Manga-archive) rather than redistributing its source dataset. Fresh installs cache recognition metadata from that linked repository on first use; the validated reduced catalogue then stays in local app data. `scripts/import_bilingual_mappings.py` can also normalize a user-supplied licensed export. Mokuro is installed on demand rather than redistributed. Python, Pillow, and `uv` license notices accompany the Windows runtime; `uv` notices accompany the macOS bundle.
