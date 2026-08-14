# Movie Library Tools

Personal archive of two independent tools for managing a folder-per-movie Windows library.
This repository holds the **tools**, not the library itself — no movie files, subtitles,
posters, generated dashboards, or watcher state are checked in.

- **[Movie Picker](movie-picker/)** — scans the library and builds an offline, self-contained
  browsable dashboard (search/filter/sort) with TMDB metadata and Letterboxd ratings.
- **[Movie Watcher](movie-watcher/)** — watches the library folder for newly added movies and
  automatically sets a poster-based folder icon and finds an Arabic subtitle, without ever
  touching movies that existed before it was started. After new movie(s) finish, it also
  triggers a Movie Picker rebuild so the dashboard stays current automatically.

The two tools' matching/rendering logic stays fully separate — the watcher runs the picker as
an ordinary subprocess, never importing or duplicating its TMDB/Letterboxd/HTML code.

## Movie Picker

Reads the movie library, resolves each film against TMDB for metadata and Letterboxd for
ratings, and generates a local dashboard with sorting, filtering, and search. Uses a
persistent cache so reruns don't re-fetch Letterboxd data or posters already resolved.

```powershell
python movie-picker/build_movie_picker.py
python movie-picker/build_movie_picker.py --render-only   # rebuild HTML only, no network
```

`build_movie_picker.py` must be copied into the root of your movie library to run — see
[movie-picker/README.md](movie-picker/README.md) for why and for full details.

## Movie Watcher

Watches a movie library folder for new top-level folders and prepares each one
automatically:

- Establishes a **baseline** of existing folders on first run and never modifies them —
  only folders created *after* the baseline is eligible for automation.
- Waits for a new folder's video file to finish copying/downloading before touching it.
- Resolves the movie against TMDB (title/year parsed from the release filename).
- Downloads the TMDB poster **into memory only** and converts it directly to a
  multi-resolution Windows `.ico` — no poster image file is ever written to the movie
  folder.
- Configures the folder icon via `desktop.ini`.
- Finds and downloads an Arabic subtitle through the configured provider, matched against
  the actual release (resolution/source/codec/group), never just the first result.
- Never renames or modifies the video file, and never renames the movie folder.
- Persists state so it survives restarts and never reprocesses a folder.
- Single-instance locked: a second launch refuses to run rather than race the first.
- Triggers a coalesced, failure-isolated Movie Picker rebuild after new movie(s) complete —
  see [movie-watcher/README.md](movie-watcher/README.md#movie-picker-auto-update).

```powershell
python movie-watcher/movie_library_watcher.py                 # continuous watch
python movie-watcher/movie_library_watcher.py --initialize    # baseline only, processes nothing
python movie-watcher/movie_library_watcher.py --status
python movie-watcher/movie_library_watcher.py --stop
python movie-watcher/movie_library_watcher.py --retry "D:\Movies\Movie Name"
```

See [movie-watcher/README.md](movie-watcher/README.md) for full details, including how to
enable it at Windows logon.

### Safety guarantee

**The watcher never retroactively processes existing movies.** A folder is eligible for
automation only if it did not exist at the time `--initialize` (or the first run) recorded
the baseline. A folder missing a poster or subtitle is never treated as "new" — the trigger
is folder creation time, not folder completeness.

## Environment variables

Both tools read API credentials from the environment only. Neither ever hardcodes,
prints, or writes a key to any output file, cache entry, or log line.

| Variable | Used by | Required |
|---|---|---|
| `TMDB_API_KEY` | Picker, Watcher | Yes — both refuse to run without it. |
| `SUBDL_API_KEY` | Watcher | Only for Arabic subtitles. Without it, poster/icon still work; subtitles are skipped and recorded as `missing_credential`. |

Set them as user environment variables before running either tool:

```powershell
[Environment]::SetEnvironmentVariable('TMDB_API_KEY', '<your key>', 'User')
[Environment]::SetEnvironmentVariable('SUBDL_API_KEY', '<your key>', 'User')
```

Open a new terminal afterward so the process picks up the new value.

## Restoring on a new machine

```powershell
git clone https://github.com/mohatab/movie-library-tools.git
cd movie-library-tools
python -m pip install -r movie-picker/requirements.txt
python -m pip install -r movie-watcher/requirements.txt
```

Then:
1. Copy `movie-picker/build_movie_picker.py` into the root of your movie library (it scans
   whatever folder it's placed in — and the watcher expects to find it right there, next to
   itself, in order to auto-trigger dashboard rebuilds).
2. If your library root isn't `D:\Movies`, edit the `LIBRARY_ROOT` constant near the top of
   `movie-watcher/movie_library_watcher.py` to match.
3. Set `TMDB_API_KEY` (and `SUBDL_API_KEY` for subtitles) as described above.
4. Run `movie_library_watcher.py --initialize` **before** adding any new movies, so your
   existing library is correctly recorded as the baseline and never gets processed.
