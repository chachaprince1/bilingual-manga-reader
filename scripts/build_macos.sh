#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$PROJECT_ROOT/.pyinstaller}"
mkdir -p "$PYINSTALLER_CONFIG_DIR" "$PROJECT_ROOT/build/macos" "$PROJECT_ROOT/dist"
python3 -m PyInstaller --noconfirm --clean --workpath "$PROJECT_ROOT/build/macos" --distpath "$PROJECT_ROOT/dist" --hidden-import zlib --hidden-import PIL.Image --hidden-import PIL.ImageOps --collect-data certifi --name 'Bilingual Manga Reader and Mokuro Converter' --add-data 'static:static' app/launcher.py
rm -rf 'dist/Bilingual Manga Reader and Mokuro Converter.app'
mkdir -p 'dist/Bilingual Manga Reader and Mokuro Converter.app/Contents/MacOS' 'dist/Bilingual Manga Reader and Mokuro Converter.app/Contents/Resources'
cp -R 'dist/Bilingual Manga Reader and Mokuro Converter' 'dist/Bilingual Manga Reader and Mokuro Converter.app/Contents/Resources/runtime'
case "$(uname -m)" in
  arm64) UV_TARGET="aarch64-apple-darwin" ;;
  x86_64) UV_TARGET="x86_64-apple-darwin" ;;
  *) echo "Unsupported macOS architecture: $(uname -m)" >&2; exit 1 ;;
esac
UV_ARCHIVE="$PROJECT_ROOT/build/macos/uv-$UV_TARGET.tar.gz"
UV_EXTRACT="$PROJECT_ROOT/build/macos/uv-extract"
if [ ! -f "$UV_ARCHIVE" ]; then
  curl -fL --retry 3 -o "$UV_ARCHIVE" "https://github.com/astral-sh/uv/releases/latest/download/uv-$UV_TARGET.tar.gz"
fi
rm -rf "$UV_EXTRACT"
mkdir -p "$UV_EXTRACT" 'dist/Bilingual Manga Reader and Mokuro Converter.app/Contents/Resources/runtime/tools'
tar -xzf "$UV_ARCHIVE" -C "$UV_EXTRACT"
cp "$(find "$UV_EXTRACT" -type f -name uv -print -quit)" 'dist/Bilingual Manga Reader and Mokuro Converter.app/Contents/Resources/runtime/tools/uv'
chmod +x 'dist/Bilingual Manga Reader and Mokuro Converter.app/Contents/Resources/runtime/tools/uv'
curl -fsSL --retry 3 -o 'dist/Bilingual Manga Reader and Mokuro Converter.app/Contents/Resources/UV-LICENSE-APACHE.txt' https://raw.githubusercontent.com/astral-sh/uv/main/LICENSE-APACHE
curl -fsSL --retry 3 -o 'dist/Bilingual Manga Reader and Mokuro Converter.app/Contents/Resources/UV-LICENSE-MIT.txt' https://raw.githubusercontent.com/astral-sh/uv/main/LICENSE-MIT
cp packaging/Info.plist 'dist/Bilingual Manga Reader and Mokuro Converter.app/Contents/Info.plist'
cp packaging/macos-launcher.sh 'dist/Bilingual Manga Reader and Mokuro Converter.app/Contents/MacOS/Bilingual Manga Reader and Mokuro Converter'
chmod +x 'dist/Bilingual Manga Reader and Mokuro Converter.app/Contents/MacOS/Bilingual Manga Reader and Mokuro Converter'
mkdir -p outputs
rm -f outputs/BilingualMangaOffline-macOS.zip
ditto -c -k --norsrc --keepParent 'dist/Bilingual Manga Reader and Mokuro Converter.app' outputs/BilingualMangaOffline-macOS.zip
shasum -a 256 outputs/BilingualMangaOffline-macOS.zip > outputs/SHA256SUMS.txt
