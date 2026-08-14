# Movie Library Watcher

Watches `D:\Movies` for **new** top-level movie folders and prepares them automatically:
TMDB match → poster → multi-size `.ico` → Windows folder icon → Arabic subtitle.

**Existing movie folders are never touched.** The 96 folders that were present when the
baseline was established on 2026-08-14 are recorded as `existing_at_initialization` and are
permanently excluded. Only folders that appear *after* that point are eligible.

---

## Install

```powershell
python -m pip install Pillow watchdog requests
```

Already installed on this machine: Pillow 12.3.0, watchdog, requests 2.31.0 (Python 3.13).

## Environment variables

| Variable | Required | Effect if missing |
|---|---|---|
| `TMDB_API_KEY` | yes | Watcher refuses to process anything and exits with a clear error. Already set (User scope). |
| `SUBDL_API_KEY` | for subtitles only | Poster + icon still work; subtitle recorded as `missing_credential`. Already set (User scope). |

Read from the environment only — never hardcoded, logged, or written to any file.
Get a SubDL key at <https://subdl.com> (Profile → API), then:

```powershell
[Environment]::SetEnvironmentVariable('SUBDL_API_KEY','<your-key>','User')
```

Open a new terminal afterwards, then re-run failed movies with `--retry`.

---

## Commands

```powershell
python D:\Movies\movie_library_watcher.py                 # continuous watch (normal use)
python D:\Movies\movie_library_watcher.py --initialize    # baseline only, processes nothing
python D:\Movies\movie_library_watcher.py --status        # summary of what needs attention
python D:\Movies\movie_library_watcher.py --once          # single discover/process pass
python D:\Movies\movie_library_watcher.py --retry "D:\Movies\Movie Name"
python D:\Movies\movie_library_watcher.py --stop         # shut the running watcher down
```

There is deliberately **no** command that processes the whole library.
`--retry` on a baseline folder is refused (exit code 3) and changes nothing.

**Only one watcher may run at a time.** It holds an OS lock on `.movie-watcher\watcher.lock`
for its lifetime; a second launch exits immediately with exit code 4 rather than racing on
`state.json` and double-downloading. Because it is an OS lock, a killed watcher never leaves a
stale lock behind. `--retry` is refused for the same reason while a watcher is live — the
sequence is `--stop`, then `--retry`, then start it again.

**Stopping:** `--stop` (works with `pythonw.exe`, where there is no console to Ctrl+C) writes a
stop request, and the watcher exits after finishing its current step. Ctrl+C also works if you
started it in a visible console.

---

## Start automatically with Windows — ENABLED

A Startup shortcut is installed at:

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Movie Library Watcher.lnk
   Target : C:\Program Files\Python313\pythonw.exe
   Args   : "D:\Movies\movie_library_watcher.py"
   Startin: D:\Movies
```

It launches the watcher at every logon, with no console window. **To disable autostart, delete
that one file** — nothing else was changed (no registry keys, no services, no scheduled tasks).

**Alternative: Task Scheduler (survives without login, auto-restarts)**

Create Task → Trigger: *At log on* → Action: Start a program
`C:\Program Files\Python313\pythonw.exe`, arguments `"D:\Movies\movie_library_watcher.py"`,
Start in `D:\Movies`. Settings → *Restart the task if it fails*.

With `pythonw.exe` there is no window; read progress in `.movie-watcher\watcher.log`.

---

## What it creates inside a NEW movie folder

| File | Purpose |
|---|---|
| `folder.ico` | 16/24/32/48/64/128/256 px icon, hidden |
| `desktop.ini` | `IconResource=folder.ico,0`, hidden+system |
| `<video name>.ar.srt` | Arabic subtitle, UTF-8 |

The folder gets the `ReadOnly` attribute — that is what makes Explorer honour `desktop.ini`.
The video file is never renamed, moved, or modified, and the folder is never renamed.

**No poster image is ever written.** The TMDB poster is downloaded into memory and converted
straight to `folder.ico`; there is no `poster.jpg`, no temp file, and nothing to clean up.
The subtitle filename is built from the exact video filename with only its recognised video
extension removed, so dotted/bracketed release names are never truncated.

A tracked folder that disappears from disk is marked `vanished` (local check only, no network)
and stops being polled. If it reappears it is treated as a brand-new folder. Baseline folders
are never eligible for this — they are terminal and never checked at all.

## State

`D:\Movies\.movie-watcher\state.json` — one record per folder (status, TMDB id, title, year,
match method + score, poster/ico/icon/subtitle status, errors, timestamps).
`D:\Movies\.movie-watcher\watcher.log` — rotating log. Nothing is stored inside movie folders.

> Do not hand-edit `state.json` with PowerShell's `Get-Content`/`Out-File` — those default to
> ANSI on read and add a BOM on write, which mangles non-ASCII folder names (e.g. *Véronique*)
> and would make an existing folder look new. Use a UTF-8-aware editor, or just delete the file
> and re-run `--initialize` if no new movies are pending.

The `.movie-watcher` directory starts with a dot, so `build_movie_picker.py` skips it.

## Movie Picker auto-update

After genuinely new movie(s) finish processing, the watcher runs `build_movie_picker.py`
(expected next to it, in the library root) as an unmodified subprocess — the two tools'
logic stays fully separate, the watcher never imports or duplicates any TMDB/Letterboxd/HTML
code.

* **Coalesced**: waits 90 seconds of quiet after the last new-movie completion before
  rebuilding, so several movies added close together share one rebuild instead of one each.
* **Isolated from movie processing**: runs only after a movie's own status is already saved
  as `completed`/`completed_with_warnings`. A rebuild failure is logged and recorded in
  `.movie-watcher\picker_state.json` (separate from `state.json`) and retried automatically
  after a 5-minute backoff — it never changes a movie's own status.
* **Never touches baseline, `needs_manual_review`, `error`, or `vanished` movies.** `--retry`
  never triggers a rebuild either — it's a manual, single-movie operation.
* **Single-flight**: guarded by its own `.movie-watcher\picker.lock`, independent of the
  watcher's own instance lock, so a slow rebuild can't overlap a second one.
* **No infinite loop**: the picker only ever writes files/subdirectories at the library root
  (`movie-picker.json`, `movie-picker.html`, `cache/`) — `cache` is already excluded from
  new-folder detection (see above), so the picker's own output can never be seen as a new
  movie.
* Progress/output from each rebuild is written to `.movie-watcher\picker_last_run.log`
  (overwritten each run, secrets redacted) so a failure can be diagnosed without bloating
  `watcher.log`.

**Known limitation:** the pending-rebuild flag is in-memory only, not persisted to disk. If
the watcher restarts within the 90-second debounce window after a movie completes, that
pending rebuild is dropped — it isn't lost forever, the next new movie triggers a fresh one,
but the dashboard can lag by one movie until then.

## How new folders are detected

watchdog fires on directory creation, plus a 10-second polling sweep as a safety net (so
folders created while the watcher was off are still caught on next start). A folder is *new*
iff its name is not already in `state.json` — never because it is missing a poster or subtitle.

Ignored: root-level files, `#Done`, `#Donre`, `.movie-watcher`, `cache`, `__pycache__`, and
anything starting with `.`, `#`, `$`, or `~`.

## Waiting for the copy to finish

Before processing, a folder must: be ≥20 s old, contain a video ≥20 MB, contain no
`.part/.crdownload/.!ut/.tmp/...` file, and present a byte-identical file listing for 3
consecutive 10-second scans. If it never settles within 6 hours it is flagged for review.

## Refusal rules (a wrong result is worse than none)

* TMDB score below threshold (0.82 with a year, 0.92 without), or the top two candidates
  within 0.05 of each other → `needs_manual_review`, **folder left completely untouched**.
* Arabic subtitle match below 0.50 → nothing downloaded, `subtitle_status = not_found`,
  poster and icon are still kept.
* Only subtitles SubDL reports as Arabic are ever considered.

Subtitles are scored against the actual release: exact release-name match = 1.0, otherwise
resolution + source + codec + release group + token overlap.
