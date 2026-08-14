#!/usr/bin/env python3
"""
build_movie_picker.py — turn D:\\Movies into a self-contained movie-picker dashboard.

Input  : the folder structure of MOVIES_ROOT (one directory per film).
           - title comes from the folder name
           - year  comes from the scene-release video filename inside it
Sources: TMDB  -> all metadata except the rating
         Letterboxd -> average rating + rating count ONLY

Outputs: movie-picker.json   complete resolved dataset (presentation-independent)
         movie-picker.html   self-contained dashboard (data inlined, no server, no deps)
         cache/letterboxd/   per-slug cache so reruns don't refetch

The TMDB API key is read ONLY from the TMDB_API_KEY environment variable and is
never written to any output file, cache entry, or log line.

Rerun with:  python build_movie_picker.py
"""

import os
import re
import sys
import json
import time
import html
import unicodedata
import datetime as dt
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
except ImportError:
    sys.exit("This script needs 'requests'.  Install with:  python -m pip install requests")

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

MOVIES_ROOT = os.path.dirname(os.path.abspath(__file__))
# "#Movies Trilogy" holds franchise sub-folders, not a single film — it is walked
# separately by scan_franchise_dir so each video inside becomes its own record.
EXCLUDE_DIRS = {"#Done", "#Donre", "#Movies Trilogy", "cache", "posters", "__pycache__"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".m4v", ".mov", ".wmv"}

OUT_JSON = os.path.join(MOVIES_ROOT, "movie-picker.json")
OUT_HTML = os.path.join(MOVIES_ROOT, "movie-picker.html")
CACHE_DIR = os.path.join(MOVIES_ROOT, "cache", "letterboxd")
POSTER_DIR = os.path.join(MOVIES_ROOT, "cache", "posters")

TMDB_BASE = "https://api.themoviedb.org/3"
POSTER_BASE = "https://image.tmdb.org/t/p/w500"

LB_DELAY = 2.0          # seconds between Letterboxd requests — be polite
LB_TIMEOUT = 20
TMDB_DELAY = 0.12       # TMDB allows ~50 req/s; stay well under
TMDB_TIMEOUT = 20
RETRY_UNAVAILABLE_AFTER_DAYS = 7   # re-try known-bad slugs only after a week

USER_AGENT = (
    "movie-picker/1.0 (personal watchlist dashboard; 96 titles; "
    "polite 2s delay; contact: local user)"
)

# Phase-1 inspection findings applied as explicit, auditable input corrections.
# Folder name -> title actually used for the TMDB search.
TITLE_OVERRIDES = {
    "Sheep Detective": "Three Bags Full: A Sheep Detective Movie",
}
# Explicit manual resolutions, for films TMDB catalogues under a name too
# different for the similarity threshold to accept. These are recorded in the
# output as match_method="manual-tmdb-id" with a reason, so the decision stays
# auditable rather than being a silent guess.
TMDB_ID_OVERRIDES = {
    "Sheep Detective": {
        "id": 1301421,
        "reason": (
            "media filename gives 'Three Bags Full: A Sheep Detective Movie'; "
            "TMDB catalogues this 2026 film as 'The Sheep Detectives' and "
            "returns it as the sole result for the folder name, the filename "
            "title, and 'Three Bags Full'"
        ),
    },
}
# Folders whose filename-derived year is known to be untrustworthy.
# ("I Want To Eat Your Pancreas" is tagged 2021 but no adaptation is from 2021.)
DISTRUST_YEAR = {
    "I Want To Eat Your Pancreas",
}

LOG_LINES = []


def log(msg):
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


# ----------------------------------------------------------------------------
# Phase 1 logic: scan the folder tree into input records
# ----------------------------------------------------------------------------

YEAR_RE = re.compile(r"(?<!\d)(19[0-9]{2}|20[0-4][0-9])(?!\d)")


def extract_year_from_filename(name):
    """Pull a plausible release year out of a scene-release filename.

    Guards against titles that *start* with a number (e.g. '2001 A Space
    Odyssey') by ignoring a year-looking token at position 0.
    """
    stem = os.path.splitext(name)[0]
    matches = list(YEAR_RE.finditer(stem))
    for m in matches:
        if m.start() == 0:
            continue  # leading number is part of the title, not a year
        return int(m.group(1))
    return None


def videos_in(folder, recursive=True):
    """All video files under a folder, largest first."""
    out = []
    walker = os.walk(folder) if recursive else [(folder, [], os.listdir(folder))]
    for root, _dirs, files in walker:
        for f in files:
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                p = os.path.join(root, f)
                try:
                    out.append((os.path.getsize(p), p, f))
                except OSError:
                    continue
    out.sort(reverse=True)
    return out


def biggest_video(folder):
    v = videos_in(folder)
    return v[0][2] if v else None


# Release-group and encoding noise that can trail a title in a scene filename.
RELEASE_NOISE = re.compile(
    r"\b(1080p|2160p|720p|480p|4k|bluray|blu-ray|brrip|bdrip|webrip|web-dl|web|hdrip|"
    r"dvdrip|x264|x265|h264|h265|hevc|10bit|aac\S*|ac3|dd5\S*|ddp5\S*|dts|remastered|"
    r"repack|proper|extended|imax|retail|dksubs|internal|limited|unrated|yify|yts\S*|"
    r"rarbg|lama|ethel|bone|neonoir|rmteam|gaz|deceit|bokutox|egybest|tubi|hi)\b.*",
    re.I,
)


def filename_title(name):
    """Derive a searchable title from a scene-release filename.

    Folder names on this drive are lossy — Windows strips punctuation, and some
    are abbreviated to the point of matching the wrong film ('Dilwale' is really
    'Dilwale Dulhania Le Jayenge'). The media filename usually carries the fuller
    title, so it is used as a second TMDB search candidate.
    """
    stem = os.path.splitext(name)[0]
    stem = re.sub(r"^\[[^\]]*\]\.?\s*", "", stem)      # leading [EgyBest].
    stem = re.sub(r"^\((.*?)\)\s*", r"\1 ", stem)      # leading (A Silent Voice)
    # Cut everything from the release year onward.
    for m in YEAR_RE.finditer(stem):
        if m.start() > 0:
            stem = stem[:m.start()]
            break
    if stem.count(".") >= 2:                            # dot-separated names only
        stem = stem.replace(".", " ")
    stem = stem.replace("_", " ")
    stem = RELEASE_NOISE.sub("", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .-[]()")
    return stem


def _record(title, year, source_file, folder_rel, notes, watched, collection=None):
    posix = folder_rel.replace("\\", "/")
    return {
        "input": {
            "title": title,
            "year": year,
            "search_title": TITLE_OVERRIDES.get(title, title),
            "source_file": source_file or "unknown",
            "folder": folder_rel,
            "folder_uri": "file:///" + MOVIES_ROOT.replace("\\", "/").rstrip("/")
                          + "/" + posix,
            "watched": watched,
            "collection": collection,
            "notes": notes,
        }
    }


def scan_dir_of_films(root, watched, records, collection=None):
    """One sub-directory per film: title from the folder, year from the media file."""
    for entry in sorted(os.scandir(root), key=lambda e: e.name.lower()):
        if not entry.is_dir() or entry.name in EXCLUDE_DIRS or entry.name.startswith("."):
            continue
        vids = videos_in(entry.path)
        if not vids:
            continue
        video = vids[0][2]
        year = extract_year_from_filename(video)
        folder = entry.name
        notes = []

        if folder in TITLE_OVERRIDES:
            notes.append(f"folder name is not the real title; searching TMDB as "
                         f"'{TITLE_OVERRIDES[folder]}' (taken from the media filename)")
        if folder in DISTRUST_YEAR and year is not None:
            notes.append(f"filename year {year} is not trustworthy for this title; "
                         f"searching TMDB without a year constraint")
            year = None
        if year is None and folder not in DISTRUST_YEAR:
            notes.append("no year found in folder or media filename")

        rel = os.path.relpath(entry.path, MOVIES_ROOT)
        rec = _record(folder, year, video, rel, notes, watched, collection)
        alt = filename_title(video)
        if alt and norm(alt) != norm(folder):
            rec["input"]["filename_title"] = alt
        records.append(rec)


def scan_franchise_dir(root, records):
    """#Movies Trilogy/<Franchise>/ — every video file is its own film."""
    for fr in sorted(os.scandir(root), key=lambda e: e.name.lower()):
        if not fr.is_dir() or fr.name.startswith("."):
            continue
        for _size, path, fname in sorted(videos_in(fr.path), key=lambda t: t[2].lower()):
            title = filename_title(fname)
            year = extract_year_from_filename(fname)
            if not title:
                continue
            notes = ["title derived from the media filename (franchise folder)"]
            if year is None:
                notes.append("no year found in the media filename")
            rel = os.path.relpath(os.path.dirname(path), MOVIES_ROOT)
            records.append(_record(title, year, fname, rel, notes, True, fr.name))


def scan_input():
    records = []
    scan_dir_of_films(MOVIES_ROOT, False, records)

    done = os.path.join(MOVIES_ROOT, "#Done")
    if os.path.isdir(done):
        scan_dir_of_films(done, True, records)
        trilogy = os.path.join(done, "#Movies Trilogy")
        if os.path.isdir(trilogy):
            scan_franchise_dir(trilogy, records)
    return records


# ----------------------------------------------------------------------------
# TMDB
# ----------------------------------------------------------------------------

class Tmdb:
    def __init__(self, key):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        # v4 bearer tokens are long JWTs; v3 keys are 32-char hex.
        self.bearer = len(key) > 40
        if self.bearer:
            self.s.headers["Authorization"] = f"Bearer {key}"
        self._key = key
        self.errors = 0

    def get(self, path, **params):
        if not self.bearer:
            params["api_key"] = self._key
        url = f"{TMDB_BASE}{path}"
        for attempt in range(4):
            try:
                r = self.s.get(url, params=params, timeout=TMDB_TIMEOUT)
            except requests.RequestException as e:
                # Never echo params — they may carry the v3 api_key.
                log(f"    TMDB network error on {path}: {type(e).__name__}")
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "2")) + 1
                log(f"    TMDB rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            if r.status_code == 401:
                raise SystemExit(
                    "TMDB rejected the credential (401). Check that TMDB_API_KEY "
                    "holds a valid v3 API key or v4 read access token."
                )
            if r.status_code == 404:
                return None
            if not r.ok:
                log(f"    TMDB HTTP {r.status_code} on {path}")
                time.sleep(1.0 * (attempt + 1))
                continue
            time.sleep(TMDB_DELAY)
            return r.json()
        self.errors += 1
        return None


def norm(s):
    """Normalise a title for comparison: strip accents, punctuation, case."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def title_similarity(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.93
    return SequenceMatcher(None, na, nb).ratio()


def year_of(datestr):
    if datestr and len(datestr) >= 4 and datestr[:4].isdigit():
        return int(datestr[:4])
    return None


def pick_tmdb_match(tmdb, rec):
    """Search TMDB and choose the most popular plausible candidate.

    Returns (chosen_or_None, disambiguation_info_or_None, considered_count).
    """
    title = rec["input"]["search_title"]
    year = rec["input"]["year"]

    # The folder name first, then the fuller title carried by the media filename.
    queries = [title]
    alt = rec["input"].get("filename_title")
    if alt and norm(alt) != norm(title):
        queries.append(alt)

    attempts = []
    for qt in queries:
        if year:
            attempts.append({"query": qt, "year": year})
    for qt in queries:
        attempts.append({"query": qt})

    for params in attempts:
        data = tmdb.get("/search/movie", include_adult="false", **params)
        if not data:
            continue
        results = data.get("results") or []
        if not results:
            continue

        constrained = "year" in params
        q = params["query"]          # score against the query that found it
        plausible = []
        for c in results:
            cy = year_of(c.get("release_date"))
            sim = max(
                title_similarity(q, c.get("title") or ""),
                title_similarity(q, c.get("original_title") or ""),
            )
            if sim < 0.82:
                continue
            if year and cy is not None and abs(cy - year) > 1:
                continue
            plausible.append((sim, c.get("popularity") or 0.0, cy, c))

        if not plausible:
            continue

        # Most popular among the plausible set — per spec.
        plausible.sort(key=lambda t: (t[1], t[0]), reverse=True)
        best = plausible[0]
        chosen = best[3]

        disamb = None
        if len(plausible) > 1:
            alts = []
            for sim, pop, cy, c in plausible[1:5]:
                alts.append({
                    "tmdb_id": c.get("id"),
                    "title": c.get("title"),
                    "year": cy,
                    "popularity": round(pop, 2),
                })
            disamb = {
                "candidate_count": len(plausible),
                "reason": (
                    "multiple plausible TMDB candidates; selected the most "
                    "popular one" + (" within the input year +/-1" if year else
                                     " (no input year available)")
                ),
                "selected": {
                    "tmdb_id": chosen.get("id"),
                    "title": chosen.get("title"),
                    "year": best[2],
                    "popularity": round(best[1], 2),
                },
                "rejected": alts,
                "searched_with_year": constrained,
            }
        return chosen, disamb, len(plausible)

    return None, None, 0


def fetch_tmdb_details(tmdb, movie_id):
    d = tmdb.get(f"/movie/{movie_id}", append_to_response="credits")
    if not d:
        return None
    credits = d.get("credits") or {}
    directors = [
        c.get("name") for c in credits.get("crew", [])
        if c.get("job") == "Director" and c.get("name")
    ]
    cast = [c.get("name") for c in (credits.get("cast") or [])[:5] if c.get("name")]
    genres = [g["name"] for g in d.get("genres") or [] if g.get("name")]
    langs = [l.get("english_name") for l in d.get("spoken_languages") or []
             if l.get("english_name")]
    countries = [c.get("name") for c in d.get("production_countries") or []
                 if c.get("name")]
    runtime = d.get("runtime") or None
    poster = d.get("poster_path")
    return {
        "id": d.get("id"),
        "title": d.get("title") or "unknown",
        "original_title": d.get("original_title") or "unknown",
        "year": year_of(d.get("release_date")),
        "director": ", ".join(directors) if directors else "unknown",
        "cast": cast,
        "genres": genres,
        "languages": langs,
        "countries": countries,
        "runtime": runtime,
        "synopsis": (d.get("overview") or "").strip() or "unknown",
        "poster_url": (POSTER_BASE + poster) if poster else None,
        "poster_local": None,          # filled in by cache_posters()
        "tmdb_url": f"https://www.themoviedb.org/movie/{d.get('id')}",
    }


# ----------------------------------------------------------------------------
# Letterboxd — rating and rating count ONLY
# ----------------------------------------------------------------------------

def slugify(text):
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " and ")
    s = re.sub(r"['\u2019\u02bc`]", "", s)        # apostrophes vanish, not hyphenate
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return re.sub(r"-{2,}", "-", s)


def slug_candidates(tm):
    """Ordered fallback slugs, used only if the /tmdb/<id>/ resolve fails."""
    out = []
    for base in (tm["title"], tm.get("original_title")):
        if not base or base == "unknown":
            continue
        s = slugify(base)
        if s and s not in out:
            out.append(s)
        if s and tm.get("year"):
            ys = f"{s}-{tm['year']}"
            if ys not in out:
                out.append(ys)
    return out


LD_RE = re.compile(
    r'<script type="application/ld\+json">\s*(?:/\*\s*<!\[CDATA\[\s*\*/)?\s*(\{.*?\})\s*(?:/\*\s*\]\]>\s*\*/)?\s*</script>',
    re.S,
)


def parse_letterboxd(page_text):
    """Extract ONLY name, year, average rating and rating count."""
    m = LD_RE.search(page_text)
    if not m:
        return None
    try:
        ld = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

    name = ld.get("name")
    lb_year = None
    ev = ld.get("releasedEvent") or []
    if isinstance(ev, list) and ev:
        sd = str(ev[0].get("startDate", ""))
        if sd[:4].isdigit():
            lb_year = int(sd[:4])

    agg = ld.get("aggregateRating") or {}
    rating = agg.get("ratingValue")
    count = agg.get("ratingCount")
    best = agg.get("bestRating") or 5
    return {
        "name": name,
        "year": lb_year,
        "rating": float(rating) if rating is not None else None,
        "rating_count": int(count) if count is not None else None,
        "scale": float(best),
    }


def cache_path(slug):
    safe = re.sub(r"[^a-z0-9\-]", "_", slug.lower())[:120] or "_"
    return os.path.join(CACHE_DIR, safe + ".json")


# The cache files are named by Letterboxd slug, but a slug is only known after
# resolution. This index maps the always-known TMDB id to the slug that was
# resolved for it, so a rerun finds the cached entry without spending a request.
INDEX_PATH = os.path.join(CACHE_DIR, "_index.json")


def index_load():
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def index_save(idx):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(idx, f, indent=2, sort_keys=True)


def cache_read(slug):
    p = cache_path(slug)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            entry = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if entry.get("status") == "available":
        return entry
    # Stale-unavailable entries get one retry per week, so a film that only
    # just appeared on Letterboxd eventually picks up a rating.
    try:
        ts = dt.datetime.fromisoformat(entry.get("fetched_at", ""))
    except ValueError:
        return entry
    age = (dt.datetime.now(dt.timezone.utc) - ts).days
    return None if age >= RETRY_UNAVAILABLE_AFTER_DAYS else entry


def cache_write(slug, entry):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path(slug), "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2, ensure_ascii=False)


class Letterboxd:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en",
        })
        self.last = 0.0
        self.requests_made = 0
        self.cache_hits = 0

    def _pace(self):
        gap = time.time() - self.last
        if gap < LB_DELAY:
            time.sleep(LB_DELAY - gap)
        self.last = time.time()

    def _get(self, url):
        self._pace()
        self.requests_made += 1
        for attempt in range(3):
            try:
                r = self.s.get(url, timeout=LB_TIMEOUT, allow_redirects=True)
            except requests.RequestException as e:
                log(f"    Letterboxd network error: {type(e).__name__}")
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 429:
                log("    Letterboxd rate limited, backing off 30s")
                time.sleep(30)
                continue
            return r
        return None

    def resolve(self, tm):
        """Return (slug, parsed, how) or (None, None, error_string).

        Primary path keys on the TMDB id, which removes slug guesswork and
        makes the match inherently verifiable. Slug construction is fallback.
        """
        r = self._get(f"https://letterboxd.com/tmdb/{tm['id']}/")
        if r is not None and r.ok and "/film/" in r.url:
            slug = r.url.rstrip("/").split("/film/")[-1].split("/")[0]
            parsed = parse_letterboxd(r.text)
            if parsed:
                return slug, parsed, "tmdb-id-redirect"

        for slug in slug_candidates(tm):
            r = self._get(f"https://letterboxd.com/film/{slug}/")
            if r is None:
                continue
            if r.status_code == 404:
                continue
            if not r.ok:
                log(f"    Letterboxd HTTP {r.status_code} for slug '{slug}'")
                continue
            parsed = parse_letterboxd(r.text)
            if not parsed:
                continue
            # Constructed slugs must be validated against the canonical film.
            name_ok = title_similarity(tm["title"], parsed["name"] or "") >= 0.85 or \
                      title_similarity(tm.get("original_title") or "", parsed["name"] or "") >= 0.85
            year_ok = (
                tm.get("year") is None or parsed["year"] is None
                or abs(parsed["year"] - tm["year"]) <= 1
            )
            if name_ok and year_ok:
                return slug, parsed, "constructed-slug-validated"
            log(f"    slug '{slug}' resolved to a different film "
                f"({parsed['name']} {parsed['year']}) — rejected")
        return None, None, "no reliable Letterboxd match"

    def rating_for(self, tm):
        """Cached lookup. Returns the letterboxd record for a movie."""
        idx = index_load()
        key = str(tm["id"])

        # Probe the resolved slug recorded for this TMDB id, then the
        # deterministic guesses, before spending a request.
        probes = []
        if key in idx and idx[key]:
            probes.append(idx[key])
        probes += [s for s in slug_candidates(tm) if s not in probes]
        for slug in probes:
            hit = cache_read(slug)
            if hit:
                self.cache_hits += 1
                return hit

        slug, parsed, how = self.resolve(tm)

        if slug is None:
            entry = {
                "slug": None,
                "rating": None,
                "rating_count": None,
                "rating_scale": 5,
                "status": "unavailable",
                "reason": how,
                "url": None,
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            # Cache the negative under the primary guess so reruns stay polite.
            cands = slug_candidates(tm)
            if cands:
                cache_write(cands[0], entry)
                idx[key] = cands[0]
                index_save(idx)
            return entry

        if parsed["rating"] is None:
            entry = {
                "slug": slug,
                "rating": None,
                "rating_count": parsed.get("rating_count"),
                "rating_scale": 5,
                "status": "unavailable",
                "reason": "page found but has no aggregate rating yet",
                "url": f"https://letterboxd.com/film/{slug}/",
                "matched_via": how,
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        else:
            entry = {
                "slug": slug,
                "rating": round(parsed["rating"], 2),
                "rating_count": parsed.get("rating_count"),
                "rating_scale": parsed.get("scale") or 5,
                "status": "available",
                "url": f"https://letterboxd.com/film/{slug}/",
                "matched_via": how,
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        cache_write(slug, entry)
        idx[key] = slug
        index_save(idx)
        return entry


# ----------------------------------------------------------------------------
# Poster cache — so the dashboard works with no internet at all
# ----------------------------------------------------------------------------

def cache_posters(records):
    """Download every poster once into cache/posters/<tmdb_id>.jpg.

    The dashboard then loads posters from disk and only falls back to the remote
    TMDB URL if a file is missing, which means it renders fully offline.
    """
    os.makedirs(POSTER_DIR, exist_ok=True)
    jobs = []
    for r in records:
        tm = r.get("tmdb")
        if not tm or not tm.get("poster_url"):
            continue
        dest = os.path.join(POSTER_DIR, f"{tm['id']}.jpg")
        rel = f"cache/posters/{tm['id']}.jpg"
        if os.path.exists(dest) and os.path.getsize(dest) > 1024:
            tm["poster_local"] = rel
            continue
        jobs.append((tm, tm["poster_url"], dest, rel))

    if not jobs:
        log("Posters: all cached already")
        return 0, 0

    log(f"Posters: downloading {len(jobs)} (cached: {len(records) - len(jobs)})")
    ok = fail = 0
    sess = requests.Session()
    sess.headers["User-Agent"] = USER_AGENT

    def fetch(job):
        tm, url, dest, rel = job
        for attempt in range(3):
            try:
                r = sess.get(url, timeout=30)
                if r.ok and r.content:
                    with open(dest, "wb") as f:
                        f.write(r.content)
                    tm["poster_local"] = rel
                    return True
            except requests.RequestException:
                time.sleep(1.0 * (attempt + 1))
        return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        for done in pool.map(fetch, jobs):
            if done:
                ok += 1
            else:
                fail += 1
    log(f"Posters: {ok} downloaded, {fail} failed")
    return ok, fail


# ----------------------------------------------------------------------------
# HTML rendering
# ----------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>D:\Movies — what to watch</title>
<style>
:root{
  --ink:#0F0D0C; --ink-2:#191513; --ink-3:#241E1B; --ink-4:#2E2622;
  --line:#332C27; --line-2:#463C36;
  --silver:#E6E1DA; --dim:#9C918A; --dimmer:#6E645E;
  --amber:#F0A83A; --amber-d:#B87C24;
  --tick:#7E93A6; --red:#C4443A; --seen:#5E8C6A;
  --display:"Bahnschrift","Segoe UI Semibold","Franklin Gothic Medium",system-ui,sans-serif;
  --body:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,sans-serif;
  --mono:"Cascadia Mono",Consolas,ui-monospace,monospace;
  --t100:48%; --t140:67%;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--ink);color:var(--silver);font-family:var(--body);
  font-size:15px;line-height:1.45;-webkit-font-smoothing:antialiased}
body.locked{overflow:hidden}
.grain{position:fixed;inset:0;pointer-events:none;z-index:60;opacity:.04;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='180' height='180' filter='url(%23n)'/%3E%3C/svg%3E")}
a{color:inherit}
:focus-visible{outline:2px solid var(--amber);outline-offset:2px;border-radius:2px}

/* ---- top bar ---------------------------------------------------------- */
.bar{position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:12px;
  padding:0 18px;height:54px;background:rgba(15,13,12,.94);
  backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.mark{font-family:var(--display);font-stretch:condensed;font-weight:700;
  font-size:19px;letter-spacing:.10em;white-space:nowrap}
.mark i{color:var(--amber);font-style:normal}
.tally{font-family:var(--mono);font-size:11px;color:var(--dimmer);
  letter-spacing:.08em;white-space:nowrap}
.grow{flex:1}
.bar input[type=search],.bar select,.bar button{font-family:var(--body);font-size:13px;
  color:var(--silver);background:var(--ink-2);border:1px solid var(--line);
  border-radius:3px;padding:7px 10px}
.bar input[type=search]{width:210px;min-width:100px}
.bar input[type=search]::placeholder{color:var(--dimmer)}
.bar select,.bar button{cursor:pointer}
.bar button:hover,.bar select:hover{border-color:var(--line-2)}
.bar button[aria-expanded=true]{border-color:var(--amber);color:var(--amber)}
#fcount{font-family:var(--mono);font-size:11px;color:var(--amber);margin-left:6px}
#dice{border-color:var(--amber-d);color:var(--amber);font-weight:600;white-space:nowrap}
#dice:hover{background:var(--amber);color:#1A1206;border-color:var(--amber)}

/* ---- filter tray ------------------------------------------------------ */
.tray{border-bottom:1px solid var(--line);background:var(--ink-2);padding:14px 18px 16px}
.tray[hidden]{display:none}
.grp{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-bottom:11px}
.grp:last-child{margin-bottom:0}
.grp>h3{font-family:var(--mono);font-size:10px;font-weight:400;color:var(--dimmer);
  letter-spacing:.14em;text-transform:uppercase;margin:0;width:74px;flex:none}
.chip{font-family:var(--body);font-size:12.5px;background:transparent;
  border:1px solid var(--line-2);color:var(--dim);border-radius:2px;
  padding:3px 9px;cursor:pointer;white-space:nowrap}
.chip:hover{color:var(--silver);border-color:var(--dim)}
.chip[aria-pressed=true]{background:var(--amber);border-color:var(--amber);
  color:#1A1206;font-weight:600}
.chip u{text-decoration:none;font-family:var(--mono);font-size:10px;opacity:.6;
  margin-left:5px}
.tray select{font-family:var(--body);font-size:12.5px;color:var(--silver);
  background:var(--ink-3);border:1px solid var(--line-2);border-radius:2px;
  padding:4px 8px;cursor:pointer;max-width:280px}

/* ---- status line ------------------------------------------------------ */
.status{display:flex;align-items:center;gap:14px;padding:11px 18px;
  border-bottom:1px solid var(--line);font-family:var(--mono);font-size:11px;
  letter-spacing:.07em;color:var(--dimmer);flex-wrap:wrap}
.status b{color:var(--silver);font-weight:400}
.status em{color:var(--amber);font-style:normal}
#clear{font-family:var(--mono);font-size:10px;letter-spacing:.1em;color:var(--dim);
  background:none;border:1px solid var(--line-2);border-radius:2px;padding:2px 8px;
  cursor:pointer;text-transform:uppercase}
#clear:hover{color:var(--silver);border-color:var(--dim)}
#clear[hidden]{display:none}

/* ---- grid ------------------------------------------------------------- */
main{padding:20px 18px 70px}
.grid{display:grid;gap:22px 16px;grid-template-columns:repeat(auto-fill,minmax(158px,1fr))}
.card{display:flex;flex-direction:column;min-width:0}
.pw{position:relative;display:block;width:100%;padding:0;aspect-ratio:2/3;
  background:var(--ink-3);border:1px solid var(--line);border-radius:2px;
  overflow:hidden;cursor:pointer;transition:transform .16s ease,border-color .16s ease}
.pw:hover{transform:translateY(-3px);border-color:var(--line-2)}
.pw img{width:100%;height:100%;object-fit:cover;display:block}
.pw.noimg::after{content:"Poster unavailable";position:absolute;inset:0;z-index:0;
  display:flex;align-items:center;justify-content:center;text-align:center;
  padding:14px;font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;
  color:var(--dimmer);line-height:1.6}
.card.seen .pw img{filter:grayscale(.72) brightness(.62)}
.card.seen .pw:hover img{filter:grayscale(.25) brightness(.85)}
.seenmark{position:absolute;top:6px;right:6px;z-index:3;width:19px;height:19px;
  border-radius:50%;background:rgba(10,8,7,.85);border:1px solid var(--seen);
  color:var(--seen);font-size:11px;line-height:17px;text-align:center}

.data{padding-top:9px}
.row{display:flex;align-items:baseline;gap:5px}
.rate{font-family:var(--display);font-stretch:condensed;font-weight:700;
  font-size:21px;line-height:1;color:var(--amber);letter-spacing:.01em}
.scale{font-family:var(--mono);font-size:9.5px;color:var(--dimmer)}
.votes{font-family:var(--mono);font-size:9.5px;color:var(--dimmer)}
.votes.thin{color:var(--red);opacity:.75}
.rate.none{font-size:11px;font-weight:600;color:var(--dimmer);letter-spacing:.06em;
  text-transform:uppercase}
.rt{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--dim)}
.rt.unk{color:var(--dimmer);font-size:9.5px;letter-spacing:.06em;text-transform:uppercase}
.ruler{position:relative;height:4px;margin:7px 0 8px;background:var(--ink-3);
  border-radius:1px;overflow:hidden}
.ruler i{position:absolute;left:0;top:0;bottom:0;width:0;
  background:var(--amber-d);border-radius:1px}
.ruler::after{content:"";position:absolute;inset:0;background:
  linear-gradient(90deg,transparent calc(var(--t100) - 1px),var(--tick) var(--t100),transparent calc(var(--t100) + 1px)),
  linear-gradient(90deg,transparent calc(var(--t140) - 1px),var(--tick) var(--t140),transparent calc(var(--t140) + 1px));
  opacity:.55}
.ruler.void{background:repeating-linear-gradient(90deg,var(--ink-3) 0 3px,transparent 3px 6px)}
.t{margin:0;font-size:13.5px;font-weight:600;line-height:1.3;letter-spacing:-.005em;
  min-height:2.6em;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical}
.m{margin:3px 0 0;font-family:var(--mono);font-size:10px;color:var(--dimmer);
  letter-spacing:.04em;line-height:1.5;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.m .dot{color:var(--amber);font-weight:700;cursor:help;margin-left:2px}
.flag{display:inline-block;font-family:var(--mono);font-size:9px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--red);border:1px solid var(--red);
  border-radius:2px;padding:1px 5px;margin-top:6px}
.flag.soft{color:var(--dimmer);border-color:var(--line-2)}
.flag.dupe{color:var(--tick);border-color:var(--tick)}
.sep{grid-column:1/-1;display:flex;align-items:center;gap:14px;margin:16px 0 2px;
  font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--dimmer)}
.sep::before,.sep::after{content:"";height:1px;background:var(--line);flex:1}
.empty{padding:80px 20px;text-align:center;color:var(--dim);font-size:14px}
.empty[hidden]{display:none}
footer{padding:0 18px 40px;font-family:var(--mono);font-size:10px;color:var(--dimmer);
  letter-spacing:.05em;line-height:1.9}
.grid.intro .card{animation:rise .34s ease-out backwards}
@keyframes rise{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:none}}

/* ---- detail panel ----------------------------------------------------- */
.scrim{position:fixed;inset:0;z-index:70;background:rgba(6,5,4,.72);
  backdrop-filter:blur(2px)}
.scrim[hidden]{display:none}
.panel{position:fixed;top:0;right:0;bottom:0;z-index:80;width:430px;max-width:100vw;
  background:var(--ink-2);border-left:1px solid var(--line-2);overflow-y:auto;
  padding:22px 24px 40px;animation:slide .2s ease-out}
.panel[hidden]{display:none}
@keyframes slide{from{transform:translateX(24px);opacity:.4}to{transform:none;opacity:1}}
.pclose{position:absolute;top:14px;right:16px;background:var(--ink-3);
  border:1px solid var(--line-2);color:var(--dim);border-radius:3px;width:28px;
  height:28px;cursor:pointer;font-size:14px;line-height:1}
.pclose:hover{color:var(--silver);border-color:var(--dim)}
.phead{display:flex;gap:16px;margin-bottom:18px}
.phead img{width:114px;flex:none;border-radius:2px;border:1px solid var(--line)}
.pmeta{min-width:0}
.pmeta h2{font-family:var(--display);font-stretch:condensed;font-weight:700;
  font-size:24px;line-height:1.12;margin:0 0 6px;letter-spacing:.005em}
.pmeta .sub{font-family:var(--mono);font-size:10.5px;color:var(--dim);
  letter-spacing:.06em;line-height:1.7}
.pbig{display:flex;align-items:baseline;gap:6px;margin:10px 0 2px}
.pbig .rate{font-size:27px}
.psyn{font-size:13.5px;line-height:1.62;color:var(--silver);margin:0 0 18px}
.psyn.none{color:var(--dimmer);font-style:italic}
.pfacts{border-top:1px solid var(--line);margin-bottom:18px}
.pfact{display:flex;gap:14px;padding:7px 0;border-bottom:1px solid var(--line);
  font-size:12.5px}
.pfact dt{font-family:var(--mono);font-size:10px;color:var(--dimmer);
  letter-spacing:.12em;text-transform:uppercase;width:86px;flex:none;padding-top:2px}
.pfact dd{margin:0;color:var(--silver)}
.pacts{display:flex;flex-wrap:wrap;gap:8px}
.pacts a,.pacts button{font-family:var(--body);font-size:12.5px;text-decoration:none;
  background:var(--ink-3);border:1px solid var(--line-2);color:var(--silver);
  border-radius:3px;padding:8px 12px;cursor:pointer}
.pacts a:hover,.pacts button:hover{border-color:var(--dim)}
.pacts .go{background:var(--amber);border-color:var(--amber);color:#1A1206;font-weight:600}
.pacts .go:hover{background:#ffbe57;border-color:#ffbe57}
.phint{font-family:var(--mono);font-size:9.5px;color:var(--dimmer);margin:10px 0 0;
  line-height:1.6}

@media (max-width:720px){
  .bar{flex-wrap:wrap;height:auto;padding:10px 14px;gap:9px}
  .bar input[type=search]{width:100%;order:5}
  .tally{display:none}
  main{padding:16px 14px 60px}
  .grid{gap:18px 12px;grid-template-columns:repeat(auto-fill,minmax(128px,1fr))}
  .grp>h3{width:100%}
  .panel{width:100%;padding:20px 16px 40px}
  .phead img{width:92px}
}
@media (prefers-reduced-motion:reduce){
  *{animation:none!important;transition:none!important}
  .pw:hover{transform:none}
}
</style>
</head>
<body>
<div class="grain" aria-hidden="true"></div>

<header class="bar">
  <div class="mark">D<i>:\</i>MOVIES</div>
  <div class="tally" id="tally"></div>
  <div class="grow"></div>
  <input type="search" id="q" placeholder="Search titles, directors, cast" autocomplete="off" spellcheck="false" aria-label="Search titles, directors, cast">
  <select id="sort" aria-label="Sort films">
    <option value="rating-desc">Highest rated</option>
    <option value="rating-asc">Lowest rated</option>
    <option value="year-desc">Newest first</option>
    <option value="year-asc">Oldest first</option>
    <option value="runtime-asc">Shortest first</option>
    <option value="runtime-desc">Longest first</option>
    <option value="title-asc">Title A–Z</option>
    <option value="title-desc">Title Z–A</option>
  </select>
  <button id="dice" title="Pick one at random from what is showing">Surprise me</button>
  <button id="ftoggle" aria-expanded="false" aria-controls="tray">Filters<span id="fcount"></span></button>
</header>

<section class="tray" id="tray" hidden>
  <div class="grp"><h3>Library</h3><span id="fw"></span></div>
  <div class="grp"><h3>Genre</h3><span id="fg"></span></div>
  <div class="grp"><h3>Decade</h3><span id="fd"></span></div>
  <div class="grp"><h3>Runtime</h3><span id="fr"></span></div>
  <div class="grp"><h3>Rating</h3><span id="fx"></span></div>
  <div class="grp"><h3>Director</h3><select id="fdir"></select></div>
</section>

<div class="status">
  <span id="shown"></span>
  <span id="active"></span>
  <button id="clear" hidden>Clear all</button>
</div>

<main>
  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" hidden>Nothing matches. Clear a filter to see more.</div>
</main>
<footer id="foot"></footer>

<div class="scrim" id="scrim" hidden></div>
<aside class="panel" id="panel" hidden role="dialog" aria-modal="true" aria-labelledby="ptitle">
  <button class="pclose" id="pclose" aria-label="Close details">✕</button>
  <div id="pbody"></div>
</aside>

<script type="application/json" id="data">__DATA__</script>
<script>
(function(){
"use strict";
var RAW = JSON.parse(document.getElementById("data").textContent);
var LS_STATE = "moviepicker.state.v3", LS_SEEN = "moviepicker.seen.v1";

function load(key,fb){ try{ var v=localStorage.getItem(key); return v?JSON.parse(v):fb; }
  catch(e){ return fb; } }
function save(key,val){ try{ localStorage.setItem(key,JSON.stringify(val)); }catch(e){} }

var marked = load(LS_SEEN, []);            // films marked watched inside the page
var markedSet = {};
marked.forEach(function(id){ markedSet[id]=1; });

var M = RAW.movies.map(function(m,i){
  var tm = m.tmdb || null, lb = m.letterboxd || {}, inp = m.input;
  var rated = lb.status === "available" && typeof lb.rating === "number";
  var title = (tm && tm.title && tm.title !== "unknown") ? tm.title : inp.title;
  var dir = (tm && tm.director && tm.director !== "unknown") ? tm.director : null;
  var cast = (tm && tm.cast) ? tm.cast : [];
  return {
    i:i, id: tm ? tm.id : ("x"+i), title:title,
    year:(tm && tm.year) ? tm.year : null,
    runtime:(tm && tm.runtime) ? tm.runtime : null,
    genres:(tm && tm.genres) ? tm.genres : [],
    languages:(tm && tm.languages) ? tm.languages : [],
    countries:(tm && tm.countries) ? tm.countries : [],
    cast:cast, director:dir,
    rating: rated ? lb.rating : null,
    votes: rated ? lb.rating_count : null,
    scale: lb.rating_scale || 5,
    url: lb.url || null,
    tmdbUrl:(tm && tm.tmdb_url) ? tm.tmdb_url : null,
    poster:(tm && (tm.poster_local || tm.poster_url)) || null,
    posterAlt:(tm && tm.poster_local && tm.poster_url) ? tm.poster_url : null,
    synopsis:(tm && tm.synopsis && tm.synopsis !== "unknown") ? tm.synopsis : null,
    onDisk: !!inp.watched,
    dup: m.duplicate || null,
    collection: inp.collection || null,
    folder: inp.folder, folderUri: inp.folder_uri,
    unmatched: m.match_status !== "matched",
    disamb: !!m.disambiguated,
    key:(title+" "+inp.title+" "+(dir||"")+" "+cast.join(" ")).toLowerCase()
  };
});
function seen(m){ return m.onDisk || !!markedSet[m.id]; }

var RMAX = M.reduce(function(a,m){ return m.runtime>a ? m.runtime : a; },0) || 200;
document.documentElement.style.setProperty("--t100",(100/RMAX*100).toFixed(2)+"%");
document.documentElement.style.setProperty("--t140",(140/RMAX*100).toFixed(2)+"%");

var genres={}, decades={}, dirs={};
M.forEach(function(m){
  m.genres.forEach(function(g){ genres[g]=(genres[g]||0)+1; });
  if(m.year){ var d=Math.floor(m.year/10)*10; decades[d]=(decades[d]||0)+1; }
  if(m.director) m.director.split(", ").forEach(function(n){ dirs[n]=(dirs[n]||0)+1; });
});
var hasNoYear = M.some(function(m){ return !m.year; });
var hasNoRt   = M.some(function(m){ return m.runtime===null; });

var DEF = {q:"",genres:{},decades:{},buckets:{},rate:"",lib:"unwatched",dir:"",sort:"rating-desc"};
var S = load(LS_STATE, null) || JSON.parse(JSON.stringify(DEF));
["genres","decades","buckets"].forEach(function(k){ if(!S[k]) S[k]={}; });
if(!S.sort) S.sort = DEF.sort;

var BUCKETS=[["u100","Under 100 min"],["100-140","100–140 min"],["o140","Over 140 min"]];
function bucketOf(rt){ return rt===null?"unknown":rt<100?"u100":rt<=140?"100-140":"o140"; }
function any(o){ for(var k in o) if(o[k]) return true; return false; }
function esc(s){ return String(s).replace(/[&<>"']/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }
function compact(n){ if(n==null) return "";
  if(n>=1e6) return (n/1e6).toFixed(n>=1e7?0:1)+"M";
  if(n>=1e3) return Math.round(n/1e3)+"k"; return String(n); }

var chips=[];
function chip(host,label,n,get,set){
  var b=document.createElement("button");
  b.className="chip"; b.type="button";
  b.innerHTML=esc(label)+(n!=null?'<u>'+n+'</u>':'');
  b.setAttribute("aria-pressed", get()?"true":"false");
  b.addEventListener("click",function(){ set(); syncChips(); render(); });
  host.appendChild(b);
  chips.push({el:b,get:get});
  return b;
}
function syncChips(){ chips.forEach(function(c){
  c.el.setAttribute("aria-pressed", c.get()?"true":"false"); }); }
function tog(bucket,key){ return [
  function(){ return !!S[bucket][key]; },
  function(){ if(S[bucket][key]) delete S[bucket][key]; else S[bucket][key]=true; }]; }
function excl(field,val){ return [
  function(){ return S[field]===val; },
  function(){ S[field] = (S[field]===val) ? "" : val; }]; }

var fw=document.getElementById("fw"), fg=document.getElementById("fg"),
    fd=document.getElementById("fd"), fr=document.getElementById("fr"),
    fx=document.getElementById("fx"), fdir=document.getElementById("fdir");

var nSeen = M.filter(seen).length;
(function(){ var a=excl("lib","unwatched"), b=excl("lib","watched");
  chip(fw,"Not watched",M.length-nSeen,a[0],a[1]);
  chip(fw,"Watched",nSeen,b[0],b[1]);
  var nDup = M.filter(function(m){ return m.dup; }).length;
  if(nDup){ var c=excl("lib","dupes"); chip(fw,"Filed twice",nDup,c[0],c[1]); }
})();
Object.keys(genres).sort().forEach(function(g){
  var t=tog("genres",g); chip(fg,g,genres[g],t[0],t[1]); });
Object.keys(decades).map(Number).sort(function(a,b){return a-b;}).forEach(function(d){
  var t=tog("decades",String(d)); chip(fd,d+"s",decades[d],t[0],t[1]); });
if(hasNoYear){ var tu=tog("decades","unknown"); chip(fd,"Year unknown",null,tu[0],tu[1]); }
BUCKETS.forEach(function(b){ var t=tog("buckets",b[0]); chip(fr,b[1],null,t[0],t[1]); });
if(hasNoRt){ var tr=tog("buckets","unknown"); chip(fr,"Runtime unknown",null,tr[0],tr[1]); }
(function(){ var a=excl("rate","rated"), b=excl("rate","unrated");
  chip(fx,"Rated only",null,a[0],a[1]); chip(fx,"Unrated only",null,b[0],b[1]); })();

(function(){
  var names=Object.keys(dirs).sort(function(a,b){
    return (dirs[b]-dirs[a]) || a.localeCompare(b); });
  var html='<option value="">All directors</option>';
  names.forEach(function(n){
    html+='<option value="'+esc(n)+'">'+esc(n)+(dirs[n]>1?"  ("+dirs[n]+" films)":"")+'</option>'; });
  fdir.innerHTML=html; fdir.value=S.dir||"";
  fdir.addEventListener("change",function(){ S.dir=fdir.value; render(); });
})();

function passes(m){
  if(S.lib==="unwatched" && seen(m)) return false;
  if(S.lib==="watched" && !seen(m)) return false;
  if(S.lib==="dupes" && !m.dup) return false;
  if(S.q && m.key.indexOf(S.q)===-1) return false;
  if(S.dir && (!m.director || m.director.split(", ").indexOf(S.dir)===-1)) return false;
  if(any(S.genres)){
    var hit=false;
    for(var k in S.genres){ if(m.genres.indexOf(k)!==-1){ hit=true; break; } }
    if(!hit) return false;
  }
  if(any(S.decades) && !S.decades[m.year?String(Math.floor(m.year/10)*10):"unknown"]) return false;
  if(any(S.buckets) && !S.buckets[bucketOf(m.runtime)]) return false;
  if(S.rate==="rated" && m.rating===null) return false;
  if(S.rate==="unrated" && m.rating!==null) return false;
  return true;
}
function cmp(a,b,mode){
  var p=mode.split("-"), k=p[0], d=p[1]==="asc"?1:-1;
  if(k==="title") return d*a.title.localeCompare(b.title,undefined,{sensitivity:"base"});
  if(k==="rating") return d*(a.rating-b.rating);
  var av=a[k], bv=b[k];
  if(av===null&&bv===null) return a.title.localeCompare(b.title);
  if(av===null) return 1;
  if(bv===null) return -1;
  return d*(av-bv);
}

var paint=0;
function card(m){
  var cls = "card" + (seen(m) ? " seen" : "");
  var h = '<article class="'+cls+'"><button class="pw'+(m.poster?'':' noimg')
        + '" data-i="'+m.i+'" aria-label="Details for '+esc(m.title)+'">';
  if(m.poster){
    var eager = paint++ < 14;
    h += '<img alt="" decoding="async" src="'+esc(m.poster)+'"'
       + (m.posterAlt ? ' data-alt="'+esc(m.posterAlt)+'"' : '')
       + (eager ? ' loading="eager" fetchpriority="high"' : ' loading="lazy"') + '>';
  }
  if(seen(m)) h += '<span class="seenmark" title="Watched">✓</span>';
  h += '</button><div class="data"><div class="row">';
  if(m.rating!==null){
    h += '<span class="rate">'+m.rating.toFixed(2)+'</span><span class="scale">/'+m.scale+'</span>'
       + '<span class="votes'+(m.votes<50000?' thin':'')+'" title="'
       + (m.votes||0).toLocaleString()+' Letterboxd ratings">'+compact(m.votes)+'</span>';
  } else {
    h += '<span class="rate none">Rating unavailable</span>';
  }
  h += m.runtime!==null ? '<span class="rt">'+m.runtime+' min</span>'
                        : '<span class="rt unk">Runtime unknown</span>';
  h += '</div>';
  h += m.runtime!==null
     ? '<div class="ruler"><i style="width:'+Math.min(100,m.runtime/RMAX*100).toFixed(1)+'%"></i></div>'
     : '<div class="ruler void"></div>';
  h += '<h2 class="t">'+esc(m.title)+'</h2>';
  var meta = esc(m.year ? m.year : "Year unknown");
  if(m.disamb) meta += '<b class="dot" title="Several TMDB candidates matched; the most popular was selected">°</b>';
  if(m.genres.length) meta += "  ·  " + esc(m.genres.slice(0,2).join(" · "));
  h += '<p class="m">'+meta+'</p>';
  if(m.unmatched) h += '<span class="flag">TMDB match unavailable</span>';
  if(!m.url && !m.unmatched) h += '<span class="flag soft">No Letterboxd match</span>';
  if(m.dup) h += '<span class="flag dupe" title="Also filed at: '
              + esc(m.dup.others.join(" | ")) + '">Filed '+m.dup.count+'×</span>';
  return h + '</div></article>';
}

var grid=document.getElementById("grid"), empty=document.getElementById("empty"),
    shown=document.getElementById("shown"), active=document.getElementById("active"),
    clearBtn=document.getElementById("clear"), fcount=document.getElementById("fcount");
var first=true, visible=[];

function render(){
  visible = M.filter(passes);
  paint = 0;
  var html;
  if(S.sort.indexOf("rating")===0){
    var r = visible.filter(function(m){return m.rating!==null;}).sort(function(a,b){return cmp(a,b,S.sort);});
    var u = visible.filter(function(m){return m.rating===null;}).sort(function(a,b){
              return a.title.localeCompare(b.title);});
    html = r.map(card).join("");
    if(u.length) html += '<div class="sep">'+u.length+' with no Letterboxd rating</div>'+u.map(card).join("");
  } else {
    html = visible.slice().sort(function(a,b){return cmp(a,b,S.sort);}).map(card).join("");
  }
  grid.className = "grid" + (first ? " intro" : "");
  grid.innerHTML = html;
  if(first){
    var cs = grid.querySelectorAll(".card");
    for(var i=0;i<cs.length && i<28;i++) cs[i].style.animationDelay=(i*22)+"ms";
    first=false;
  }
  empty.hidden = visible.length>0;
  shown.innerHTML = '<b>'+visible.length+'</b> of '+M.length+' films';

  var bits=[];
  if(S.lib==="unwatched") bits.push("not watched");
  if(S.lib==="watched") bits.push("watched");
  if(S.lib==="dupes") bits.push("filed more than once");
  if(S.q) bits.push('“'+S.q+'”');
  if(S.dir) bits.push(S.dir);
  var g=Object.keys(S.genres); if(g.length) bits.push(g.join(", "));
  var d=Object.keys(S.decades); if(d.length) bits.push(d.map(function(x){
    return x==="unknown"?"year unknown":x+"s"; }).join(", "));
  var b=Object.keys(S.buckets); if(b.length) bits.push(b.map(function(x){
    var f=BUCKETS.filter(function(y){return y[0]===x;});
    return f.length?f[0][1].toLowerCase():"runtime unknown"; }).join(", "));
  if(S.rate) bits.push(S.rate==="rated"?"rated only":"unrated only");
  active.innerHTML = bits.length ? '<em>'+esc(bits.join("  ·  "))+'</em>' : '';
  clearBtn.hidden = !bits.length;
  var n = g.length+d.length+b.length+(S.rate?1:0)+(S.q?1:0)+(S.dir?1:0)+(S.lib?1:0);
  fcount.textContent = n ? n : "";
  save(LS_STATE, S);
}

/* poster fallback: local file first, remote TMDB second, placeholder last */
grid.addEventListener("error",function(e){
  var img=e.target;
  if(img.tagName!=="IMG") return;
  var alt=img.getAttribute("data-alt");
  if(alt){ img.removeAttribute("data-alt"); img.src=alt; return; }
  var pw=img.parentNode; if(pw){ pw.classList.add("noimg"); img.remove(); }
},true);

/* ---- detail panel ----------------------------------------------------- */
var panel=document.getElementById("panel"), scrim=document.getElementById("scrim"),
    pbody=document.getElementById("pbody"), lastFocus=null;

function fact(label,val){
  return val ? '<div class="pfact"><dt>'+label+'</dt><dd>'+val+'</dd></div>' : "";
}
function openPanel(m){
  lastFocus = document.activeElement;
  var isSeen = seen(m);
  var h = '<div class="phead">';
  h += m.poster ? '<img alt="" src="'+esc(m.poster)+'">' : '';
  h += '<div class="pmeta"><h2 id="ptitle">'+esc(m.title)+'</h2><div class="sub">'
     + esc(m.year || "Year unknown")
     + (m.runtime ? "  ·  "+m.runtime+" min" : "  ·  runtime unknown")
     + (m.collection ? "<br>"+esc(m.collection)+" collection" : "")
     + '</div><div class="pbig">';
  h += m.rating!==null
     ? '<span class="rate">'+m.rating.toFixed(2)+'</span><span class="scale">/'+m.scale
       +'</span><span class="votes">'+(m.votes||0).toLocaleString()+' ratings</span>'
     : '<span class="rate none">Rating unavailable</span>';
  h += '</div></div></div>';

  h += m.synopsis ? '<p class="psyn">'+esc(m.synopsis)+'</p>'
                  : '<p class="psyn none">No synopsis available.</p>';
  h += '<dl class="pfacts">'
     + fact("Director", m.director ? esc(m.director) : '<span style="color:var(--dimmer)">unknown</span>')
     + fact("Cast", m.cast.length ? esc(m.cast.join(", ")) : "")
     + fact("Genres", m.genres.length ? esc(m.genres.join(", ")) : "")
     + fact("Language", m.languages.length ? esc(m.languages.join(", ")) : "")
     + fact("Country", m.countries.length ? esc(m.countries.join(", ")) : "")
     + fact("Folder", '<span style="font-family:var(--mono);font-size:11px">'+esc(m.folder)+'</span>')
     + (m.dup ? fact("Duplicate",
         '<span style="color:var(--tick)">This film is filed '+m.dup.count
         + ' times. Also at:</span><br><span style="font-family:var(--mono);font-size:11px">'
         + m.dup.others.map(esc).join("<br>")+'</span>') : "")
     + '</dl>';

  h += '<div class="pacts">';
  h += '<a class="go" href="'+esc(m.folderUri)+'">Open folder</a>';
  h += '<button id="pcopy" data-path="'+esc(m.folder)+'">Copy path</button>';
  if(m.url) h += '<a href="'+esc(m.url)+'" target="_blank" rel="noopener">Letterboxd ↗</a>';
  if(m.tmdbUrl) h += '<a href="'+esc(m.tmdbUrl)+'" target="_blank" rel="noopener">TMDB ↗</a>';
  if(!m.onDisk) h += '<button id="pseen">'+(isSeen?"Unmark watched":"Mark watched")+'</button>';
  h += '</div>';
  if(m.onDisk) h += '<p class="phint">Filed under #Done on disk, so it counts as watched.</p>';
  h += '<p class="phint">If "Open folder" does nothing, your browser is blocking local links — use Copy path instead.</p>';

  pbody.innerHTML = h;
  panel.hidden = false; scrim.hidden = false; document.body.classList.add("locked");
  document.getElementById("pclose").focus();

  var cp = document.getElementById("pcopy");
  if(cp) cp.addEventListener("click",function(){
    var full = "D:\\Movies\\" + cp.getAttribute("data-path");
    if(navigator.clipboard) navigator.clipboard.writeText(full);
    cp.textContent = "Copied"; setTimeout(function(){ cp.textContent="Copy path"; },1400);
  });
  var ps = document.getElementById("pseen");
  if(ps) ps.addEventListener("click",function(){
    if(markedSet[m.id]) { delete markedSet[m.id]; }
    else { markedSet[m.id]=1; }
    save(LS_SEEN, Object.keys(markedSet));
    ps.textContent = seen(m) ? "Unmark watched" : "Mark watched";
    render();
  });
}
function closePanel(){
  panel.hidden = true; scrim.hidden = true; document.body.classList.remove("locked");
  if(lastFocus && lastFocus.focus) lastFocus.focus();
}
grid.addEventListener("click",function(e){
  var btn = e.target.closest(".pw");
  if(!btn) return;
  var m = M[+btn.getAttribute("data-i")];
  if(m) openPanel(m);
});
document.getElementById("pclose").addEventListener("click",closePanel);
scrim.addEventListener("click",closePanel);
document.addEventListener("keydown",function(e){
  if(e.key==="Escape" && !panel.hidden){ closePanel(); }
});

/* ---- controls --------------------------------------------------------- */
var q=document.getElementById("q");
q.value = S.q || "";
q.addEventListener("input",function(){ S.q=q.value.trim().toLowerCase(); render(); });
q.addEventListener("keydown",function(e){
  if(e.key==="Escape"){ q.value=""; S.q=""; render(); } });

var sortSel=document.getElementById("sort");
sortSel.value = S.sort;
sortSel.addEventListener("change",function(e){ S.sort=e.target.value; render(); });

var tray=document.getElementById("tray"), ft=document.getElementById("ftoggle");
ft.addEventListener("click",function(){
  var open = tray.hidden;
  tray.hidden = !open;
  ft.setAttribute("aria-expanded", open?"true":"false");
});

clearBtn.addEventListener("click",function(){
  S = JSON.parse(JSON.stringify(DEF)); S.lib=""; S.sort=sortSel.value;
  q.value=""; fdir.value="";
  syncChips(); render();
});

document.getElementById("dice").addEventListener("click",function(){
  if(!visible.length) return;
  openPanel(visible[Math.floor(Math.random()*visible.length)]);
});

document.addEventListener("keydown",function(e){
  if(e.key==="/" && document.activeElement!==q && panel.hidden){
    e.preventDefault(); q.focus(); q.select(); }
});

var rated = M.filter(function(m){ return m.rating!==null; });
var lo = rated.length?Math.min.apply(null,rated.map(function(m){return m.rating;})):0;
var hi = rated.length?Math.max.apply(null,rated.map(function(m){return m.rating;})):0;
document.getElementById("tally").textContent =
  M.length+" FILMS · "+(M.length-nSeen)+" UNSEEN · "+lo.toFixed(2)+"–"+hi.toFixed(2);

var st = RAW.stats || {};
document.getElementById("foot").innerHTML =
  "Metadata from TMDB · ratings from Letterboxd, 0.5–5 scale · built "
  + esc((RAW.generated_at||"").slice(0,16).replace("T"," ")) + " UTC<br>"
  + esc(st.matched||0)+" matched · "+esc(st.unmatched||0)+" unmatched · "
  + esc(st.rated||0)+" rated · "+esc(st.unrated||0)+" unrated · "
  + esc(st.posters_cached||0)+" posters cached locally · ruler ticks mark 100 and 140 minutes";

syncChips();
render();
})();
</script>
</body>
</html>
"""


def render_html(payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # Keep the inline JSON from ever terminating the <script> block early.
    data = data.replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__DATA__", data)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def render_only():
    """Rebuild the HTML from the existing JSON. No TMDB, no Letterboxd, no key.

    This is what makes the dataset genuinely presentation-independent: the
    dashboard can be restyled and regenerated without refetching anything.
    """
    if not os.path.exists(OUT_JSON):
        sys.exit(f"No dataset at {OUT_JSON}. Run a full build first.")
    with open(OUT_JSON, "r", encoding="utf-8") as f:
        payload = json.load(f)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(render_html(payload))
    log(f"Re-rendered {OUT_HTML} from {OUT_JSON} ({len(payload['movies'])} films, "
        f"no network requests)")


def main():
    t0 = time.time()

    if "--render-only" in sys.argv:
        render_only()
        return

    key = os.environ.get("TMDB_API_KEY", "").strip()
    if not key:
        sys.exit(
            "TMDB_API_KEY is not set.\n"
            "Set it and re-run, e.g.:\n"
            '  PowerShell (this session):  $env:TMDB_API_KEY = "<your key>"\n'
            '  PowerShell (persistent):    setx TMDB_API_KEY "<your key>"\n'
        )

    os.makedirs(CACHE_DIR, exist_ok=True)

    records = scan_input()
    log(f"Scanned {len(records)} folders in {MOVIES_ROOT}")

    tmdb = Tmdb(key)
    lb = Letterboxd()

    issues = []
    disambiguations = []

    for n, rec in enumerate(records, 1):
        title = rec["input"]["title"]
        yr = rec["input"]["year"]
        log(f"[{n}/{len(records)}] {title} ({yr or 'year unknown'})")

        for note in rec["input"]["notes"]:
            issues.append({"title": title, "kind": "input", "detail": note})

        # --- TMDB -----------------------------------------------------------
        override = TMDB_ID_OVERRIDES.get(title)
        if override:
            chosen, disamb, n_plausible = {"id": override["id"]}, None, 1
            rec["match_method"] = "manual-tmdb-id"
            rec["match_note"] = override["reason"]
            log(f"    using manual TMDB id override {override['id']}")
            issues.append({"title": title, "kind": "manual-override",
                           "detail": override["reason"]})
        else:
            rec["match_method"] = "search"
            try:
                chosen, disamb, n_plausible = pick_tmdb_match(tmdb, rec)
            except SystemExit:
                raise
            except Exception as e:
                log(f"    TMDB lookup failed: {type(e).__name__}")
                chosen, disamb, n_plausible = None, None, 0
                issues.append({"title": title, "kind": "tmdb-error",
                               "detail": f"{type(e).__name__} during search"})

        if not chosen:
            rec["tmdb"] = None
            rec["letterboxd"] = {
                "slug": None, "rating": None, "rating_count": None,
                "rating_scale": 5, "status": "unavailable",
                "reason": "no TMDB match, so no canonical title/year to match on",
                "url": None,
            }
            rec["match_status"] = "unmatched"
            rec["disambiguated"] = False
            log("    -> NO TMDB MATCH")
            issues.append({"title": title, "kind": "unmatched",
                           "detail": "no sufficiently plausible TMDB candidate"})
            continue

        try:
            details = fetch_tmdb_details(tmdb, chosen["id"])
        except Exception as e:
            details = None
            log(f"    TMDB details failed: {type(e).__name__}")

        if not details:
            rec["tmdb"] = None
            rec["letterboxd"] = {
                "slug": None, "rating": None, "rating_count": None,
                "rating_scale": 5, "status": "unavailable",
                "reason": "TMDB details request failed", "url": None,
            }
            rec["match_status"] = "unmatched"
            rec["disambiguated"] = bool(disamb)
            issues.append({"title": title, "kind": "tmdb-error",
                           "detail": "details endpoint returned nothing"})
            continue

        rec["tmdb"] = details
        rec["match_status"] = "matched"
        rec["disambiguated"] = bool(disamb)
        if disamb:
            rec["disambiguation"] = disamb
            disambiguations.append({"input": title, "input_year": yr, **disamb})

        for field in ("director", "synopsis"):
            if details[field] == "unknown":
                issues.append({"title": title, "kind": "missing-metadata",
                               "detail": f"TMDB has no {field}"})
        if not details["genres"]:
            issues.append({"title": title, "kind": "missing-metadata",
                           "detail": "TMDB has no genres"})
        if details["runtime"] is None:
            issues.append({"title": title, "kind": "missing-metadata",
                           "detail": "TMDB has no runtime"})
        if details["poster_url"] is None:
            issues.append({"title": title, "kind": "missing-metadata",
                           "detail": "TMDB has no poster"})

        log(f"    -> TMDB {details['id']}  {details['title']} ({details['year']})"
            + ("  [disambiguated]" if disamb else ""))

        # --- Letterboxd (rating only) ---------------------------------------
        try:
            rec["letterboxd"] = lb.rating_for(details)
        except Exception as e:
            log(f"    Letterboxd failed: {type(e).__name__}")
            rec["letterboxd"] = {
                "slug": None, "rating": None, "rating_count": None,
                "rating_scale": 5, "status": "unavailable",
                "reason": f"{type(e).__name__} during fetch", "url": None,
            }

        r = rec["letterboxd"]
        if r["status"] == "available":
            log(f"    -> Letterboxd {r['rating']}/5 ({r['rating_count']} ratings)")
        else:
            log(f"    -> Letterboxd rating unavailable ({r.get('reason')})")
            issues.append({"title": title, "kind": "letterboxd",
                           "detail": r.get("reason") or "unavailable"})

    # --- duplicate detection ------------------------------------------------
    # Two folders resolving to the same TMDB id means the same film is filed
    # twice — usually a misnamed file rather than an intentional second copy.
    by_id = {}
    for r in records:
        tm = r.get("tmdb")
        if tm:
            by_id.setdefault(tm["id"], []).append(r)

    duplicates = []
    for tid, group in sorted(by_id.items()):
        if len(group) < 2:
            continue
        duplicates.append({
            "tmdb_id": tid,
            "title": group[0]["tmdb"]["title"],
            "year": group[0]["tmdb"]["year"],
            "copies": [{
                "input_title": g["input"]["title"],
                "folder": g["input"]["folder"],
                "file": g["input"]["source_file"],
            } for g in group],
        })
        for g in group:
            g["duplicate"] = {
                "count": len(group),
                "others": [x["input"]["folder"] for x in group if x is not g],
            }
        issues.append({
            "title": group[0]["tmdb"]["title"],
            "kind": "duplicate",
            "detail": f"{len(group)} folders resolve to the same film: "
                      + " | ".join(g["input"]["folder"] for g in group),
        })

    if duplicates:
        log("")
        log(f"Duplicates: {len(duplicates)} film(s) filed more than once")
        for d in duplicates:
            log(f"  {d['title']} ({d['year']}) — tmdb {d['tmdb_id']}")
            for c in d["copies"]:
                log(f"     {c['folder']}")
                log(f"        file: {c['file']}")
    else:
        log("Duplicates: none")

    # --- posters ------------------------------------------------------------
    try:
        cache_posters(records)
    except Exception as e:
        log(f"Poster caching failed ({type(e).__name__}); remote URLs will be used")

    # --- assemble output ----------------------------------------------------
    matched = sum(1 for r in records if r["match_status"] == "matched")
    rated = sum(1 for r in records
                if (r.get("letterboxd") or {}).get("status") == "available")
    elapsed = time.time() - t0

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": {"root": MOVIES_ROOT, "kind": "directory-scan",
                   "excluded": sorted(EXCLUDE_DIRS)},
        "stats": {
            "total": len(records),
            "watched": sum(1 for r in records if r["input"]["watched"]),
            "unwatched": sum(1 for r in records if not r["input"]["watched"]),
            "posters_cached": sum(1 for r in records
                                  if (r.get("tmdb") or {}).get("poster_local")),
            "matched": matched,
            "unmatched": len(records) - matched,
            "rated": rated,
            "unrated": len(records) - rated,
            "disambiguated": len(disambiguations),
            "duplicate_groups": len(duplicates),
            "duplicate_copies": sum(len(d["copies"]) for d in duplicates),
            "letterboxd_requests": lb.requests_made,
            "letterboxd_cache_hits": lb.cache_hits,
            "elapsed_seconds": round(elapsed, 1),
        },
        "disambiguations": disambiguations,
        "duplicates": duplicates,
        "issues": issues,
        "movies": records,
    }

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(render_html(payload))

    mins, secs = divmod(int(elapsed), 60)
    log("")
    log(f"TMDB      : {matched} matched, {len(records)-matched} unmatched")
    log(f"Letterboxd: {rated} rated, {len(records)-rated} unrated "
        f"({lb.requests_made} requests, {lb.cache_hits} cache hits)")
    log(f"Disambiguations: {len(disambiguations)}")
    log(f"Wrote {OUT_JSON}")
    log(f"Wrote {OUT_HTML}")
    log(f"Run time: {mins}m {secs}s")


if __name__ == "__main__":
    main()
