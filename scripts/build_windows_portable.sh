#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON_VERSION="3.12.10"
PILLOW_VERSION="11.3.0"
CERTIFI_VERSION="2025.8.3"
ZIG_VERSION="0.15.1"
ZIG_SHA256="c4bd624d901c1268f2deb9d8eb2d86a2f8b97bafa3f118025344242da2c54d7b"
BUILD_ROOT="$PROJECT_ROOT/build/windows-portable"
CACHE_ROOT="$BUILD_ROOT/cache"
STAGE_ROOT="$BUILD_ROOT/Bilingual Manga"
PYTHON_ARCHIVE="$CACHE_ROOT/python-$PYTHON_VERSION-embed-amd64.zip"
UV_ARCHIVE="$CACHE_ROOT/uv-x86_64-pc-windows-msvc.zip"
ZIG_ARCHIVE="$CACHE_ROOT/zig-aarch64-macos-$ZIG_VERSION.tar.xz"

mkdir -p "$CACHE_ROOT"
rm -rf "$STAGE_ROOT"
mkdir -p "$STAGE_ROOT/Lib/site-packages" "$STAGE_ROOT/tools"

if [ ! -f "$PYTHON_ARCHIVE" ]; then
  curl -fL --retry 3 -o "$PYTHON_ARCHIVE" "https://www.python.org/ftp/python/$PYTHON_VERSION/python-$PYTHON_VERSION-embed-amd64.zip"
fi
unzip -q "$PYTHON_ARCHIVE" -d "$STAGE_ROOT"
mv "$STAGE_ROOT/LICENSE.txt" "$STAGE_ROOT/PYTHON-LICENSE.txt"

PILLOW_WHEEL="$(find "$CACHE_ROOT" -maxdepth 1 -name "pillow-$PILLOW_VERSION-cp312-cp312-win_amd64.whl" -print -quit)"
if [ -z "$PILLOW_WHEEL" ]; then
  python3 -m pip download --disable-pip-version-check --no-deps --only-binary=:all: --platform win_amd64 --python-version 312 --implementation cp --abi cp312 "Pillow==$PILLOW_VERSION" -d "$CACHE_ROOT"
  PILLOW_WHEEL="$(find "$CACHE_ROOT" -maxdepth 1 -name "pillow-$PILLOW_VERSION-cp312-cp312-win_amd64.whl" -print -quit)"
fi
unzip -q "$PILLOW_WHEEL" -d "$STAGE_ROOT/Lib/site-packages"

CERTIFI_WHEEL="$(find "$CACHE_ROOT" -maxdepth 1 -name "certifi-$CERTIFI_VERSION-py3-none-any.whl" -print -quit)"
if [ -z "$CERTIFI_WHEEL" ]; then
  python3 -m pip download --disable-pip-version-check --no-deps --only-binary=:all: "certifi==$CERTIFI_VERSION" -d "$CACHE_ROOT"
  CERTIFI_WHEEL="$(find "$CACHE_ROOT" -maxdepth 1 -name "certifi-$CERTIFI_VERSION-py3-none-any.whl" -print -quit)"
fi
unzip -q "$CERTIFI_WHEEL" -d "$STAGE_ROOT/Lib/site-packages"

if [ ! -f "$UV_ARCHIVE" ]; then
  curl -fL --retry 3 -o "$UV_ARCHIVE" "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
fi
unzip -joq "$UV_ARCHIVE" uv.exe -d "$STAGE_ROOT/tools"
curl -fsSL --retry 3 -o "$STAGE_ROOT/UV-LICENSE-APACHE.txt" https://raw.githubusercontent.com/astral-sh/uv/main/LICENSE-APACHE
curl -fsSL --retry 3 -o "$STAGE_ROOT/UV-LICENSE-MIT.txt" https://raw.githubusercontent.com/astral-sh/uv/main/LICENSE-MIT

cp -R app static "$STAGE_ROOT/"
find "$STAGE_ROOT/app" -type d -name __pycache__ -prune -exec rm -rf {} +
cp LICENSE "$STAGE_ROOT/LICENSE.txt"
cp packaging/windows/README.txt "$STAGE_ROOT/README.txt"

PTH_FILE="$STAGE_ROOT/python312._pth"
printf 'python312.zip\n.\nLib/site-packages\nimport site\n' > "$PTH_FILE"

if [ ! -f "$ZIG_ARCHIVE" ]; then
  curl -fL --retry 3 -o "$ZIG_ARCHIVE" "https://ziglang.org/download/$ZIG_VERSION/zig-aarch64-macos-$ZIG_VERSION.tar.xz"
fi
printf '%s  %s\n' "$ZIG_SHA256" "$ZIG_ARCHIVE" | shasum -a 256 -c -
if [ ! -x "$BUILD_ROOT/zig/zig" ]; then
  rm -rf "$BUILD_ROOT/zig"
  mkdir -p "$BUILD_ROOT/zig"
  tar -xJf "$ZIG_ARCHIVE" --strip-components=1 -C "$BUILD_ROOT/zig"
fi
"$BUILD_ROOT/zig/zig" cc -target x86_64-windows-gnu -municode -O2 -Wl,--subsystem,windows \
  packaging/windows/launcher.c -o "$STAGE_ROOT/Bilingual Manga.exe"
cp "$STAGE_ROOT/Bilingual Manga.exe" "$STAGE_ROOT/Bilingual Manga Reader and Mokuro Converter.exe"

mkdir -p outputs
rm -f outputs/BilingualMangaOffline-Windows.zip
(cd "$BUILD_ROOT" && zip -qry "$PROJECT_ROOT/outputs/BilingualMangaOffline-Windows.zip" "Bilingual Manga")
