#!/usr/bin/env python3
"""Download OS-specific ffmpeg/ffprobe and build a one-file MusicTagger binary."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor" / "ffmpeg"
DIST = ROOT / "dist"

BTBN_LATEST = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
# Portable macOS builds (system frameworks only — not Homebrew dylibs).
FFMPEG_STATIC_TAG = "b6.1.1"
FFMPEG_STATIC_BASE = (
    f"https://github.com/eugeneware/ffmpeg-static/releases/download/{FFMPEG_STATIC_TAG}"
)


def tool_name(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def make_executable(path: Path) -> None:
    if sys.platform == "win32":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def download(url: str, dest: Path) -> None:
    print(f"Downloading {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "MusicTagger-build/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out)


def find_extracted_binary(root: Path, name: str) -> Path:
    target = tool_name(name)
    matches = sorted(
        p for p in root.rglob(target) if p.is_file() and p.name.lower() == target.lower()
    )
    if not matches:
        raise FileNotFoundError(f"Could not find {target} after extract in {root}")
    # Prefer .../bin/ffmpeg when present
    for path in matches:
        if path.parent.name.lower() == "bin":
            return path
    return matches[0]


def extract_archive(archive: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip" or archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(out_dir)
        return
    if archive.name.endswith(".tar.xz") or archive.suffixes[-2:] == [".tar", ".xz"]:
        with tarfile.open(archive, "r:xz") as tf:
            tf.extractall(out_dir)
        return
    raise RuntimeError(f"Unsupported archive format: {archive.name}")


def copy_tools_from_extract(extract_dir: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        src = find_extracted_binary(extract_dir, name)
        target = dest / tool_name(name)
        shutil.copy2(src, target)
        make_executable(target)
        print(f"  -> {target} ({target.stat().st_size // (1024 * 1024)} MB)")


def copy_tools_from_path(dest: Path) -> bool:
    ffmpeg = shutil.which(tool_name("ffmpeg")) or shutil.which("ffmpeg")
    ffprobe = shutil.which(tool_name("ffprobe")) or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return False
    dest.mkdir(parents=True, exist_ok=True)
    for src, name in ((ffmpeg, "ffmpeg"), (ffprobe, "ffprobe")):
        target = dest / tool_name(name)
        shutil.copy2(src, target)
        make_executable(target)
        print(f"Copied {name} from PATH -> {target}")
    return True


def ffmpeg_archive_for_host() -> tuple[str, str]:
    """Return (url, kind) where kind is 'zip' or 'tar.xz'."""
    system = sys.platform
    machine = platform.machine().lower()

    if system == "win32":
        if machine in ("arm64", "aarch64"):
            name = "ffmpeg-master-latest-winarm64-gpl.zip"
        else:
            name = "ffmpeg-master-latest-win64-gpl.zip"
        return f"{BTBN_LATEST}/{name}", "zip"

    if system.startswith("linux"):
        if machine in ("arm64", "aarch64"):
            name = "ffmpeg-master-latest-linuxarm64-gpl.tar.xz"
        else:
            name = "ffmpeg-master-latest-linux64-gpl.tar.xz"
        return f"{BTBN_LATEST}/{name}", "tar.xz"

    raise RuntimeError(f"No BtbN package mapping for {system}/{machine}")


def darwin_static_urls() -> tuple[str, str]:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return (
        f"{FFMPEG_STATIC_BASE}/ffmpeg-darwin-{arch}",
        f"{FFMPEG_STATIC_BASE}/ffprobe-darwin-{arch}",
    )


def ensure_ffmpeg(*, refresh: bool, use_system: bool) -> Path:
    ffmpeg_path = VENDOR / tool_name("ffmpeg")
    ffprobe_path = VENDOR / tool_name("ffprobe")
    if ffmpeg_path.is_file() and ffprobe_path.is_file() and not refresh:
        print(f"Using existing ffmpeg tools in {VENDOR}")
        return VENDOR

    if VENDOR.exists() and refresh:
        shutil.rmtree(VENDOR)
    VENDOR.mkdir(parents=True, exist_ok=True)

    if use_system:
        if not copy_tools_from_path(VENDOR):
            raise SystemExit(" --use-system-ffmpeg set but ffmpeg/ffprobe not on PATH")
        print("WARNING: system/Homebrew ffmpeg is often not portable across machines.")
        return VENDOR

    if sys.platform == "darwin":
        print(f"Downloading portable macOS ffmpeg ({FFMPEG_STATIC_TAG})…")
        ffmpeg_url, ffprobe_url = darwin_static_urls()
        for url, name in ((ffmpeg_url, "ffmpeg"), (ffprobe_url, "ffprobe")):
            target = VENDOR / name
            download(url, target)
            make_executable(target)
            print(f"  -> {target} ({target.stat().st_size // (1024 * 1024)} MB)")
        return VENDOR

    url, _kind = ffmpeg_archive_for_host()
    with tempfile.TemporaryDirectory(prefix="ffmpeg-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / Path(url).name
        download(url, archive)
        extract_dir = tmp_path / "extract"
        print(f"Extracting {archive.name} …")
        extract_archive(archive, extract_dir)
        copy_tools_from_extract(extract_dir, VENDOR)
    return VENDOR


def run_pyinstaller(vendor: Path) -> Path:
    sep = ";" if sys.platform == "win32" else ":"
    ffmpeg = vendor / tool_name("ffmpeg")
    ffprobe = vendor / tool_name("ffprobe")
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise SystemExit(f"Missing bundled tools under {vendor}")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "MusicTagger",
        f"--add-binary={ffmpeg}{sep}ffmpeg",
        f"--add-binary={ffprobe}{sep}ffmpeg",
        str(ROOT / "music_tagger.py"),
    ]
    # Windows: hide console. macOS: avoid --windowed+--onefile (deprecated in PyInstaller 7).
    if sys.platform == "win32":
        cmd.append("--windowed")

    print("Running:", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)

    if sys.platform == "win32":
        artifact = DIST / "MusicTagger.exe"
    else:
        artifact = DIST / "MusicTagger"
    if not artifact.exists():
        # macOS windowed sometimes lands as .app — still report dist/
        raise SystemExit(f"Build finished but artifact not found at {artifact}")
    print(f"Built: {artifact} ({artifact.stat().st_size // (1024 * 1024)} MB)")
    return artifact


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh-ffmpeg",
        action="store_true",
        help="Re-download/copy ffmpeg even if vendor/ already exists",
    )
    parser.add_argument(
        "--use-system-ffmpeg",
        action="store_true",
        help="Copy ffmpeg/ffprobe from PATH instead of downloading portable builds",
    )
    parser.add_argument(
        "--ffmpeg-only",
        action="store_true",
        help="Only prepare vendor/ffmpeg, do not run PyInstaller",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vendor = ensure_ffmpeg(
        refresh=args.refresh_ffmpeg,
        use_system=args.use_system_ffmpeg,
    )
    if args.ffmpeg_only:
        print(f"ffmpeg ready in {vendor}")
        return 0
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "PyInstaller not installed. Run:\n"
            f"  {sys.executable} -m pip install -r requirements-build.txt",
            file=sys.stderr,
        )
        return 1
    run_pyinstaller(vendor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
