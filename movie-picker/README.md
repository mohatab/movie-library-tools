# Movie Picker

Turns a folder-per-movie library into a self-contained, offline movie-picker dashboard.

- **Input**: the folder structure of the library root — one directory per film. Title
  comes from the folder name; year comes from the scene-release video filename inside it.
- **TMDB**: all metadata except the rating (poster, overview, genres, etc.).
- **Letterboxd**: average rating + rating count only, matched via
  `letterboxd.com/tmdb/<id>/` (which redirects to the correct film page — far more
  reliable than constructing slugs from the title).
- **Output**: `movie-picker.json` (the resolved dataset) and `movie-picker.html` (a
  self-contained dashboard with the data inlined — no server, no build step, no
  dependencies to view it). Both are regenerated on every run and are not checked into
  this repository.
- **Caching**: `cache/letterboxd/_index.json` maps TMDB id → resolved Letterboxd slug so
  reruns make zero Letterboxd requests for films already resolved. `cache/posters/` holds
  downloaded poster images so the page works offline.

## Important: where this script must live

`MOVIES_ROOT` is set to the directory the script itself is in
(`os.path.dirname(os.path.abspath(__file__))`) — it scans wherever it's placed. This repo
keeps it under `movie-picker/` for organization, but **to actually run it, copy
`build_movie_picker.py` into the root of your movie library** (the folder containing your
movie subfolders), then run it from there.

## Requirements

```powershell
python -m pip install -r requirements.txt
```

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `TMDB_API_KEY` | yes | Read from the environment only — never hardcoded, logged, or written to `movie-picker.json`/`movie-picker.html`/cache. |

## Usage

Run from your movie library root (see above):

```powershell
python build_movie_picker.py
```

Rebuild only the HTML from the existing `movie-picker.json` — zero network requests, no
API key needed. Useful for restyling the dashboard without re-fetching anything:

```powershell
python build_movie_picker.py --render-only
```

## What it excludes automatically

`#Done`, `#Donre`, `#Movies Trilogy`, `cache`, `posters`, `__pycache__`, and any directory
starting with `.` are skipped as scan input.
