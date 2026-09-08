"""Managed, local Mokuro conversion engine.

The reader UI owns this workflow.  A separate Python environment is installed
under Application Support on first use because PyTorch and the OCR models are
far too large for the small reader bundle.  Manga pages are never sent to a
service; only published Python packages and model weights are downloaded.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

MOKURO_VERSION = "0.2.5"
INSTALL_LOCK = threading.Lock()
Progress = Callable[[str, str], None]


def _native_command(command: list[str]) -> list[str]:
    """Keep universal Python helpers on the app's native Apple Silicon slice."""
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return ["/usr/bin/arch", "-arm64", *command]
    return command


def _python_version(executable: Path) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            _native_command([str(executable), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"]),
            capture_output=True,
            text=True,
            timeout=8,
            check=True,
        )
        major, minor = (int(value) for value in result.stdout.strip().split(".", 1))
        return major, minor
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def find_host_python() -> Path | None:
    """Find a real Python capable of creating the managed OCR environment."""
    candidates = [
        Path("/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"),
        Path("/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"),
        Path("/Library/Frameworks/Python.framework/Versions/3.10/bin/python3"),
    ]
    for command in ("python3", "python"):
        discovered = shutil.which(command)
        if discovered:
            candidates.append(Path(discovered))
    if not getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable))
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.expanduser().resolve()
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        # The Windows embeddable runtime deliberately cannot create venvs.
        # The portable build ships uv for that job instead.
        if os.name == "nt" and any(candidate.parent.glob("python*._pth")):
            continue
        version = _python_version(candidate)
        if version and (3, 10) <= version < (3, 14):
            return candidate
    return None


def bundled_uv() -> Path | None:
    """Return the bundled uv bootstrapper used by the Windows portable build."""
    names = ("uv.exe",) if os.name == "nt" else ("uv",)
    locations = [ROOT for ROOT in (Path(sys.executable).resolve().parent, Path(__file__).resolve().parent.parent)]
    for location in locations:
        for name in names:
            for candidate in (location / "tools" / name, location / name):
                if candidate.is_file():
                    return candidate
    return None


def bootstrap_available() -> bool:
    return bool(find_host_python() or bundled_uv())


def engine_python(root: Path) -> Path:
    return root / "converter" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def engine_ready(root: Path, *, verify: bool = False) -> bool:
    executable = engine_python(root)
    if not executable.is_file():
        return False
    marker = root / "converter" / "engine.json"
    try:
        if json.loads(marker.read_text(encoding="utf-8")).get("mokuro") == MOKURO_VERSION:
            return True
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass
    if not verify:
        return False
    try:
        result = subprocess.run(
            _native_command([str(executable), "-c", "import fitz,mokuro,torch; print(mokuro.__version__)"]),
            capture_output=True,
            text=True,
            timeout=90,
            check=True,
        )
        return result.stdout.strip().splitlines()[-1] == MOKURO_VERSION
    except (OSError, subprocess.SubprocessError, IndexError):
        return False


def _run_stream(
    command: list[str], environment: dict[str, str], progress: Progress, phase: str
) -> None:
    process = subprocess.Popen(
        _native_command(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        env=environment,
    )
    recent: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        clean = line.strip().replace("\r", "")
        if clean:
            recent.append(clean)
            recent = recent[-12:]
            progress(phase, clean[-300:])
    code = process.wait()
    if code:
        detail = " · ".join(recent[-3:]) or f"process exited with code {code}"
        raise RuntimeError(detail)


def converter_environment(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    cache = root / "converter" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "HF_HOME": str(cache / "huggingface"),
            "XDG_CACHE_HOME": str(cache),
            "PYTHONUNBUFFERED": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    return environment


def ensure_engine(root: Path, progress: Progress) -> Path:
    """Install the pinned engine once, then return its Python executable."""
    with INSTALL_LOCK:
        if engine_ready(root):
            return engine_python(root)
        if engine_ready(root, verify=True):
            host = find_host_python()
            (root / "converter" / "engine.json").write_text(
                json.dumps({"mokuro": MOKURO_VERSION, "python": str(host or "existing")}, indent=2),
                encoding="utf-8",
            )
            return engine_python(root)
        host = find_host_python()
        uv = bundled_uv()
        if not host and not uv:
            raise RuntimeError(
                "The Mokuro conversion bootstrap is unavailable. Re-download the complete app package and try again."
            )
        converter_root = root / "converter"
        venv = converter_root / "venv"
        converter_root.mkdir(parents=True, exist_ok=True)
        progress("installing", "Preparing the private conversion engine (first use only)…")
        if venv.exists():
            shutil.rmtree(venv, ignore_errors=True)
        if host:
            subprocess.run(_native_command([str(host), "-m", "venv", str(venv)]), check=True)
        else:
            progress("installing", "Downloading the private Python runtime (first use only)…")
            assert uv is not None
            subprocess.run(
                [
                    str(uv),
                    "venv",
                    "--python",
                    "3.12",
                    "--python-preference",
                    "only-managed",
                    "--seed",
                    str(venv),
                ],
                check=True,
                env=converter_environment(root),
            )
        executable = engine_python(root)
        environment = converter_environment(root)
        progress("installing", "Downloading Mokuro, PyTorch, and PDF support…")
        _run_stream(
            [
                str(executable),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                f"mokuro=={MOKURO_VERSION}",
                "manga-ocr==0.1.16",
                "torch==2.14.0",
                "torchvision==0.29.0",
                "transformers==5.16.1",
                "opencv-python==5.0.0.93",
                "PyMuPDF==1.28.2",
            ],
            environment,
            progress,
            "installing",
        )
        if not engine_ready(root, verify=True):
            raise RuntimeError("The Mokuro conversion engine did not pass its installation check")
        (converter_root / "engine.json").write_text(
            json.dumps({"mokuro": MOKURO_VERSION, "python": str(host or "uv-managed-3.12")}, indent=2),
            encoding="utf-8",
        )
        return executable


def render_pdf(executable: Path, source: Path, pages: Path, root: Path, progress: Progress) -> None:
    pages.mkdir(parents=True, exist_ok=True)
    progress("rendering", "Turning the PDF into manga page images…")
    script = """
import fitz, pathlib, sys
source, output = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
document = fitz.open(source)
if not document.page_count or document.page_count > 5000:
    raise ValueError(f'PDF has an unsupported page count: {document.page_count}')
for index, page in enumerate(document):
    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    pixmap.save(output / f'{index + 1:05d}.png')
    print(f'Rendered page {index + 1} of {document.page_count}', flush=True)
""".strip()
    _run_stream(
        [str(executable), "-c", script, str(source), str(pages)],
        converter_environment(root),
        progress,
        "rendering",
    )


def run_mokuro(executable: Path, pages: Path, root: Path, progress: Progress) -> Path:
    progress(
        "converting",
        "Loading local OCR models. Hardware acceleration is used automatically when supported; the first conversion downloads model weights once…",
    )
    _run_stream(
        [
            str(executable),
            "-m",
            "mokuro",
            str(pages),
            "--disable_confirmation",
            "--legacy_html=False",
        ],
        converter_environment(root),
        progress,
        "converting",
    )
    output = pages.parent / f"{pages.name}.mokuro"
    if not output.is_file():
        raise RuntimeError("Mokuro finished without creating a .mokuro file")
    return output
