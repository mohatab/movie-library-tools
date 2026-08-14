#!/usr/bin/env python3
"""
movie_library_watcher.py - Watches D:\\Movies for NEW top-level movie folders and
prepares them automatically: TMDB match -> poster -> .ico -> folder icon -> Arabic subtitle.

CRITICAL RULE: movie folders that already existed when the baseline was established are
NEVER touched. Only folders that appear after initialization are eligible.

After genuinely new movie(s) finish processing, build_movie_picker.py (in this same
directory) is invoked as a subprocess to refresh movie-picker.json/html, coalesced with a
90s debounce and isolated so a dashboard failure never affects a movie's own status.

Modes:
    python movie_library_watcher.py                 # continuous watch
    python movie_library_watcher.py --initialize    # establish baseline only, process nothing
    python movie_library_watcher.py --status        # print state summary
    python movie_library_watcher.py --once          # one reconcile+process pass, then exit
    python movie_library_watcher.py --retry "D:\\Movies\\Movie Name"

Requires: Pillow, watchdog, requests
Env vars: TMDB_API_KEY (required), SUBDL_API_KEY (required for subtitles only)
"""

from __future__ import annotations

import argparse
import ctypes
import difflib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

LIBRARY_ROOT = Path(r"D:\Movies")
STATE_DIR = LIBRARY_ROOT / ".movie-watcher"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "watcher.log"
LOCK_FILE = STATE_DIR / "watcher.lock"
PID_FILE = STATE_DIR / "watcher.pid"
STOP_FILE = STATE_DIR / "stop.request"
LOG_MAX_BYTES = 2 * 1024 * 1024

# Movie Picker auto-update. The picker is invoked as an unmodified subprocess - the
# watcher never imports or duplicates its TMDB/Letterboxd/HTML logic.
PICKER_SCRIPT = LIBRARY_ROOT / "build_movie_picker.py"
PICKER_STATE_FILE = STATE_DIR / "picker_state.json"
PICKER_LOCK_FILE = STATE_DIR / "picker.lock"
PICKER_RUN_LOG_FILE = STATE_DIR / "picker_last_run.log"
PICKER_DEBOUNCE_SECONDS = 90        # coalesce movies that finish close together
PICKER_TIMEOUT_SECONDS = 20 * 60    # generous ceiling for a cold-cache first run
PICKER_RETRY_BACKOFF_SECONDS = 5 * 60

STATE_VERSION = 1

# Folders that are never candidates for processing.
EXCLUDED_NAMES = {
    "cache",
    "__pycache__",
    "system volume information",
    "$recycle.bin",
    "found.000",
    "movie-watcher",
}
# Any folder whose name starts with one of these is skipped ('#Done', '#Donre', '.claude', ...)
EXCLUDED_PREFIXES = (".", "#", "$", "~")

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts",
    ".flv", ".webm", ".mpg", ".mpeg", ".divx", ".rmvb",
}
# Signs that a download/copy is still in flight.
PARTIAL_EXTS = {
    ".part", ".crdownload", ".!ut", ".tmp", ".partial", ".downloading",
    ".aria2", ".!qb", ".bts", ".opdownload", ".filepart",
}

MIN_VIDEO_BYTES = 20 * 1024 * 1024  # ignore trailers/samples smaller than this

# Stability: a folder must present the same snapshot this many consecutive scans.
SCAN_INTERVAL = 10          # seconds between scans
STABLE_SCANS_REQUIRED = 3   # -> ~30s of no change
MIN_FOLDER_AGE = 20         # seconds since folder creation before we even look
STABILITY_TIMEOUT = 6 * 3600

# Politeness
TMDB_MIN_GAP = 0.35
SUBDL_MIN_GAP = 1.0

# Matching thresholds
TMDB_ACCEPT_WITH_YEAR = 0.82
TMDB_ACCEPT_NO_YEAR = 0.92
SUBTITLE_ACCEPT = 0.50

# Output file names created inside NEW movie folders only
ICO_NAME = "folder.ico"
DESKTOP_INI = "desktop.ini"
SUBTITLE_SUFFIX = ".ar.srt"

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w780"
SUBDL_API = "https://api.subdl.com/api/v1/subtitles"
SUBDL_DL = "https://dl.subdl.com"

USER_AGENT = "movie-library-watcher/1.0"

# --------------------------------------------------------------------------------------
# Secrets - environment only, never written anywhere
# --------------------------------------------------------------------------------------

_SECRETS: list[str] = []


def _read_env(name: str) -> str | None:
    """Read an env var. Falls back to the current user's registry environment, because
    a process started from a shell that predates the variable will not have inherited it.
    The value is never logged, stored, or echoed."""
    val = os.environ.get(name)
    if val:
        val = val.strip()
    if not val and sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                val = str(winreg.QueryValueEx(key, name)[0]).strip()
        except OSError:
            val = None
    if val:
        _SECRETS.append(val)
    return val or None


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret and secret in text:
            text = text.replace(secret, "***REDACTED***")
    return text


# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

_log_lock = threading.Lock()


def log(message: str, *, folder: str | None = None) -> None:
    message = redact(str(message))
    stamp = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{stamp}] "
    if folder:
        prefix += f"({folder}) "
    line = prefix + message
    with _log_lock:
        print(line, flush=True)
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
                backup = LOG_FILE.with_suffix(".log.1")
                backup.unlink(missing_ok=True)
                LOG_FILE.rename(backup)
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now().strftime('%Y-%m-%d')} {line}\n")
        except OSError:
            pass


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------------------
# Single instance
# --------------------------------------------------------------------------------------


class SingleInstance:
    """An exclusive lock held for the lifetime of the process.

    Two watchers on one library would race on state.json and download the same poster and
    subtitle twice, so only one may run. The lock is an OS byte-range lock rather than a
    PID file, which means the OS drops it automatically if the process is killed - a crash
    can never leave a stale lock that blocks the next start.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.handle.close()
        self.handle = None


def running_watcher_pid() -> int | None:
    """None if no watcher holds the lock, else the pid it recorded (0 if unknown)."""
    probe = SingleInstance(LOCK_FILE)
    if probe.acquire():
        probe.release()
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


# --------------------------------------------------------------------------------------
# Movie Picker dashboard state (separate from per-movie state.json on purpose - a
# dashboard-rebuild failure must never be able to touch a movie's own record).
# --------------------------------------------------------------------------------------


def load_picker_state() -> dict:
    default = {"status": None, "last_attempt": None, "last_success": None, "last_error": None}
    if not PICKER_STATE_FILE.exists():
        return default
    try:
        data = json.loads(PICKER_STATE_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default
    default.update(data)
    return default


def save_picker_state(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PICKER_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PICKER_STATE_FILE)


# --------------------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------------------


class State:
    def __init__(self) -> None:
        self.data: dict = {
            "version": STATE_VERSION,
            "library_root": str(LIBRARY_ROOT),
            "initialized_at": None,
            "movies": {},
        }
        self._lock = threading.Lock()

    # -- persistence -------------------------------------------------------------------
    def load(self) -> bool:
        if not STATE_FILE.exists():
            return False
        try:
            # utf-8-sig so a state file re-saved by an external editor (or PowerShell,
            # which writes a BOM) still loads.
            self.data = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"WARNING: could not read state file ({exc}); refusing to guess. "
                f"Fix or delete {STATE_FILE} and re-run --initialize.")
            raise SystemExit(2)
        self.data.setdefault("movies", {})
        return True

    def save(self) -> None:
        with self._lock:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(STATE_FILE)

    # -- accessors ---------------------------------------------------------------------
    @property
    def movies(self) -> dict:
        return self.data["movies"]

    def known(self, name: str) -> bool:
        return name in self.movies

    def is_baseline(self, name: str) -> bool:
        return self.movies.get(name, {}).get("status") == "existing_at_initialization"

    def record_baseline(self, names: list[str]) -> None:
        stamp = now_iso()
        self.data["initialized_at"] = stamp
        for name in names:
            self.movies[name] = {
                "folder_name": name,
                "folder_path": str(LIBRARY_ROOT / name),
                "status": "existing_at_initialization",
                "detected_at": stamp,
                "note": "present before watcher initialization - never processed",
            }

    def add_new(self, name: str) -> dict:
        entry = {
            "folder_name": name,
            "folder_path": str(LIBRARY_ROOT / name),
            "status": "pending",
            "detected_at": now_iso(),
            "tmdb_id": None,
            "title": None,
            "year": None,
            "match_method": None,
            "match_score": None,
            "poster_status": "pending",
            "ico_status": "pending",
            "icon_status": "pending",
            "subtitle_status": "pending",
            "subtitle_file": None,
            "errors": [],
            "last_attempt": None,
            "completed_at": None,
        }
        self.movies[name] = entry
        return entry


# --------------------------------------------------------------------------------------
# Folder discovery
# --------------------------------------------------------------------------------------


def is_candidate_dir(path: Path) -> bool:
    name = path.name
    if not name or name.startswith(EXCLUDED_PREFIXES):
        return False
    if name.lower() in EXCLUDED_NAMES:
        return False
    return True


def list_top_level_dirs() -> list[str]:
    out = []
    try:
        for entry in os.scandir(LIBRARY_ROOT):
            if entry.is_dir(follow_symlinks=False) and is_candidate_dir(Path(entry.path)):
                out.append(entry.name)
    except OSError as exc:
        log(f"WARNING: cannot list {LIBRARY_ROOT}: {exc}")
    return sorted(out)


def find_video(folder: Path) -> Path | None:
    """Largest video file directly in the folder (or one level down, as some releases nest)."""
    best: Path | None = None
    best_size = 0
    for depth_glob in ("*", "*/*"):
        for p in folder.glob(depth_glob):
            try:
                if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
                    continue
                size = p.stat().st_size
            except OSError:
                continue
            if size < MIN_VIDEO_BYTES:
                continue
            if "sample" in p.stem.lower() and size < 400 * 1024 * 1024:
                continue
            if size > best_size:
                best, best_size = p, size
        if best is not None:
            break
    return best


def folder_snapshot(folder: Path) -> tuple | None:
    """(files+sizes, has_partial). None if the folder vanished."""
    items = []
    has_partial = False
    try:
        for p in folder.rglob("*"):
            try:
                if p.is_dir():
                    continue
                st = p.stat()
            except OSError:
                continue
            if p.suffix.lower() in PARTIAL_EXTS:
                has_partial = True
            items.append((str(p.relative_to(folder)).lower(), st.st_size))
    except OSError:
        return None
    return (tuple(sorted(items)), has_partial)


# --------------------------------------------------------------------------------------
# Title / year extraction
# --------------------------------------------------------------------------------------

JUNK_TOKENS = {
    # resolution / source
    "480p", "540p", "576p", "720p", "1080p", "1080i", "1440p", "2160p", "4k", "8k", "uhd",
    "hd", "fhd", "sd", "webrip", "web", "webdl", "web-dl", "bluray", "blu-ray", "brrip",
    "bdrip", "bdremux", "remux", "hdrip", "dvdrip", "dvdscr", "dvd", "hdtv", "pdtv",
    "hdcam", "cam", "ts", "tc", "telesync", "telecine", "r5", "vhsrip", "amzn", "nf",
    "netflix", "hulu", "dsnp", "disney", "hmax", "max", "atvp", "pcok", "stan", "itunes",
    "ma", "iplayer", "crav",
    # codecs / hdr
    "x264", "x265", "h264", "h265", "h", "avc", "hevc", "xvid", "divx", "10bit", "8bit",
    "hdr", "hdr10", "hdr10plus", "dv", "dolbyvision", "sdr", "hlg", "bit",
    # audio
    "aac", "aac2", "aac5", "ac3", "eac3", "dd", "ddp", "dd5", "ddp5", "dts", "dtshd",
    "dts-hd", "truehd", "atmos", "flac", "mp3", "opus", "2ch", "6ch", "8ch", "5", "7",
    "1", "0", "2", "channel", "dual", "audio",
    # editions / misc
    "extended", "unrated", "uncut", "directors", "director", "cut", "dc", "theatrical",
    "remastered", "restored", "criterion", "imax", "proper", "repack", "internal",
    "limited", "festival", "anniversary", "edition", "special", "final", "complete",
    "multi", "subbed", "dubbed", "sub", "subs", "hardsub", "retail", "open", "matte",
    "3d", "half", "sbs", "ou", "hsbs", "part",
}

GROUP_HINTS = {
    "yts", "yify", "rarbg", "etrg", "evo", "fgt", "sparks", "amiable", "geckos", "drones",
    "cinefile", "psa", "galaxyrg", "tigole", "qxr", "megusta", "successfulcrab", "rgb",
    "edith", "cmrg", "nogrp", "flux", "kogi", "playweb", "ntb", "tommy", "sic", "xebec",
}

YEAR_RE = re.compile(r"^(19|20)\d{2}$")


def _tokenize(name: str) -> list[str]:
    name = re.sub(r"[\[\](){}]", " ", name)
    name = re.sub(r"[._\-+]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return [t for t in name.split(" ") if t]


def _is_junk(token: str) -> bool:
    low = token.lower().strip("'\"!,;")
    if low in JUNK_TOKENS or low in GROUP_HINTS:
        return True
    if re.fullmatch(r"\d{3,4}p", low):
        return True
    if re.fullmatch(r"[xh]26[45]", low):
        return True
    return False


def parse_title_year(video_name: str | None, folder_name: str) -> tuple[str, int | None, str]:
    """Return (title, year, source). Video filename wins; folder name is the fallback."""
    for source, raw in (("video_filename", video_name), ("folder_name", folder_name)):
        if not raw:
            continue
        stem = strip_video_ext(raw) if source == "video_filename" else raw
        tokens = _tokenize(stem)
        if not tokens:
            continue

        first_junk = next((i for i, t in enumerate(tokens) if _is_junk(t)), len(tokens))
        year_idx = [i for i, t in enumerate(tokens) if YEAR_RE.match(t.strip("'\""))]
        # A leading year is part of the title ("2001 A Space Odyssey"), never the release year.
        year_idx = [i for i in year_idx if i > 0]

        chosen = None
        before_junk = [i for i in year_idx if i < first_junk]
        if before_junk:
            chosen = before_junk[-1]
        elif year_idx:
            chosen = year_idx[-1]

        if chosen is not None:
            year = int(tokens[chosen].strip("'\""))
            title_tokens = tokens[:chosen]
        else:
            year = None
            title_tokens = tokens[:first_junk]

        title = " ".join(title_tokens).strip(" -._&")
        title = re.sub(r"\s+", " ", title)
        if len(title) >= 2:
            return title, year, source

    return folder_name.strip(), None, "folder_name"


def normalize_title(s: str) -> str:
    s = s.lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9\u0600-\u06ff ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


# --------------------------------------------------------------------------------------
# Rate-limited HTTP
# --------------------------------------------------------------------------------------


class Pacer:
    def __init__(self, gap: float) -> None:
        self.gap = gap
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            delta = time.monotonic() - self.last
            if delta < self.gap:
                time.sleep(self.gap - delta)
            self.last = time.monotonic()


tmdb_pacer = Pacer(TMDB_MIN_GAP)
subdl_pacer = Pacer(SUBDL_MIN_GAP)

SESSION = requests.Session()
SESSION.headers["User-Agent"] = USER_AGENT


class SafeHTTPError(Exception):
    """An HTTP error whose message is guaranteed not to carry a query string (and thus no key)."""


def http_get(url: str, *, pacer: Pacer, params: dict | None = None, stream: bool = False,
             timeout: int = 30) -> requests.Response:
    pacer.wait()
    try:
        resp = SESSION.get(url, params=params, stream=stream, timeout=timeout)
    except requests.RequestException as exc:
        raise SafeHTTPError(f"request to {url.split('?')[0]} failed: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise SafeHTTPError(f"{url.split('?')[0]} returned HTTP {resp.status_code}")
    return resp


# --------------------------------------------------------------------------------------
# TMDB
# --------------------------------------------------------------------------------------


def tmdb_search(api_key: str, title: str, year: int | None) -> list[dict]:
    results: list[dict] = []
    seen: set[int] = set()
    attempts = [{"query": title, "year": year}] if year else []
    attempts.append({"query": title})
    for extra in attempts:
        params = {"api_key": api_key, "include_adult": "false", "language": "en-US"}
        params.update({k: v for k, v in extra.items() if v})
        data = http_get(f"{TMDB_API}/search/movie", pacer=tmdb_pacer, params=params).json()
        for item in data.get("results", []):
            if item["id"] not in seen:
                seen.add(item["id"])
                results.append(item)
        if results and extra.get("year"):
            break
    return results


def score_candidate(cand: dict, title: str, year: int | None) -> tuple[float, str]:
    names = [cand.get("title") or "", cand.get("original_title") or ""]
    sim = max(similarity(title, n) for n in names if n) if any(names) else 0.0
    rel = (cand.get("release_date") or "")[:4]
    cand_year = int(rel) if rel.isdigit() else None

    method = "title_similarity"
    score = sim
    if year and cand_year:
        diff = abs(cand_year - year)
        if diff == 0:
            score = min(1.0, sim + 0.12)
            method = "title+exact_year"
        elif diff == 1:
            score = sim + 0.02
            method = "title+year_off_by_one"
        else:
            score = sim - 0.35
            method = "title_year_mismatch"
    elif year and not cand_year:
        score = sim - 0.10
    # popularity only ever breaks ties
    score += min(cand.get("popularity", 0.0), 100.0) / 100.0 * 0.02
    return score, method


def tmdb_resolve(api_key: str, title: str, year: int | None, folder: str) -> dict | None:
    candidates = tmdb_search(api_key, title, year)
    if not candidates:
        log(f"TMDB: no candidates for '{title}'" + (f" ({year})" if year else ""), folder=folder)
        return None

    scored = sorted(
        ((*score_candidate(c, title, year), c) for c in candidates),
        key=lambda t: t[0],
        reverse=True,
    )
    best_score, best_method, best = scored[0]
    threshold = TMDB_ACCEPT_WITH_YEAR if year else TMDB_ACCEPT_NO_YEAR

    if best_score < threshold:
        log(f"TMDB: best candidate '{best.get('title')}' "
            f"({(best.get('release_date') or '????')[:4]}) scored {best_score:.2f} < "
            f"{threshold:.2f} - too weak, not accepting", folder=folder)
        return None

    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if len(scored) > 1 and best_score - runner_up < 0.05 and best_score < 0.95:
        log(f"TMDB: top two candidates are too close ({best_score:.2f} vs {runner_up:.2f}) "
            f"- ambiguous, not accepting", folder=folder)
        return None

    details = http_get(f"{TMDB_API}/movie/{best['id']}", pacer=tmdb_pacer,
                       params={"api_key": api_key, "language": "en-US"}).json()
    return {
        "id": details["id"],
        "title": details.get("title") or best.get("title"),
        "year": int((details.get("release_date") or "0000")[:4] or 0) or None,
        "poster_path": details.get("poster_path"),
        "imdb_id": details.get("imdb_id"),
        "match_method": best_method,
        "match_score": round(best_score, 3),
    }


# --------------------------------------------------------------------------------------
# Poster -> ICO -> folder icon
# --------------------------------------------------------------------------------------


def fetch_poster(poster_path: str) -> Image.Image:
    """Download the TMDB poster into memory and return a decoded image.

    Nothing is ever written to disk: no poster.jpg, no temporary file. That satisfies
    'delete the temporary poster' by construction - there is no file a crash could orphan,
    and the source image's metadata never lands on the filesystem at all.
    """
    resp = http_get(TMDB_IMG + poster_path, pacer=tmdb_pacer, timeout=60)
    buffer = io.BytesIO(resp.content)
    with Image.open(buffer) as src:
        src.load()          # decode fully before the buffer goes out of scope
        return src.convert("RGBA")


def poster_to_ico(img: Image.Image, ico: Path) -> None:
    """In-memory poster (2:3, JPG or PNG) -> square multi-resolution ICO,
    letterboxed on transparency. Only folder.ico is written."""
    unprotect(ico)
    side = 256
    w, h = img.size
    scale = min(side / w, side / h)
    new = (max(1, round(w * scale)), max(1, round(h * scale)))
    resized = img.resize(new, Image.LANCZOS)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(resized, ((side - new[0]) // 2, (side - new[1]) // 2))
    canvas.save(ico, format="ICO", sizes=ICO_SIZES)


FILE_ATTRIBUTE_READONLY = 0x01
FILE_ATTRIBUTE_HIDDEN = 0x02
FILE_ATTRIBUTE_SYSTEM = 0x04


def set_attributes(path: Path, attrs: int) -> None:
    if sys.platform != "win32":
        return
    current = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    if current == -1:
        current = 0
    ctypes.windll.kernel32.SetFileAttributesW(str(path), current | attrs)


def clear_attributes(path: Path, attrs: int) -> None:
    if sys.platform != "win32":
        return
    current = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    if current == -1:
        return
    ctypes.windll.kernel32.SetFileAttributesW(str(path), current & ~attrs)


def unprotect(path: Path) -> None:
    """Windows refuses to overwrite a hidden/system/read-only file, so clear those bits
    before rewriting a file we created on an earlier run."""
    if path.exists():
        clear_attributes(path, FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_READONLY)


def apply_folder_icon(folder: Path, ico: Path) -> None:
    """Standard Windows desktop.ini mechanism. Relative IconResource keeps the folder
    self-contained and sidesteps any non-ASCII path encoding issues in desktop.ini."""
    ini = folder / DESKTOP_INI
    unprotect(ini)
    content = (
        "[.ShellClassInfo]\r\n"
        f"IconResource={ico.name},0\r\n"
        "ConfirmFileOp=0\r\n"
        "[ViewState]\r\n"
        "Mode=\r\n"
        "Vid=\r\n"
        "FolderType=Generic\r\n"
    )
    ini.write_text(content, encoding="utf-8-sig", newline="")
    set_attributes(ini, FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
    set_attributes(ico, FILE_ATTRIBUTE_HIDDEN)
    # A folder must be ReadOnly (or System) for Explorer to honour its desktop.ini.
    set_attributes(folder, FILE_ATTRIBUTE_READONLY)

    if sys.platform == "win32":
        SHCNE_UPDATEITEM, SHCNF_PATHW = 0x00002000, 0x0005
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_UPDATEITEM, SHCNF_PATHW, ctypes.c_wchar_p(str(folder)), None
        )


# --------------------------------------------------------------------------------------
# Arabic subtitles (SubDL)
# --------------------------------------------------------------------------------------

RELEASE_TOKENS = {
    "resolution": {"480p", "576p", "720p", "1080p", "1440p", "2160p", "4k"},
    "source": {"webrip", "webdl", "web", "bluray", "brrip", "bdrip", "remux", "hdrip",
               "dvdrip", "hdtv", "cam", "ts"},
    "codec": {"x264", "x265", "h264", "h265", "avc", "hevc", "xvid"},
}


def release_features(name: str) -> dict:
    tokens = {t.lower().strip("[]()") for t in _tokenize(name)}
    feats = {}
    for kind, vocab in RELEASE_TOKENS.items():
        found = tokens & vocab
        feats[kind] = next(iter(found)) if found else None
    if feats["source"] == "web":
        feats["source"] = "webdl"
    groups = tokens & GROUP_HINTS
    feats["group"] = next(iter(groups)) if groups else None
    feats["tokens"] = tokens
    return feats


SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt", ".zip", ".rar"}


def strip_ext(name: str, extensions: set[str]) -> str:
    """Remove exactly one *recognised* extension from the end of a filename.

    Deliberately avoids Path.stem / Path.suffix: those split on the last dot whatever it
    is, so a name with no real extension - or an unknown one - silently loses its tail.
    Release names are full of dots and brackets ('...AAC5.1-[YTS.GG - YTS.BZ]'), so the
    rule here is strict: if the name does not end in an extension we know, it is returned
    completely unchanged.
    """
    lowered = name.lower()
    for ext in extensions:
        if lowered.endswith(ext) and len(name) > len(ext):
            return name[: -len(ext)]
    return name


def strip_video_ext(filename: str) -> str:
    """Exact video filename -> same string minus only its video extension."""
    return strip_ext(filename, VIDEO_EXTS)


def strip_known_ext(name: str) -> str:
    return strip_ext(name, VIDEO_EXTS | SUBTITLE_EXTS)


def subtitle_score(sub_release: str, video_name: str) -> float:
    sub_stem = strip_known_ext(sub_release)
    video_stem = strip_known_ext(video_name)
    a = release_features(sub_stem)
    b = release_features(video_stem)
    if normalize_title(sub_stem) == normalize_title(video_stem):
        return 1.0
    score = 0.0
    if a["resolution"] and a["resolution"] == b["resolution"]:
        score += 0.30
    elif a["resolution"] and b["resolution"]:
        score -= 0.10
    if a["source"] and a["source"] == b["source"]:
        score += 0.30
    elif a["source"] and b["source"]:
        score -= 0.05
    if a["codec"] and a["codec"] == b["codec"]:
        score += 0.15
    elif a["codec"] and b["codec"]:
        # Different codec of the same source (YTS x264 vs x265) is usually still in sync,
        # so this is a ranking nudge, not a rejection.
        score -= 0.10
    if a["group"] and a["group"] == b["group"]:
        score += 0.35
    overlap = a["tokens"] & b["tokens"]
    score += min(len(overlap), 6) / 6 * 0.20
    # 1.0 is reserved for an exact release-name match above, so a near match can never tie it.
    return max(0.0, min(0.95, score))


def is_arabic(sub: dict) -> bool:
    lang = str(sub.get("language") or "").strip().lower()
    name = str(sub.get("lang") or "").strip().lower()
    return lang in {"ar", "ara", "arabic", "ar_sa"} or name in {"arabic", "ar"}


def decode_subtitle(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1256", "iso-8859-6", "utf-16"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if enc != "utf-8" or "\ufffd" not in text:
            return text
    return raw.decode("utf-8", errors="replace")


def fetch_arabic_subtitle(api_key: str, tmdb_id: int, video_name: str,
                          folder: str) -> tuple[str | None, float, str | None]:
    """Returns (srt_text, score, release_name) or (None, score, None)."""
    params = {
        "api_key": api_key,
        "tmdb_id": tmdb_id,
        "languages": "AR",
        "type": "movie",
        "subs_per_page": 30,
    }
    data = http_get(SUBDL_API, pacer=subdl_pacer, params=params).json()
    if not data.get("status", False):
        raise SafeHTTPError(f"SubDL returned status=false ({data.get('error', 'no detail')})")

    subs = [s for s in data.get("subtitles", []) if is_arabic(s)]
    if not subs:
        log("SubDL: no Arabic subtitles listed for this title", folder=folder)
        return None, 0.0, None

    scored = sorted(
        ((subtitle_score(s.get("release_name") or s.get("name") or "", video_name), s)
         for s in subs),
        key=lambda t: t[0],
        reverse=True,
    )
    best_score, best = scored[0]
    release = best.get("release_name") or best.get("name") or "?"
    if best_score < SUBTITLE_ACCEPT:
        log(f"SubDL: best Arabic match '{release}' scored {best_score:.2f} < "
            f"{SUBTITLE_ACCEPT:.2f} - refusing to download a mismatched subtitle",
            folder=folder)
        return None, best_score, None

    url = best.get("url") or ""
    if not url:
        return None, best_score, None
    resp = http_get(SUBDL_DL + url if url.startswith("/") else url,
                    pacer=subdl_pacer, timeout=60)

    payload = resp.content
    if payload[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith((".srt", ".ass", ".sub"))]
            srts = [n for n in names if n.lower().endswith(".srt")] or names
            if not srts:
                raise SafeHTTPError("SubDL archive contained no subtitle file")
            srts.sort(key=lambda n: zf.getinfo(n).file_size, reverse=True)
            payload = zf.read(srts[0])
    return decode_subtitle(payload), best_score, release


# --------------------------------------------------------------------------------------
# Processing pipeline
# --------------------------------------------------------------------------------------


def process_movie(state: State, name: str, tmdb_key: str, subdl_key: str | None) -> None:
    folder = LIBRARY_ROOT / name
    entry = state.movies[name]
    entry["status"] = "processing"
    entry["last_attempt"] = now_iso()
    entry["errors"] = []
    warnings = False

    video = find_video(folder)
    if video is None:
        entry["status"] = "needs_manual_review"
        entry["errors"].append("no video file found in folder")
        log("No video file found - marked needs_manual_review", folder=name)
        state.save()
        return

    title, year, source = parse_title_year(video.name, name)
    log(f"Parsed title '{title}'" + (f" ({year})" if year else " (no year)") +
        f" from {source}", folder=name)

    # ---- TMDB --------------------------------------------------------------------
    try:
        match = tmdb_resolve(tmdb_key, title, year, name)
    except SafeHTTPError as exc:
        entry["status"] = "error"
        entry["errors"].append(f"tmdb: {exc}")
        log(f"TMDB request failed: {exc} - will retry later", folder=name)
        state.save()
        return

    if match is None:
        entry["status"] = "needs_manual_review"
        entry["poster_status"] = entry["ico_status"] = entry["icon_status"] = "skipped_no_match"
        entry["subtitle_status"] = "skipped_no_match"
        entry["errors"].append("no reliable TMDB match")
        log("No reliable TMDB match - folder left untouched, needs manual review", folder=name)
        state.save()
        return

    entry.update({
        "tmdb_id": match["id"],
        "title": match["title"],
        "year": match["year"],
        "match_method": match["match_method"],
        "match_score": match["match_score"],
    })
    log(f"TMDB match: {match['title']} ({match['year']}), id={match['id']} "
        f"[{match['match_method']}, score {match['match_score']}]", folder=name)
    state.save()

    # ---- poster (memory only) / ico / icon ----------------------------------------
    ico = folder / ICO_NAME
    poster_img: Image.Image | None = None
    if match["poster_path"]:
        try:
            poster_img = fetch_poster(match["poster_path"])
            entry["poster_status"] = "ok"
            log(f"Poster fetched into memory ({poster_img.width}x{poster_img.height}) "
                f"- no image file written", folder=name)
        except (SafeHTTPError, OSError, ValueError) as exc:
            warnings = True
            entry["poster_status"] = "failed"
            entry["errors"].append(f"poster: {exc}")
            log(f"Poster download failed: {exc}", folder=name)
    else:
        warnings = True
        entry["poster_status"] = "unavailable"
        log("TMDB has no poster for this title", folder=name)

    if poster_img is not None:
        try:
            poster_to_ico(poster_img, ico)
            entry["ico_status"] = "ok"
            log(f"ICO generated ({len(ICO_SIZES)} sizes) -> {ICO_NAME}", folder=name)
        except (OSError, ValueError) as exc:
            warnings = True
            entry["ico_status"] = "failed"
            entry["errors"].append(f"ico: {exc}")
            log(f"ICO conversion failed: {exc}", folder=name)
        finally:
            poster_img.close()
            poster_img = None

    if entry["ico_status"] == "ok":
        try:
            apply_folder_icon(folder, ico)
            entry["icon_status"] = "ok"
            log("Folder icon configured (desktop.ini)", folder=name)
        except OSError as exc:
            warnings = True
            entry["icon_status"] = "failed"
            entry["errors"].append(f"icon: {exc}")
            log(f"Folder icon configuration failed: {exc}", folder=name)
    state.save()

    # ---- Arabic subtitle ----------------------------------------------------------
    # Built from the exact filename observed on disk, with only the video extension
    # removed - never re-parsed, so bracketed/dotted release names survive intact.
    target = video.with_name(strip_video_ext(video.name) + SUBTITLE_SUFFIX)
    if target.exists():
        entry["subtitle_status"] = "already_present"
        entry["subtitle_file"] = target.name
        log("Arabic subtitle already present - skipping", folder=name)
    elif not subdl_key:
        warnings = True
        entry["subtitle_status"] = "missing_credential"
        entry["errors"].append("SUBDL_API_KEY not set")
        log("SUBDL_API_KEY is not set - skipping subtitle. Set it and use --retry.", folder=name)
    else:
        log("Searching Arabic subtitles...", folder=name)
        try:
            text, score, release = fetch_arabic_subtitle(subdl_key, match["id"], video.name, name)
            if text is None:
                warnings = True
                entry["subtitle_status"] = "not_found"
            else:
                target.write_text(text, encoding="utf-8", newline="")
                entry["subtitle_status"] = "ok"
                entry["subtitle_file"] = target.name
                entry["subtitle_match_score"] = round(score, 3)
                entry["subtitle_release"] = release
                log(f"Arabic subtitle saved as {target.name} "
                    f"(release '{release}', score {score:.2f})", folder=name)
        except (SafeHTTPError, OSError, ValueError, zipfile.BadZipFile) as exc:
            warnings = True
            entry["subtitle_status"] = "failed"
            entry["errors"].append(f"subtitle: {exc}")
            log(f"Subtitle lookup failed: {exc}", folder=name)

    entry["status"] = "completed_with_warnings" if warnings else "completed"
    entry["completed_at"] = now_iso()
    state.save()
    log("Completed with warnings." if warnings else "Completed successfully.", folder=name)


# --------------------------------------------------------------------------------------
# Watch loop
# --------------------------------------------------------------------------------------

TERMINAL_STATES = {
    "completed", "completed_with_warnings", "needs_manual_review",
    "existing_at_initialization", "vanished",
}


class Watcher:
    def __init__(self, state: State, tmdb_key: str, subdl_key: str | None) -> None:
        self.state = state
        self.tmdb_key = tmdb_key
        self.subdl_key = subdl_key
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.snapshots: dict[str, tuple] = {}
        self.stable_counts: dict[str, int] = {}
        self.first_seen: dict[str, float] = {}
        # Movie Picker auto-update bookkeeping. Deliberately in-memory only (not
        # persisted to state.json): if the watcher restarts mid-debounce, a pending
        # rebuild is dropped rather than risking a stale/duplicated trigger. It will
        # fire again the next time a genuinely new movie completes.
        self.picker_dirty = False
        self.picker_dirty_since: float | None = None
        self.picker_next_retry_at: float | None = None
        self.picker_lock = SingleInstance(PICKER_LOCK_FILE)

    # -- discovery ---------------------------------------------------------------------
    def discover(self) -> None:
        for name in list_top_level_dirs():
            entry = self.state.movies.get(name)
            if entry is not None:
                # A folder that had vanished is back. Baseline entries are never touched
                # here, so this can only ever revive a folder that was already new.
                if entry.get("status") == "vanished":
                    self.state.add_new(name)
                    self.reset_tracking(name)
                    log(f"Previously vanished folder reappeared, treating as new: "
                        f"{LIBRARY_ROOT / name}")
                    log("Waiting for file stability...", folder=name)
                continue
            self.state.add_new(name)
            self.reset_tracking(name)
            log(f"New folder detected: {LIBRARY_ROOT / name}")
            log("Waiting for file stability...", folder=name)
        self.state.save()

    def reset_tracking(self, name: str) -> None:
        """Forget any stability progress remembered for this folder name."""
        self.first_seen[name] = time.time()
        self.snapshots.pop(name, None)
        self.stable_counts.pop(name, None)

    def mark_vanished(self, name: str) -> None:
        """A folder we were tracking is no longer on disk. Purely local bookkeeping -
        no network calls, and not an error condition for the watcher."""
        entry = self.state.movies[name]
        entry["status"] = "vanished"
        entry["vanished_at"] = now_iso()
        self.snapshots.pop(name, None)
        self.stable_counts.pop(name, None)
        self.first_seen.pop(name, None)
        self.state.save()
        log("Folder is no longer on disk - marked 'vanished', no longer being checked. "
            "It will be treated as new if it reappears.", folder=name)

    # -- stability ---------------------------------------------------------------------
    def check_stability(self, name: str) -> bool:
        folder = LIBRARY_ROOT / name
        if not folder.is_dir():
            return False
        age = time.time() - self.first_seen.get(name, time.time())
        if age < MIN_FOLDER_AGE:
            return False

        snap = folder_snapshot(folder)
        if snap is None:
            return False
        files, has_partial = snap
        if has_partial:
            self.stable_counts[name] = 0
            self.snapshots[name] = snap
            return False
        if find_video(folder) is None:
            self.stable_counts[name] = 0
            self.snapshots[name] = snap
            return False

        if self.snapshots.get(name) == snap:
            self.stable_counts[name] = self.stable_counts.get(name, 0) + 1
        else:
            self.stable_counts[name] = 1
        self.snapshots[name] = snap

        if self.stable_counts[name] >= STABLE_SCANS_REQUIRED:
            log("Video file is stable.", folder=name)
            return True

        if age > STABILITY_TIMEOUT:
            entry = self.state.movies[name]
            entry["status"] = "needs_manual_review"
            entry["errors"] = ["folder never became stable within timeout"]
            self.state.save()
            log("Folder never stabilised - marked needs_manual_review", folder=name)
        return False

    # -- one pass ----------------------------------------------------------------------
    def cycle(self) -> None:
        self.discover()
        for name, entry in list(self.state.movies.items()):
            if entry.get("status") in TERMINAL_STATES:
                continue
            # Baseline entries are terminal and already skipped above, so this can never
            # retire a pre-existing movie folder.
            if not (LIBRARY_ROOT / name).is_dir():
                self.mark_vanished(name)
                continue
            self.first_seen.setdefault(name, time.time())
            if not self.check_stability(name):
                continue
            try:
                process_movie(self.state, name, self.tmdb_key, self.subdl_key)
            except Exception as exc:  # never let one movie kill the watcher
                entry["status"] = "error"
                entry.setdefault("errors", []).append(f"unexpected: {type(exc).__name__}: {exc}")
                self.state.save()
                log(f"Unexpected error: {type(exc).__name__}: {exc}", folder=name)
            else:
                # A folder only ever reaches cycle()'s process_movie() call once - it is
                # terminal (skipped above) on every subsequent cycle - so this can only
                # fire once per genuinely new movie. Baseline, vanished, needs_manual_review
                # and error outcomes never take this branch.
                if entry.get("status") in ("completed", "completed_with_warnings"):
                    if not self.picker_dirty:
                        self.picker_dirty_since = time.time()
                    self.picker_dirty = True
        self.maybe_rebuild_picker()

    # -- Movie Picker auto-update --------------------------------------------------------
    def maybe_rebuild_picker(self) -> None:
        """Rebuild the Movie Picker dashboard after new movie(s) finish, coalesced and
        failure-isolated. Runs strictly after this cycle's movies are already saved as
        completed, so a dashboard failure can never affect a movie's own status."""
        if not self.picker_dirty:
            return
        now = time.time()
        if now - (self.picker_dirty_since or now) < PICKER_DEBOUNCE_SECONDS:
            return  # still coalescing - more movies may finish in this window
        if self.picker_next_retry_at and now < self.picker_next_retry_at:
            return  # backing off after a recent failure

        if not PICKER_SCRIPT.exists():
            log(f"Movie Picker rebuild skipped: {PICKER_SCRIPT} not found.")
            self.picker_dirty = False
            self.picker_dirty_since = None
            return

        if not self.picker_lock.acquire():
            # Already running (or a manual run holds the lock) - stay dirty and retry
            # on a later cycle rather than stacking a second concurrent rebuild.
            return
        try:
            log("Movie Picker: new movie(s) processed - updating dashboard...")
            state = load_picker_state()
            state["last_attempt"] = now_iso()
            save_picker_state(state)

            try:
                # self.tmdb_key was already resolved with the registry fallback in
                # main() - os.environ itself may not carry it (a shell started before
                # the variable existed won't have inherited it), so it must be injected
                # explicitly rather than relying on subprocess.run's default inheritance.
                picker_env = dict(os.environ)
                picker_env["TMDB_API_KEY"] = self.tmdb_key
                result = subprocess.run(
                    [sys.executable, str(PICKER_SCRIPT)],
                    cwd=str(LIBRARY_ROOT),
                    capture_output=True,
                    text=True,
                    timeout=PICKER_TIMEOUT_SECONDS,
                    env=picker_env,
                )
            except subprocess.TimeoutExpired:
                self._picker_failed(f"timed out after {PICKER_TIMEOUT_SECONDS}s")
                return
            except OSError as exc:
                self._picker_failed(f"failed to launch: {exc}")
                return

            try:
                PICKER_RUN_LOG_FILE.write_text(
                    redact((result.stdout or "") + "\n" + (result.stderr or "")),
                    encoding="utf-8",
                )
            except OSError:
                pass

            if result.returncode != 0:
                tail = redact((result.stderr or result.stdout or "").strip())[-500:]
                self._picker_failed(f"exit code {result.returncode}: {tail}")
                return

            self.picker_dirty = False
            self.picker_dirty_since = None
            self.picker_next_retry_at = None
            state = load_picker_state()
            state.update(status="ok", last_success=now_iso(), last_error=None)
            save_picker_state(state)
            log("Movie Picker: dashboard updated (movie-picker.json / movie-picker.html). "
                f"Details: {PICKER_RUN_LOG_FILE}")
        finally:
            self.picker_lock.release()

    def _picker_failed(self, detail: str) -> None:
        log(f"Movie Picker update failed: {detail}. The movie itself is still marked "
            f"completed; the dashboard will retry automatically in "
            f"{PICKER_RETRY_BACKOFF_SECONDS // 60} minutes.")
        state = load_picker_state()
        state.update(status="failed", last_error=detail, last_error_at=now_iso())
        save_picker_state(state)
        self.picker_next_retry_at = time.time() + PICKER_RETRY_BACKOFF_SECONDS
        # picker_dirty stays True so it is retried after the backoff.

    # -- run ---------------------------------------------------------------------------
    def run(self) -> None:
        observer = self.start_observer()
        log(f"Watching {LIBRARY_ROOT} for new top-level folders. Press Ctrl+C to stop.")
        log(f"{sum(1 for m in self.state.movies.values() if m['status'] == 'existing_at_initialization')} "
            f"baseline folders will never be touched.")
        try:
            while not self.stop.is_set():
                if STOP_FILE.exists():
                    STOP_FILE.unlink(missing_ok=True)
                    log("Stop requested via --stop - shutting down.")
                    break
                self.cycle()
                self.wake.wait(SCAN_INTERVAL)
                self.wake.clear()
        except KeyboardInterrupt:
            log("Stop requested - shutting down.")
        finally:
            if observer:
                observer.stop()
                observer.join(timeout=5)
            self.state.save()
            log("Watcher stopped. State saved.")

    def start_observer(self):
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log("watchdog not installed - falling back to polling only.")
            return None

        wake = self.wake

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if event.is_directory:
                    wake.set()

            def on_moved(self, event):
                if event.is_directory:
                    wake.set()

        observer = Observer()
        observer.schedule(Handler(), str(LIBRARY_ROOT), recursive=False)
        observer.start()
        return observer


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def cmd_initialize(state: State, *, quiet: bool = False) -> None:
    dirs = list_top_level_dirs()
    state.record_baseline(dirs)
    state.save()
    if not quiet:
        log(f"Baseline established: {len(dirs)} existing top-level folders recorded as "
            f"'existing_at_initialization'. None of them were touched.")
        log(f"State: {STATE_FILE}")


def cmd_status(state: State) -> None:
    counts: dict[str, int] = {}
    for entry in state.movies.values():
        counts[entry.get("status", "?")] = counts.get(entry.get("status", "?"), 0) + 1
    print(f"Library    : {LIBRARY_ROOT}")
    print(f"State      : {STATE_FILE}")
    print(f"Initialized: {state.data.get('initialized_at')}")

    picker = load_picker_state()
    if picker.get("status") is not None:
        print(f"Movie Picker: {picker.get('status')}"
              + (f" - last success {picker.get('last_success')}" if picker.get("last_success") else "")
              + (f" - last error: {picker.get('last_error')}" if picker.get("status") == "failed" else ""))

    print("Status counts:")
    for status, count in sorted(counts.items()):
        print(f"  {status:<30} {count}")
    interesting = [e for e in state.movies.values()
                   if e.get("status") not in ("existing_at_initialization", "completed")]
    if interesting:
        print("\nMovies needing attention:")
        for e in interesting:
            print(f"  [{e.get('status')}] {e['folder_name']}"
                  + (f" - subtitle: {e.get('subtitle_status')}" if e.get("subtitle_status") else "")
                  + (f" - {'; '.join(e.get('errors') or [])}" if e.get("errors") else ""))


def cmd_retry(state: State, target: str, tmdb_key: str, subdl_key: str | None) -> int:
    path = Path(target).resolve()
    if path.parent != LIBRARY_ROOT.resolve():
        log(f"ERROR: {path} is not a top-level folder of {LIBRARY_ROOT}.")
        return 2
    if not path.is_dir():
        log(f"ERROR: {path} does not exist.")
        return 2
    name = path.name
    if state.is_baseline(name):
        log(f"REFUSED: '{name}' existed at initialization. Pre-existing movie folders are "
            f"never processed. Nothing was changed.")
        return 3
    if not state.known(name):
        log(f"'{name}' is not in state yet - registering it as a new movie.")
        state.add_new(name)
    state.movies[name]["status"] = "pending"
    state.save()
    log(f"Retrying {name}...")
    process_movie(state, name, tmdb_key, subdl_key)
    return 0


def cmd_stop() -> int:
    """Ask a running watcher to shut down cleanly. Works with pythonw.exe, where there is
    no console to Ctrl+C."""
    pid = running_watcher_pid()
    if pid is None:
        log("No watcher is running.")
        STOP_FILE.unlink(missing_ok=True)
        return 1
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STOP_FILE.write_text(now_iso(), encoding="utf-8")
    log(f"Stop requested (pid {pid or 'unknown'}); waiting for it to finish the current step...")
    for _ in range(60):
        if running_watcher_pid() is None:
            log("Watcher stopped.")
            return 0
        time.sleep(1)
    log("Watcher has not exited yet - it may be mid-download. It will stop after the current "
        "movie; check watcher.log.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch D:\\Movies for new movie folders.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--initialize", action="store_true",
                       help="establish the baseline only; process nothing")
    group.add_argument("--status", action="store_true", help="print state summary and exit")
    group.add_argument("--once", action="store_true",
                       help="run a single discover/process pass and exit")
    group.add_argument("--retry", metavar="FOLDER",
                       help="re-process one specific new movie folder")
    group.add_argument("--stop", action="store_true",
                       help="ask the running watcher to shut down cleanly")
    args = parser.parse_args()

    if args.stop:
        return cmd_stop()

    if not LIBRARY_ROOT.is_dir():
        log(f"ERROR: library root {LIBRARY_ROOT} not found.")
        return 2

    state = State()
    had_state = state.load()

    if args.status:
        if not had_state:
            log("No state yet. Run --initialize first.")
            return 1
        cmd_status(state)
        return 0

    if args.initialize:
        if had_state:
            log(f"Baseline already exists ({state.data.get('initialized_at')}) with "
                f"{len(state.movies)} recorded folders. Nothing changed.")
            return 0
        cmd_initialize(state)
        return 0

    if not had_state:
        log("No baseline found - establishing one now. Existing folders will NOT be processed.")
        cmd_initialize(state)
        if args.retry:
            log("Baseline just created; the retry target is now part of the baseline.")

    tmdb_key = _read_env("TMDB_API_KEY")
    if not tmdb_key:
        log("ERROR: TMDB_API_KEY is not set. Set it as a user environment variable and "
            "restart. No movie was processed.")
        return 2
    subdl_key = _read_env("SUBDL_API_KEY")
    if not subdl_key:
        log("NOTE: SUBDL_API_KEY is not set - posters and icons will work, Arabic subtitles "
            "will be skipped and recorded as 'missing_credential'.")

    if args.retry:
        # A retry while the watcher is live would have both processes writing the same
        # folder and the same state entry, so refuse rather than corrupt anything.
        running = running_watcher_pid()
        if running is not None:
            log(f"REFUSED: a watcher is already running (pid {running or 'unknown'}). "
                f"Run --stop first, then --retry, then start it again. Nothing was changed.")
            return 4
        return cmd_retry(state, args.retry, tmdb_key, subdl_key)

    lock = SingleInstance(LOCK_FILE)
    if not lock.acquire():
        other = running_watcher_pid()
        log(f"Another watcher is already running (pid {other or 'unknown'}) - this instance "
            f"is exiting so the two cannot race on the same library.")
        return 4

    STOP_FILE.unlink(missing_ok=True)   # never inherit a stale request from a past run
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        watcher = Watcher(state, tmdb_key, subdl_key)
        if args.once:
            watcher.cycle()
            log("Single pass complete.")
            return 0
        watcher.run()
        return 0
    finally:
        PID_FILE.unlink(missing_ok=True)
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
