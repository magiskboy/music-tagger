#!/usr/bin/env python3
"""Music Tagger — scan a folder and edit audio tags via ffmpeg/ffprobe."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import tkinter.font as tkfont
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
    ".aiff",
    ".aif",
    ".alac",
}

# Display columns (key -> header). File/Folder come first in the tree.
TAG_COLUMNS: list[tuple[str, str, int]] = [
    ("title", "Title", 160),
    ("artist", "Artist", 120),
    ("album", "Album", 140),
    ("album_artist", "Album Artist", 120),
    ("track", "Track", 50),
    ("date", "Year", 50),
    ("genre", "Genre", 90),
    ("comment", "Comment", 120),
]

# Alternate ffprobe keys that map to our canonical names.
TAG_ALIASES: dict[str, str] = {
    "TITLE": "title",
    "ARTIST": "artist",
    "ALBUM": "album",
    "ALBUM_ARTIST": "album_artist",
    "albumartist": "album_artist",
    "TRACK": "track",
    "tracknumber": "track",
    "TRACKNUMBER": "track",
    "DATE": "date",
    "YEAR": "date",
    "year": "date",
    "GENRE": "genre",
    "COMMENT": "comment",
}

FFPROBE_TIMEOUT_SEC = 15
FFMPEG_TIMEOUT_SEC = 120
READ_WORKERS = 6

_FFMPEG: str | None = None
_FFPROBE: str | None = None


def _tool_filename(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def _iter_tool_candidates(name: str) -> list[Path]:
    binary = _tool_filename(name)
    candidates: list[Path] = []
    # PyInstaller --onefile extracts to sys._MEIPASS
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "ffmpeg" / binary)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "ffmpeg" / binary)
    candidates.append(Path(__file__).resolve().parent / "vendor" / "ffmpeg" / binary)
    return candidates


def find_tool(name: str) -> str | None:
    """Prefer bundled ffmpeg/ffprobe, then PATH."""
    for path in _iter_tool_candidates(name):
        if path.is_file():
            return str(path)
    return shutil.which(_tool_filename(name)) or shutil.which(name)


def ffmpeg_bin() -> str:
    global _FFMPEG
    if _FFMPEG is None:
        found = find_tool("ffmpeg")
        if not found:
            raise RuntimeError("ffmpeg not found (bundled or PATH)")
        _FFMPEG = found
    return _FFMPEG


def ffprobe_bin() -> str:
    global _FFPROBE
    if _FFPROBE is None:
        found = find_tool("ffprobe")
        if not found:
            raise RuntimeError("ffprobe not found (bundled or PATH)")
        _FFPROBE = found
    return _FFPROBE


def require_binaries() -> None:
    missing = [name for name in ("ffprobe", "ffmpeg") if find_tool(name) is None]
    if missing:
        raise SystemExit(
            f"Missing required tool(s): {', '.join(missing)}. "
            "Bundle ffmpeg via build.py or install it on PATH."
        )


def scan_audio_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                files.append(path)
    files.sort(key=lambda p: str(p).lower())
    return files


def normalize_tags(raw: dict[str, Any]) -> dict[str, str]:
    tags = {key: "" for key, _, _ in TAG_COLUMNS}
    for key, value in raw.items():
        if value is None:
            continue
        text = str(value).strip()
        canon = TAG_ALIASES.get(key, key.lower())
        if canon in tags and not tags[canon]:
            tags[canon] = text
    return tags


def empty_tags() -> dict[str, str]:
    return {key: "" for key, _, _ in TAG_COLUMNS}


def read_tags(path: Path) -> dict[str, str]:
    cmd = [
        ffprobe_bin(),
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_entries",
        "format_tags",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=FFPROBE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return empty_tags() | {"_error": f"ffprobe timed out after {FFPROBE_TIMEOUT_SEC}s"}
    except OSError as exc:
        return empty_tags() | {"_error": str(exc)}

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "ffprobe failed").strip()
        return empty_tags() | {"_error": err}

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return empty_tags()

    raw = (data.get("format") or {}).get("tags") or {}
    return normalize_tags(raw)


def play_with_system(path: Path) -> None:
    """Open/play a file with the OS default application (macOS / Windows / Linux)."""
    target = path.expanduser()
    if not target.is_file():
        raise FileNotFoundError(f"File not found: {target}")

    resolved = str(target)

    if sys.platform == "darwin":
        # Finder / default app (Music, VLC, …)
        cmd = shutil.which("open")
        if not cmd:
            raise RuntimeError("macOS 'open' command not found")
        subprocess.Popen([cmd, resolved], start_new_session=True)
        return

    if sys.platform == "win32":
        # Default associated player via shell verb "open"
        try:
            os.startfile(resolved)  # type: ignore[attr-defined]
        except OSError:
            # Fallback when association/startfile fails
            subprocess.Popen(
                ["cmd", "/c", "start", "", resolved],
                start_new_session=True,
            )
        return

    # Linux / *BSD / other Unix
    for tool in ("xdg-open", "gio", "kde-open", "gnome-open"):
        cmd = shutil.which(tool)
        if not cmd:
            continue
        args = [cmd, "open", resolved] if tool == "gio" else [cmd, resolved]
        subprocess.Popen(args, start_new_session=True)
        return

    raise RuntimeError(
        "No system opener found (tried xdg-open, gio, kde-open, gnome-open)"
    )


def _applescript_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _copy_files_macos(paths: list[Path]) -> None:
    items = ", ".join(
        f"POSIX file {_applescript_quote(str(p.resolve()))}" for p in paths
    )
    script = f"set the clipboard to {{{items}}}"
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "osascript failed").strip()
        raise RuntimeError(err)


def _copy_files_windows(paths: list[Path]) -> None:
    import ctypes
    from ctypes import wintypes

    CF_HDROP = 15
    GMEM_MOVEABLE = 0x0002

    class DROPFILES(ctypes.Structure):
        _fields_ = [
            ("pFiles", wintypes.DWORD),
            ("pt", wintypes.POINT),
            ("fNC", wintypes.BOOL),
            ("fWide", wintypes.BOOL),
        ]

    file_list = ("\0".join(str(p.resolve()) for p in paths) + "\0\0").encode(
        "utf-16-le"
    )
    header = DROPFILES()
    header.pFiles = ctypes.sizeof(DROPFILES)
    header.pt.x = 0
    header.pt.y = 0
    header.fNC = False
    header.fWide = True

    total = ctypes.sizeof(DROPFILES) + len(file_list)
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, total)
    if not handle:
        raise RuntimeError("GlobalAlloc failed")
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        raise RuntimeError("GlobalLock failed")
    try:
        ctypes.memmove(locked, ctypes.byref(header), ctypes.sizeof(DROPFILES))
        ctypes.memmove(locked + ctypes.sizeof(DROPFILES), file_list, len(file_list))
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise RuntimeError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_HDROP, handle):
            kernel32.GlobalFree(handle)
            raise RuntimeError("SetClipboardData failed")
        # Clipboard now owns `handle`.
    finally:
        user32.CloseClipboard()


def _copy_files_linux(paths: list[Path]) -> None:
    uris = [p.resolve().as_uri() for p in paths]
    uri_payload = ("\n".join(uris) + "\n").encode("utf-8")
    gnome_payload = ("copy\n" + "\n".join(uris) + "\n").encode("utf-8")

    errors: list[str] = []

    # Wayland
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        result = subprocess.run(
            ["wl-copy", "--type", "text/uri-list"],
            input=uri_payload,
            check=False,
        )
        if result.returncode == 0:
            return
        errors.append("wl-copy failed")

    # X11 — prefer GNOME file-manager format, then generic uri-list
    xclip = shutil.which("xclip")
    if xclip:
        for mime, payload in (
            ("x-special/gnome-copied-files", gnome_payload),
            ("text/uri-list", uri_payload),
        ):
            result = subprocess.run(
                [xclip, "-selection", "clipboard", "-t", mime],
                input=payload,
                check=False,
            )
            if result.returncode == 0:
                return
        errors.append("xclip failed")

    xsel = shutil.which("xsel")
    if xsel:
        result = subprocess.run(
            [xsel, "--clipboard", "--input"],
            input=uri_payload,
            check=False,
        )
        if result.returncode == 0:
            return
        errors.append("xsel failed")

    hint = "Install wl-clipboard (Wayland) or xclip (X11)."
    detail = f" ({'; '.join(errors)})" if errors else ""
    raise RuntimeError(f"No working clipboard tool for file copy.{detail} {hint}")


def copy_files_to_clipboard(paths: list[Path]) -> None:
    """Put file paths on the system clipboard so Finder/Explorer/file managers can Paste."""
    if not paths:
        raise ValueError("No files to copy")
    missing = [p for p in paths if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"File not found: {missing[0]}")

    if sys.platform == "darwin":
        _copy_files_macos(paths)
    elif sys.platform == "win32":
        _copy_files_windows(paths)
    else:
        _copy_files_linux(paths)


def write_tags(path: Path, tags: dict[str, str]) -> None:
    """Rewrite file metadata with ffmpeg stream copy, then replace original."""
    suffix = path.suffix
    fd, tmp_name = tempfile.mkstemp(suffix=suffix, dir=path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)

    cmd = [ffmpeg_bin(), "-y", "-i", str(path), "-map_metadata", "-1"]
    for key, _, _ in TAG_COLUMNS:
        value = tags.get(key, "").strip()
        if value:
            cmd.extend(["-metadata", f"{key}={value}"])
    cmd.extend(["-c", "copy", str(tmp_path)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=FFMPEG_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "ffmpeg failed").strip()
            tail = "\n".join(err.splitlines()[-12:])
            raise RuntimeError(tail or "ffmpeg failed")
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


class MusicTaggerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.folder: Path | None = None
        self.title("Music Tagger")
        self.geometry("1200x640")
        self.minsize(800, 400)

        self.rows: dict[str, dict[str, Any]] = {}  # iid -> {path, tags, dirty}
        self._edit_entry: ttk.Entry | None = None
        self._edit_iid: str | None = None
        self._edit_col: str | None = None
        self._sort_col: str | None = None
        self._sort_reverse = False
        self._column_titles: dict[str, str] = {
            "file": "File",
            "folder": "Folder",
            **{key: header for key, header, _ in TAG_COLUMNS},
        }
        self._tooltip: tk.Toplevel | None = None
        self._tooltip_text = ""

        self._ui_queue: queue.Queue = queue.Queue()
        self._load_generation = 0
        self._busy = False

        self._build_ui()
        self.after(100, self._poll_queue)
        self.after(100, self.open_folder)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=(8, 8, 8, 4))
        top.pack(fill=tk.X)

        self.open_btn = ttk.Button(top, text="Open Folder…", command=self.open_folder)
        self.open_btn.pack(side=tk.LEFT)
        self.folder_var = tk.StringVar(value="No folder selected")
        ttk.Label(top, textvariable=self.folder_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0)
        )

        btn_bar = ttk.Frame(self, padding=(8, 4, 8, 8))
        btn_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self.reload_btn = ttk.Button(btn_bar, text="Reload", command=self.load_files)
        self.reload_btn.pack(side=tk.LEFT)
        self.apply_sel_btn = ttk.Button(
            btn_bar, text="Apply Selected", command=self.apply_selected
        )
        self.apply_sel_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.apply_all_btn = ttk.Button(
            btn_bar, text="Apply All Dirty", command=self.apply_all_dirty
        )
        self.apply_all_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.copy_btn = ttk.Button(
            btn_bar, text="Copy Files", command=self.copy_selected_files
        )
        self.copy_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.summary_var = tk.StringVar(value="0 files · 0 changed")
        ttk.Label(btn_bar, textvariable=self.summary_var).pack(side=tk.RIGHT)

        self.status_var = tk.StringVar(value="")
        ttk.Label(btn_bar, textvariable=self.status_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(16, 16)
        )

        tree_frame = ttk.Frame(self, padding=(8, 0, 8, 0))
        tree_frame.pack(fill=tk.BOTH, expand=True)

        # Taller rows so inline editors (and Vietnamese diacritics) are not clipped.
        self._ui_font = tkfont.nametofont("TkDefaultFont")
        style = ttk.Style(self)
        row_h = max(28, self._ui_font.metrics("linespace") + 12)
        style.configure("Treeview", rowheight=row_h, font=self._ui_font)
        style.configure("Treeview.Heading", font=self._ui_font)

        columns = ("file", "folder") + tuple(key for key, _, _ in TAG_COLUMNS)
        self.tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
        )

        # File = basename only (stretch); Folder = parent path (fixed, compact).
        self.tree.heading(
            "file", text="File", command=lambda: self.sort_by_column("file")
        )
        self.tree.column("file", width=260, minwidth=140, stretch=True, anchor=tk.W)
        self.tree.heading(
            "folder", text="Folder", command=lambda: self.sort_by_column("folder")
        )
        self.tree.column("folder", width=110, minwidth=60, stretch=False, anchor=tk.W)
        for key, header, width in TAG_COLUMNS:
            self.tree.heading(
                key, text=header, command=lambda c=key: self.sort_by_column(c)
            )
            self.tree.column(
                key, width=width, minwidth=40, stretch=False, anchor=tk.W
            )

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self.tree.tag_configure("dirty", foreground="#0b5fff")
        self.tree.tag_configure("error", foreground="#b00020")

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-1>", self._on_click_elsewhere)
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", lambda _e: self._hide_tooltip())
        self.bind("<Escape>", lambda _e: self._cancel_edit())
        # File copy for system Paste (Explorer / Finder / file managers)
        self.bind("<Control-c>", self._on_copy_shortcut)
        self.bind("<Control-C>", self._on_copy_shortcut)
        self.bind("<Command-c>", self._on_copy_shortcut)
        self.tree.bind("<Control-c>", self._on_copy_shortcut)
        self.tree.bind("<Control-C>", self._on_copy_shortcut)
        self.tree.bind("<Command-c>", self._on_copy_shortcut)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for btn in (
            self.open_btn,
            self.reload_btn,
            self.apply_sel_btn,
            self.apply_all_btn,
            self.copy_btn,
        ):
            btn.configure(state=state)

    def open_folder(self) -> None:
        if self._busy:
            return
        initial = str(self.folder) if self.folder else str(Path.home())
        chosen = filedialog.askdirectory(
            parent=self,
            title="Select music folder",
            initialdir=initial,
            mustexist=True,
        )
        if not chosen:
            return
        folder = Path(chosen).expanduser()
        # Avoid resolve() on network mounts — can be slow.
        folder = folder.absolute()
        if not folder.is_dir():
            messagebox.showerror("Open Folder", f"Not a directory:\n{folder}")
            return
        self.folder = folder
        self.folder_var.set(str(folder))
        self.title(f"Music Tagger — {folder.name}")
        self.load_files()

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "clear":
                    self._cancel_edit()
                    for iid in self.tree.get_children():
                        self.tree.delete(iid)
                    self.rows.clear()
                    self._sort_col = None
                    self._sort_reverse = False
                    self._refresh_heading_labels()
                    self._update_summary()
                elif kind == "row":
                    self._add_row(payload)
                elif kind == "load_done":
                    self._set_busy(False)
                    _total, errors = payload
                    self.status_var.set(
                        f"{errors} read error(s)" if errors else ""
                    )
                    self._update_summary()
                elif kind == "apply_item":
                    self._apply_item_result(payload)
                elif kind == "apply_done":
                    self._set_busy(False)
                    ok, total, errors = payload
                    self.status_var.set("")
                    self._update_summary()
                    if errors:
                        messagebox.showwarning(
                            "Apply finished with errors",
                            f"Updated {ok}/{total} file(s).\n\n"
                            + "\n\n".join(errors[:8])
                            + ("\n\n…" if len(errors) > 8 else ""),
                        )
                    else:
                        messagebox.showinfo("Apply", f"Updated {ok} file(s).")
                elif kind == "error":
                    self._set_busy(False)
                    self.status_var.set("")
                    messagebox.showerror("Error", payload)
        except queue.Empty:
            pass
        self.after(50, self._poll_queue)

    def _add_row(self, item: dict[str, Any]) -> None:
        path: Path = item["path"]
        tags: dict[str, str] = item["tags"]
        error = tags.pop("_error", None)
        try:
            rel_path = path.relative_to(self.folder)
            rel = str(rel_path)
            folder = "" if rel_path.parent == Path(".") else str(rel_path.parent)
        except ValueError:
            rel = str(path)
            folder = str(path.parent)

        name = path.name
        values = [name, folder] + [tags.get(key, "") for key, _, _ in TAG_COLUMNS]
        iid = self.tree.insert("", tk.END, values=values)
        self.rows[iid] = {
            "path": path,
            "rel": rel,
            "name": name,
            "folder": folder,
            "tags": tags,
            "original": dict(tags),
            "dirty": False,
            "error": error,
        }
        if error:
            self.tree.item(iid, tags=("error",))

    def load_files(self) -> None:
        if self._busy:
            return
        if self.folder is None:
            self.open_folder()
            return
        folder = self.folder
        self._set_busy(True)
        self._load_generation += 1
        generation = self._load_generation
        self.status_var.set(f"Scanning {folder} …")

        def worker() -> None:
            try:
                self._ui_queue.put(("status", f"Scanning {folder} …"))
                files = scan_audio_files(folder)
                if generation != self._load_generation:
                    return

                self._ui_queue.put(("clear", None))
                total = len(files)

                if total == 0:
                    self._ui_queue.put(("load_done", (0, 0)))
                    return

                done = 0
                errors = 0
                # Preserve display order: collect then emit sorted by index.
                results: list[dict[str, Any] | None] = [None] * total

                with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
                    futures = {
                        pool.submit(read_tags, path): idx
                        for idx, path in enumerate(files)
                    }
                    for fut in as_completed(futures):
                        if generation != self._load_generation:
                            pool.shutdown(wait=False, cancel_futures=True)
                            return
                        idx = futures[fut]
                        path = files[idx]
                        tags = fut.result()
                        if tags.get("_error"):
                            errors += 1
                        results[idx] = {"path": path, "tags": tags}
                        done += 1
                        self._ui_queue.put(
                            (
                                "status",
                                f"Reading {path.stem}… ({done}/{total})",
                            )
                        )

                for item in results:
                    if item is not None:
                        self._ui_queue.put(("row", item))
                self._ui_queue.put(("load_done", (total, errors)))
            except Exception as exc:  # noqa: BLE001
                self._ui_queue.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _update_summary(self) -> None:
        total = len(self.rows)
        dirty = sum(1 for r in self.rows.values() if r["dirty"])
        self.summary_var.set(f"{total} files · {dirty} changed")

    def _refresh_heading_labels(self) -> None:
        for col, title in self._column_titles.items():
            if col == self._sort_col:
                marker = " ▼" if self._sort_reverse else " ▲"
                self.tree.heading(col, text=title + marker)
            else:
                self.tree.heading(col, text=title)

    @staticmethod
    def _sort_key(col: str, value: str) -> tuple:
        text = value[2:] if col == "file" and value.startswith("* ") else value
        text = text.strip()
        if col in ("track", "date"):
            # "3/12" -> 3; non-numeric sorts after numbers
            num_part = text.split("/", 1)[0].strip()
            try:
                return (0, int(num_part), text.casefold())
            except ValueError:
                return (1, 0, text.casefold())
        return (0, 0, text.casefold())

    def sort_by_column(self, col: str) -> None:
        if self._busy:
            return
        self._commit_edit()

        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False

        items = list(self.tree.get_children(""))
        items.sort(
            key=lambda iid: self._sort_key(col, self.tree.set(iid, col)),
            reverse=self._sort_reverse,
        )
        for index, iid in enumerate(items):
            self.tree.move(iid, "", index)

        self._refresh_heading_labels()

    def _on_click_elsewhere(self, _event: tk.Event) -> None:
        if self._edit_entry is not None:
            self._commit_edit()

    def _on_copy_shortcut(self, _event: tk.Event | None = None) -> str:
        # Don't steal Ctrl/Cmd+C while editing a cell.
        if self._edit_entry is not None:
            return ""
        self.copy_selected_files()
        return "break"

    def copy_selected_files(self) -> None:
        if self._busy:
            return
        self._commit_edit()
        selected = list(self.tree.selection())
        if not selected:
            messagebox.showinfo("Copy Files", "Select one or more rows first.")
            return
        paths = [self.rows[iid]["path"] for iid in selected if iid in self.rows]
        try:
            copy_files_to_clipboard(paths)
        except (OSError, RuntimeError, ValueError, FileNotFoundError) as exc:
            messagebox.showerror("Copy Files", str(exc))
            return
        self.status_var.set(f"Copied {len(paths)} file(s)")
        self.after(2000, lambda: self.status_var.set("") if not self._busy else None)

    def _on_double_click(self, event: tk.Event) -> str | None:
        if self._busy:
            return "break"
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return None

        column = self.tree.identify_column(event.x)
        row_iid = self.tree.identify_row(event.y)
        if not row_iid:
            return None

        col_index = int(column.replace("#", "")) - 1
        col_ids = self.tree["columns"]
        col_id = col_ids[col_index]
        if col_id in ("file", "folder"):
            self._cancel_edit()
            if col_id == "file":
                row = self.rows.get(row_iid)
                if row is None:
                    return "break"
                try:
                    play_with_system(row["path"])
                except (OSError, RuntimeError) as exc:
                    messagebox.showerror("Play", f"Could not open file:\n{exc}")
            return "break"

        self._cancel_edit()
        bbox = self.tree.bbox(row_iid, column)
        if not bbox:
            return None
        x, y, width, height = bbox

        # ttk.Entry clipped to Treeview bbox is too short on macOS; use tk.Entry
        # sized by font metrics and centered in the cell.
        line = self._ui_font.metrics("linespace")
        edit_h = max(height, line + 10)
        edit_y = y + (height - edit_h) // 2

        value = self.tree.set(row_iid, col_id)
        entry = tk.Entry(
            self.tree,
            font=self._ui_font,
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
        )
        entry.place(x=x, y=edit_y, width=width, height=edit_h)
        entry.insert(0, value)
        entry.select_range(0, tk.END)
        entry.focus_set()

        entry.bind("<Return>", lambda _e: self._commit_edit())
        entry.bind("<FocusOut>", lambda _e: self._commit_edit())
        entry.bind("<Escape>", lambda _e: self._cancel_edit())

        self._edit_entry = entry
        self._edit_iid = row_iid
        self._edit_col = col_id
        return "break"

    def _cancel_edit(self) -> None:
        if self._edit_entry is not None:
            self._edit_entry.destroy()
        self._edit_entry = None
        self._edit_iid = None
        self._edit_col = None

    def _commit_edit(self) -> None:
        if self._edit_entry is None or self._edit_iid is None or self._edit_col is None:
            return

        new_value = self._edit_entry.get()
        iid = self._edit_iid
        col = self._edit_col
        self._cancel_edit()

        row = self.rows.get(iid)
        if row is None:
            return

        row["tags"][col] = new_value
        self.tree.set(iid, col, new_value)
        self._refresh_dirty(iid)

    def _on_tree_motion(self, event: tk.Event) -> None:
        row_iid = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row_iid or row_iid not in self.rows:
            self._hide_tooltip()
            return
        col_index = int(col.replace("#", "")) - 1
        col_ids = self.tree["columns"]
        if col_index < 0 or col_index >= len(col_ids):
            self._hide_tooltip()
            return
        col_id = col_ids[col_index]
        if col_id not in ("file", "folder"):
            self._hide_tooltip()
            return
        text = self.rows[row_iid]["rel"]
        if text == self._tooltip_text and self._tooltip is not None:
            return
        self._show_tooltip(event.x_root + 12, event.y_root + 8, text)

    def _show_tooltip(self, x: int, y: int, text: str) -> None:
        self._hide_tooltip()
        self._tooltip_text = text
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        label = ttk.Label(
            tip,
            text=text,
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            padding=(6, 3),
        )
        label.pack()
        self._tooltip = tip

    def _hide_tooltip(self) -> None:
        if self._tooltip is not None:
            self._tooltip.destroy()
            self._tooltip = None
        self._tooltip_text = ""

    def _refresh_dirty(self, iid: str) -> None:
        row = self.rows[iid]
        dirty = any(
            row["tags"].get(key, "") != row["original"].get(key, "")
            for key, _, _ in TAG_COLUMNS
        )
        row["dirty"] = dirty
        display = f"* {row['name']}" if dirty else row["name"]
        self.tree.set(iid, "file", display)

        tags: list[str] = []
        if row.get("error"):
            tags.append("error")
        if dirty:
            tags.append("dirty")
        self.tree.item(iid, tags=tuple(tags))

        if not self._busy:
            self._update_summary()

    def _apply_item_result(self, payload: dict[str, Any]) -> None:
        iid = payload["iid"]
        row = self.rows.get(iid)
        if row is None:
            return
        if payload.get("ok"):
            fresh = payload["tags"]
            row["tags"] = fresh
            row["original"] = dict(fresh)
            row["dirty"] = False
            row["error"] = None
            for key, _, _ in TAG_COLUMNS:
                self.tree.set(iid, key, fresh.get(key, ""))
            self._refresh_dirty(iid)
        else:
            row["error"] = payload["error"]
            self.tree.item(
                iid, tags=("error", "dirty") if row["dirty"] else ("error",)
            )

    def _apply_iids(self, iids: list[str]) -> None:
        if not iids:
            messagebox.showinfo("Apply", "No files selected.")
            return
        if self._busy:
            return

        self._set_busy(True)
        self.status_var.set("Writing…")

        # Snapshot tag dicts so edits during write don't race.
        jobs = [
            (iid, self.rows[iid]["path"], dict(self.rows[iid]["tags"]), self.rows[iid]["rel"])
            for iid in iids
        ]

        def worker() -> None:
            ok = 0
            errors: list[str] = []
            total = len(jobs)
            for i, (iid, path, tags, rel) in enumerate(jobs, start=1):
                self._ui_queue.put(
                    ("status", f"Writing {path.stem}… ({i}/{total})")
                )
                try:
                    write_tags(path, tags)
                    fresh = read_tags(path)
                    fresh.pop("_error", None)
                    self._ui_queue.put(
                        ("apply_item", {"iid": iid, "ok": True, "tags": fresh})
                    )
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    self._ui_queue.put(
                        ("apply_item", {"iid": iid, "ok": False, "error": str(exc)})
                    )
                    errors.append(f"{rel}: {exc}")
            self._ui_queue.put(("apply_done", (ok, total, errors)))

        threading.Thread(target=worker, daemon=True).start()

    def apply_selected(self) -> None:
        self._commit_edit()
        selected = list(self.tree.selection())
        dirty_selected = [iid for iid in selected if self.rows[iid]["dirty"]]
        if selected and not dirty_selected:
            if not messagebox.askyesno(
                "Apply Selected",
                "Selected files have no edits. Write current tags anyway?",
            ):
                return
            dirty_selected = selected
        self._apply_iids(dirty_selected)

    def apply_all_dirty(self) -> None:
        self._commit_edit()
        dirty = [iid for iid, row in self.rows.items() if row["dirty"]]
        if not dirty:
            messagebox.showinfo("Apply All", "No unsaved changes.")
            return
        if not messagebox.askyesno(
            "Apply All",
            f"Write tags for {len(dirty)} modified file(s)?",
        ):
            return
        self._apply_iids(dirty)


def main() -> int:
    require_binaries()
    app = MusicTaggerApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
