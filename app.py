"""Walkbox: find walking routes anywhere, add them to a searchable local
library, and download them as GPX files.

Sources:
  * OpenStreetMap official route relations   (c) OpenStreetMap contributors, ODbL
  * OpenStreetMap named footpaths, grouped   (c) OpenStreetMap contributors, ODbL
  * NZ Department of Conservation tracks     Source: DOC, CC BY 4.0
  * NZ national tracks (councils, LINZ, DOC)  Herenga a Nuku Outdoor Access Commission
  * GPX files you import yourself
"""
import html
import io
import json
import math
import os
import pickle
import re
import select
import sqlite3
import shutil
import subprocess
import sys
import threading
import time
import zlib
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as futures_wait
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import requests
from flask import Flask, Response, abort, g, jsonify, render_template, request

OVERPASS_URLS = [
    u.strip()
    for u in os.environ.get(
        "OVERPASS_URLS",
        "https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter",
    ).split(",")
    if u.strip()
]
DOC_URL = os.environ.get(
    "DOC_URL",
    "https://services1.arcgis.com/3JjYDyG3oajxU6HO/ArcGIS/rest/services/"
    "DOC_Walking_Experiences/FeatureServer/1/query",
)
NZT_URL = os.environ.get(
    "NZT_URL",
    "https://services2.arcgis.com/b5ADKIcWivL5vNaV/ArcGIS/rest/services/"
    "National_Tracks_Dataset_Pilot/FeatureServer/0/query",
)
WEATHER_URL = os.environ.get("WEATHER_URL", "https://api.open-meteo.com/v1/forecast")
COMMONS_URL = os.environ.get("COMMONS_URL", "https://commons.wikimedia.org/w/api.php")
PHOTOS_PER_MIN = int(os.environ.get("PHOTOS_PER_MINUTE", "30"))
VALHALLA_URL = os.environ.get("VALHALLA_URL", "").strip().rstrip("/")
ELEVATION_URL = os.environ.get("ELEVATION_URL", "https://api.open-meteo.com/v1/elevation")
DB_PATH = os.environ.get("DB_PATH", "/data/walkbox.db")
DATA_DIR = os.path.dirname(DB_PATH) or "."
OSM_EXTRACT_URL = os.environ.get("OSM_EXTRACT_URL", "https://download.geofabrik.de/australia-oceania/new-zealand-latest.osm.pbf")
OVERPASS_LOCAL_URL = os.environ.get("OVERPASS_LOCAL_URL", "").strip()    # e.g. http://walkbox-overpass/api/interpreter
VALHALLA_DIR = os.environ.get("VALHALLA_DIR", "/valhalla")               # optional read-only mount, only to show the size
OVERPASS_DIR = os.environ.get("OVERPASS_DIR", "/overpass")
SEED_CELL_DEG = float(os.environ.get("SEED_CELL_DEG", "0.5"))
SEED_PAUSE = float(os.environ.get("SEED_PAUSE_SECONDS", "0.3"))          # a polite gap between requests to the DOC / council servers
OSM_REFRESH_DAYS = float(os.environ.get("OSM_REFRESH_DAYS", "30"))        # the OSM file and store are renewed this often (0 = never by themselves)
MAX_SPAN_DEG = float(os.environ.get("MAX_SPAN_DEG", "0.5"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "150"))
SEARCH_LIMIT = int(os.environ.get("SEARCH_LIMIT", "25"))       # walks shown per map search, nearest the map centre first; 0 = no limit
MAX_PATH_WAYS = int(os.environ.get("MAX_PATH_WAYS", "2500"))
MIN_PATH_KM = float(os.environ.get("MIN_PATH_KM", "0.8"))
MIN_TRACK_KM = float(os.environ.get("MIN_TRACK_KM", "0.3"))
MAX_IMPORT_BYTES = int(os.environ.get("MAX_IMPORT_MB", "15")) * 1024 * 1024
ROUTE_TYPES = os.environ.get("ROUTE_TYPES", "hiking|foot|walking")
PATH_HIGHWAYS = os.environ.get("PATH_HIGHWAYS", "footway|path|track|pedestrian")
USER_AGENT = os.environ.get("USER_AGENT", "walkbox/1.1 (self-hosted walk library)")

ALL_SOURCES = ("routes", "paths", "doc", "nz")
NZ_BOX = (-48.0, -34.0, 166.0, 179.5)  # south, north, west, east
PATH_BASE = 10 ** 12  # synthetic ids for grouped OSM paths
DOC_BASE = 2 * 10 ** 12  # synthetic ids for DOC tracks
NZT_BASE = 3 * 10 ** 12  # synthetic ids for NZ national tracks
IMPORT_BASE = 4 * 10 ** 12  # synthetic ids for imported GPX files
CUSTOM_BASE = 5 * 10 ** 12  # ids for customised walks
GENERIC_NAMES = {"footpath", "path", "track", "walkway", "footway", "pavement", "steps",
                 "pedestrian", "boardwalk", "trail", "walking track", "unnamed"}

WALK_GRADE = {1: "Path", 2: "Short walk", 3: "Walking track", 4: "Tramping track",
              5: "Challenging tramping track", 6: "Route"}
DOG_TEXT = {"OffLeash": "off lead", "OnLeash": "on lead only", "Permit": "permit required",
            "ProhibitedAtTimes": "prohibited at times", "Prohibited": "not allowed"}
OSM_DOG = {"yes": "allowed", "leashed": "on lead only", "unleashed": "off lead", "no": "not allowed", "designated": "allowed"}
OSM_WHEELCHAIR = {"yes": "yes", "designated": "yes", "limited": "limited", "no": "no"}


def tag_facts(tags):
    """Dog and wheelchair information from OpenStreetMap tags (only what the map actually says)."""
    out = {}
    dog = OSM_DOG.get(str((tags or {}).get("dog", "")).lower())
    if dog:
        out["dogs"] = dog
    wc = OSM_WHEELCHAIR.get(str((tags or {}).get("wheelchair", "")).lower())
    if wc:
        out["wheelchair"] = wc
    return out

ATTRIBUTION = {
    "osm-route": "(c) OpenStreetMap contributors, ODbL (https://www.openstreetmap.org/copyright)",
    "osm-path": "(c) OpenStreetMap contributors, ODbL (https://www.openstreetmap.org/copyright)",
    "nz-track": "Source: Herenga a Nuku Outdoor Access Commission national tracks dataset, compiled "
                "from councils, LINZ and DOC. Licence not stated by the service; check the managing "
                "organisation before reuse.",
    "custom": "Customised by the user.",
    "import": "Imported by the user from a GPX file. Check the original source's terms before sharing.",
    "doc": "Source: New Zealand Department of Conservation, CC BY 4.0 "
           "(https://creativecommons.org/licenses/by/4.0/)",
}
SOURCE_LABEL = {"osm-route": "OSM route", "osm-path": "OSM path", "doc": "DOC",
                "nz-track": "NZ tracks", "import": "Imported", "custom": "Custom"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
_overpass_locks = {"bulk": threading.Lock(), "fast": threading.Lock()}   # the big searches and the lookups for an opened walk do not wait for each other
_cache = {}  # walk id -> parsed walk (from recent searches)
_cache_lock = threading.Lock()
_rate_lock = threading.Lock()
_weather_cache = {}
SEARCH_TTL = int(os.environ.get("SEARCH_CACHE_SECONDS", "21600"))
GRID_DEG = 0.05                    # searches are aligned to this grid (about 5 km), so nearby views share one cached search
HEDGE_SECONDS = float(os.environ.get("OVERPASS_HEDGE_SECONDS", "6"))
OVERPASS_TIMEOUT = int(os.environ.get("OVERPASS_TIMEOUT", "30"))
KV_TTL = {"photos": 30 * 86400, "light": 7 * 86400, "detail": 7 * 86400, "places": 86400, "terrain": 7 * 86400, "profile": 30 * 86400}
_rate_calls = {}


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS walks (
    osm_id      INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    ref         TEXT,
    network     TEXT,
    operator    TEXT,
    description TEXT,
    distance_km REAL NOT NULL,
    lat         REAL,
    lon         REAL,
    place       TEXT,
    tags        TEXT,
    geom        TEXT NOT NULL,
    added_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS walks_name ON walks(name);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS store (
    osm_id INTEGER PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL,
    s REAL, w REAL, n REAL, e REAL, light TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS store_src ON store(source, s, n);
CREATE INDEX IF NOT EXISTS store_name ON store(name);      -- lets a name search read just the names, not every saved route line
CREATE TABLE IF NOT EXISTS poi (
    id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL,
    name TEXT, hours TEXT, website TEXT
);
CREATE INDEX IF NOT EXISTS poi_lat ON poi(lat);
CREATE TABLE IF NOT EXISTS seed_state (
    item TEXT PRIMARY KEY, status TEXT NOT NULL, done REAL, total REAL, msg TEXT, finished REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS seed_cells (item TEXT NOT NULL, cell TEXT NOT NULL, PRIMARY KEY (item, cell));
"""
EXTRA_COLUMNS = {
    "source": "TEXT", "difficulty": "TEXT", "route_type": "TEXT", "ascent_m": "REAL",
    "link": "TEXT", "region": "TEXT", "time_text": "TEXT",
    "my_rating": "INTEGER", "my_note": "TEXT", "profile": "TEXT", "terrain": "TEXT",
    "dogs": "TEXT", "wheelchair": "TEXT", "status": "TEXT", "alerts_url": "TEXT", "conditions": "TEXT",
    "done": "INTEGER", "done_at": "TEXT",
}


def db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.executescript(SCHEMA)
        have = {r[1] for r in g.db.execute("PRAGMA table_info(walks)")}
        for col, typ in EXTRA_COLUMNS.items():
            if col not in have:
                g.db.execute("ALTER TABLE walks ADD COLUMN %s %s" % (col, typ))
        g.db.commit()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------
def haversine_m(a, b):
    r = 6371008.8
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(d))


def chain_length_m(chain):
    return sum(haversine_m(chain[i], chain[i + 1]) for i in range(len(chain) - 1))


def _same(a, b):
    return abs(a[0] - b[0]) < 1e-7 and abs(a[1] - b[1]) < 1e-7


def chain_segments(segs):
    """Join way geometries end-to-end into continuous chains (longest first)."""
    remaining = [list(s) for s in segs if len(s) >= 2]
    chains = []
    while remaining:
        chain = remaining.pop(0)
        grew = True
        while grew:
            grew = False
            for i, s in enumerate(remaining):
                if _same(chain[-1], s[0]):
                    chain = chain + s[1:]
                elif _same(chain[-1], s[-1]):
                    chain = chain + s[::-1][1:]
                elif _same(chain[0], s[-1]):
                    chain = s[:-1] + chain
                elif _same(chain[0], s[0]):
                    chain = s[::-1][:-1] + chain
                else:
                    continue
                remaining.pop(i)
                grew = True
                break
        chains.append(chain)
    chains.sort(key=chain_length_m, reverse=True)
    return chains


def simplify(chain, max_points=250):
    if len(chain) <= max_points:
        return chain
    step = len(chain) / max_points
    out = [chain[int(i * step)] for i in range(max_points)]
    out.append(chain[-1])
    return out


# --------------------------------------------------------------------------
# Derived, AllTrails-style stats
# --------------------------------------------------------------------------
def route_type_of(chains):
    main = chains[0]
    if len(main) > 2 and haversine_m(main[0], main[-1]) < 150 and chain_length_m(main) > 500:
        return "Loop"
    return "Point to point"


SAC = {
    "hiking": "Easy", "mountain_hiking": "Moderate", "demanding_mountain_hiking": "Hard",
    "alpine_hiking": "Hard", "demanding_alpine_hiking": "Hard", "difficult_alpine_hiking": "Hard",
}


def difficulty_from_tags(tags):
    sac = (tags or {}).get("sac_scale")
    return SAC.get(sac)


def est_minutes(distance_km, ascent_m):
    """Naismith-style: 5 km/h plus 10 minutes per 100 m of climbing."""
    return int(round(distance_km * 12 + (ascent_m or 0) * 0.1))


def fmt_minutes(m):
    if m < 60:
        return "%d min" % m
    return "%d h %02d min" % (m // 60, m % 60)


def elevation_gain(chain):
    """Best-effort total ascent in metres using a free elevation API."""
    pts = simplify(chain, 100)
    try:
        r = requests.get(
            ELEVATION_URL,
            params={"latitude": ",".join("%.5f" % p[0] for p in pts),
                    "longitude": ",".join("%.5f" % p[1] for p in pts)},
            headers={"User-Agent": USER_AGENT}, timeout=20,
        )
        elev = r.json().get("elevation") if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None
    if not elev or len(elev) != len(pts) or any(e is None for e in elev):
        return None
    gain = 0.0
    for a, b in zip(elev, elev[1:]):
        if b - a > 1.0:  # ignore sub-metre noise
            gain += b - a
    return round(gain, 0)


# --------------------------------------------------------------------------
# Walk construction
# --------------------------------------------------------------------------
def make_walk(osm_id, source, name, chains, tags=None, **extra):
    chains = [c for c in chains if len(c) >= 2]
    if not chains:
        return None
    pts = [p for c in chains for p in c]
    tags = tags or {}
    w = {
        "osm_id": osm_id, "source": source, "name": name,
        "ref": tags.get("ref"), "network": tags.get("network"), "operator": tags.get("operator"),
        "description": tags.get("description") or tags.get("note"),
        "distance_km": round(sum(chain_length_m(c) for c in chains) / 1000.0, 2),
        "lat": sum(p[0] for p in pts) / len(pts), "lon": sum(p[1] for p in pts) / len(pts),
        "tags": tags, "chains": chains, "route_type": route_type_of(chains),
        "difficulty": difficulty_from_tags(tags), "ascent_m": None,
        "link": None, "region": None, "time_text": None,
    }
    w.update(tag_facts(tags))
    w.update({k: v for k, v in extra.items() if v is not None})
    return w


def parse_relation(el):
    tags = el.get("tags", {})
    segs = [[(p["lat"], p["lon"]) for p in m["geometry"]]
            for m in el.get("members", []) if m.get("type") == "way" and m.get("geometry")]
    chains = chain_segments(segs)
    name = tags.get("name") or tags.get("ref") or "Unnamed route %d" % el["id"]
    return make_walk(el["id"], "osm-route", name, chains, tags)


def group_named_paths(elements, min_km=None):
    """Group named footpath ways into one walk per name."""
    min_km = MIN_PATH_KM if min_km is None else min_km
    groups = {}
    for el in elements:
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        tags = el.get("tags", {})
        name = (tags.get("name") or "").strip()
        if not name or name.lower() in GENERIC_NAMES:
            continue
        groups.setdefault(name, []).append(
            ([(p["lat"], p["lon"]) for p in el["geometry"]], tags))
    walks = []
    for name, items in groups.items():
        chains = chain_segments([seg for seg, _ in items])
        dist = sum(chain_length_m(c) for c in chains) / 1000.0
        if dist < min_km:
            continue
        merged = {}
        for _, t in items:
            for k, v in t.items():
                merged.setdefault(k, v)
        lat0, lon0 = chains[0][0]
        wid = PATH_BASE + zlib.crc32(("%s|%.2f|%.2f" % (name, lat0, lon0)).encode("utf-8"))
        wk = make_walk(wid, "osm-path", name, chains, merged)
        if wk:
            walks.append(wk)
    return walks


def parse_doc(data):
    walks = []
    for f in data.get("features", []):
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") == "LineString":
            lines = [coords]
        elif geom.get("type") == "MultiLineString":
            lines = coords
        else:
            continue
        segs = [[(c[1], c[0]) for c in line] for line in lines if len(line) >= 2]
        chains = chain_segments(segs)
        p = {str(k).lower(): v for k, v in (f.get("properties") or {}).items()}
        name = p.get("name") or "DOC track"
        oid = p.get("objectid") or f.get("id") or zlib.crc32(name.encode("utf-8"))
        link = p.get("walkingandtrampingwebpage") or p.get("staticlink")
        wk = make_walk(
            DOC_BASE + int(oid), "doc", name, chains, {"operator": "Department of Conservation"},
            description=p.get("introduction"), difficulty=p.get("difficulty"),
            time_text=p.get("completiontime"), region=p.get("region"), link=link, alerts_url=link,
        )
        if wk:
            walks.append(wk)
    return walks


def _lines_of(geom):
    coords = (geom or {}).get("coordinates") or []
    if (geom or {}).get("type") == "LineString":
        lines = [coords]
    elif (geom or {}).get("type") == "MultiLineString":
        lines = coords
    else:
        return []
    return [[(c[1], c[0]) for c in line] for line in lines if len(line) >= 2]


def parse_nz_tracks(data):
    """Group the national track segments by name into one walk per track."""
    groups = {}
    for f in data.get("features", []):
        segs = _lines_of(f.get("geometry"))
        if not segs:
            continue
        p = {str(k).lower(): v for k, v in (f.get("properties") or {}).items()}
        name = str(p.get("name") or p.get("maintrail") or "").strip()
        if not name or name.lower() in GENERIC_NAMES:
            continue
        grp = groups.setdefault(name.lower(), {"name": name, "segs": [], "props": []})
        grp["segs"] += segs
        grp["props"].append(p)
    walks = []
    for grp in groups.values():
        chains = chain_segments(grp["segs"])
        dist = sum(chain_length_m(c) for c in chains) / 1000.0
        if dist < MIN_TRACK_KM:
            continue
        sm = _nz_summary(grp["props"])
        lat0, lon0 = chains[0][0]
        wid = NZT_BASE + zlib.crc32(("%s|%.2f|%.2f" % (grp["name"], lat0, lon0)).encode("utf-8"))
        wk = make_walk(
            wid, "nz-track", grp["name"], chains, {"operator": sm["operator"]},
            description=sm["description"], difficulty=sm["difficulty"], link=sm["link"], region=sm["region"],
            dogs=sm["dogs"], wheelchair=sm["wheelchair"], status=sm["status"], conditions=sm["conditions"],
        )
        if wk:
            walks.append(wk)
    return walks


# --------------------------------------------------------------------------
# Remote sources
# --------------------------------------------------------------------------
def _short_error(url, exc):
    host = url.split("/")[2] if "//" in url else url
    text = str(exc).lower()
    if "name resolution" in text or "nodename" in text or "name or service not known" in text:
        reason = "DNS lookup failed (container has no working DNS)"
    elif "timed out" in text:
        reason = "timed out"
    else:
        reason = str(exc)[:120]
    return "%s: %s" % (host, reason)


def _overpass_once(url, query, timeout=None):
    try:
        r = requests.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=timeout or OVERPASS_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError("%s: HTTP %s" % (url.split("/")[2], r.status_code))
        data = r.json()
        remark = str(data.get("remark") or "") if isinstance(data, dict) else ""
        if "runtime error" in remark.lower():
            raise RuntimeError("%s: server too busy (%s)" % (url.split("/")[2], remark[:60]))
        return data
    except requests.RequestException as ex:
        raise RuntimeError(_short_error(url, ex))
    except ValueError as ex:
        raise RuntimeError(_short_error(url, ex))


_local_ok = {"ts": 0.0, "ok": False}


def local_overpass_ready():
    """True when the self-hosted Overpass container answers (checked at most once a minute)."""
    if not OVERPASS_LOCAL_URL:
        return False
    now = time.time()
    if now - _local_ok["ts"] > 60:
        _local_ok["ts"] = now
        try:
            r = requests.get(OVERPASS_LOCAL_URL.rsplit("/", 1)[0] + "/status", headers={"User-Agent": USER_AGENT}, timeout=4)
            _local_ok["ok"] = getattr(r, "status_code", 0) == 200
        except requests.RequestException:
            _local_ok["ok"] = False
    return _local_ok["ok"]


def overpass(query, lane="fast"):
    """Ask the first server; if it has not answered within a few seconds ask the next one as well, and use whoever answers first.

    lane "bulk" is for the list searches, "fast" for everything about a walk you have opened: they never queue behind each other.
    """
    urls = ([OVERPASS_LOCAL_URL] if local_overpass_ready() else []) + OVERPASS_URLS
    if not urls:
        raise RuntimeError("No Overpass server configured")
    errors = []
    limit = None
    with _overpass_locks.get(lane, _overpass_locks["fast"]):
        pool = ThreadPoolExecutor(max_workers=len(urls))
        try:
            pending = {pool.submit(_overpass_once, urls[0], query, limit): urls[0]}
            nxt = 1
            while pending:
                hedge = HEDGE_SECONDS if nxt < len(urls) else None
                done, _ = futures_wait(list(pending), timeout=hedge, return_when=FIRST_COMPLETED)
                if not done:                                    # the server is slow: try the next one too
                    pending[pool.submit(_overpass_once, urls[nxt], query, limit)] = urls[nxt]
                    nxt += 1
                    continue
                for fut in done:
                    pending.pop(fut)
                    try:
                        return fut.result()
                    except RuntimeError as ex:
                        errors.append(str(ex))
                if not pending and nxt < len(urls):            # everything so far failed: try the next one
                    pending[pool.submit(_overpass_once, urls[nxt], query, limit)] = urls[nxt]
                    nxt += 1
            raise RuntimeError("; ".join(errors))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------
# Persistent cache: slow lookups are kept in the database so they survive restarts
# --------------------------------------------------------------------------
def kv_ttl(kind):
    return SEARCH_TTL if kind == "search" else KV_TTL[kind]


def kv_get(kind, key):
    ttl = kv_ttl(kind)
    if ttl <= 0:
        return None
    row = db().execute("SELECT v, ts FROM kv WHERE k=?", ("%s:%s" % (kind, key),)).fetchone()
    if row and time.time() - row["ts"] < ttl:
        try:
            return json.loads(row["v"])
        except ValueError:
            return None
    return None


def kv_put(kind, key, obj):
    if kv_ttl(kind) <= 0:
        return
    conn = db()
    conn.execute("INSERT OR REPLACE INTO kv (k, v, ts) VALUES (?,?,?)", ("%s:%s" % (kind, key), json.dumps(obj), time.time()))
    conn.commit()
    if int(time.time()) % 50 == 0:                      # now and then, drop rows that are too old to be used
        conn.execute("DELETE FROM kv WHERE ts < ?", (time.time() - max(max(KV_TTL.values()), SEARCH_TTL),))
        conn.commit()


def kv_put_many(kind, items):
    if kv_ttl(kind) <= 0 or not items:
        return
    conn = db()
    now = time.time()
    conn.executemany("INSERT OR REPLACE INTO kv (k, v, ts) VALUES (?,?,?)",
                     [("%s:%s" % (kind, k), json.dumps(v), now) for k, v in items])
    conn.commit()


def snap_box(s, w, n, e):
    """Widen a search area outwards to the grid, so views that differ a little are the same search."""
    g = GRID_DEG
    f = lambda v: round(v, 3)
    return f(math.floor(s / g + 1e-9) * g), f(math.floor(w / g + 1e-9) * g), f(math.ceil(n / g - 1e-9) * g), f(math.ceil(e / g - 1e-9) * g)


def covering_search(s, w, n, e, srcs):
    """An earlier finished search of the same sources whose area contains this one."""
    ttl = SEARCH_TTL
    if ttl <= 0:
        return None
    rows = db().execute("SELECT k FROM kv WHERE k LIKE 'search:%' AND ts > ? ORDER BY ts DESC LIMIT 400", (time.time() - ttl,)).fetchall()
    for r in rows:
        try:
            box, got = r["k"][len("search:"):].split("|")
            s2, w2, n2, e2 = (float(x) for x in box.split(","))
        except ValueError:
            continue
        if got == srcs and s2 <= s + 1e-9 and w2 <= w + 1e-9 and n2 >= n - 1e-9 and e2 >= e - 1e-9:
            hit = kv_get("search", r["k"][len("search:"):])
            if hit:
                return hit
    return None


def cached_walk(osm_id):
    """A search result or a loaded walk from memory or the cache (route included when it was loaded before)."""
    with _cache_lock:
        wk = _cache.get(osm_id)
    if wk and wk.get("chains"):
        return wk
    full = kv_get("detail", osm_id)
    if full:
        with _cache_lock:
            _cache[osm_id] = full
        return full
    if wk:
        return wk
    light = kv_get("light", osm_id)
    if light:
        with _cache_lock:
            _cache[osm_id] = light
        return light
    return None


def make_light(osm_id, source, name, recipe, tags=None, distance_km=None, approx=False, centre=None, **extra):
    """A search-result entry with no route geometry yet. The recipe says how to fetch the route later."""
    tags = tags or {}
    w = {
        "osm_id": osm_id, "source": source, "name": name,
        "ref": tags.get("ref"), "network": tags.get("network"), "operator": tags.get("operator"),
        "description": tags.get("description") or tags.get("note"),
        "distance_km": round(distance_km, 2) if distance_km else None, "approx": approx,
        "lat": centre[0] if centre else None, "lon": centre[1] if centre else None,
        "tags": tags, "chains": None, "route_type": None, "difficulty": difficulty_from_tags(tags),
        "ascent_m": None, "link": None, "region": None, "time_text": None, "recipe": recipe,
    }
    w.update(tag_facts(tags))
    w.update({k: v for k, v in extra.items() if v is not None})
    return w


def _props(f):
    return {str(k).lower(): v for k, v in (f.get("attributes") or f.get("properties") or {}).items()}


def _km(p):
    try:
        v = float(p.get("shape__length"))
    except (TypeError, ValueError):
        return None
    return v / 1000.0 if v > 0 else None


def list_osm(s, w, n, e, want_routes, want_paths, max_rel=None, max_ways=None, lane="bulk", qtimeout=25):
    """Cheap OSM search: names, tags and bounding boxes only (no geometry).

    Exact tag matches (not regular expressions) and one global bbox keep the query fast on busy servers.
    """
    head = "[out:json][timeout:%d][bbox:%f,%f,%f,%f];" % (qtimeout, s, w, n, e)
    rel_types = [t for t in ROUTE_TYPES.split("|") if t]
    way_types = [t for t in PATH_HIGHWAYS.split("|") if t]
    parts = [head]
    if want_routes:
        parts.append("(%s)->.r;" % "".join('rel["type"="route"]["route"="%s"];' % t for t in rel_types))
    if want_paths:
        parts.append("(%s)->.p;" % "".join(
            'way["highway"="%s"]["name"]["access"!~"^(private|no)$"]["foot"!="no"];' % t for t in way_types))
    if want_routes:
        parts.append(".r out tags bb %d;" % (max_rel or MAX_RESULTS))
    if want_paths:
        parts.append(".p out tags bb %d;" % (max_ways or MAX_PATH_WAYS))
    walks, groups = [], {}
    for el in overpass("".join(parts), lane=lane).get("elements", []):
        b = el.get("bounds")
        if not b:
            continue
        tags = el.get("tags", {})
        if el.get("type") == "relation" and want_routes:
            name = tags.get("name") or tags.get("ref") or "Unnamed route %d" % el["id"]
            walks.append(make_light(el["id"], "osm-route", name, {"kind": "relation"}, tags,
                                    centre=((b["minlat"] + b["maxlat"]) / 2, (b["minlon"] + b["maxlon"]) / 2),
                                    bb=[b["minlat"], b["minlon"], b["maxlat"], b["maxlon"]]))
        elif el.get("type") == "way" and want_paths:
            name = (tags.get("name") or "").strip()
            if not name or name.lower() in GENERIC_NAMES:
                continue
            g = groups.setdefault(name, {"tags": {}, "diag": 0.0, "b": [90.0, 180.0, -90.0, -180.0]})
            g["diag"] += haversine_m((b["minlat"], b["minlon"]), (b["maxlat"], b["maxlon"])) / 1000.0
            g["b"] = [min(g["b"][0], b["minlat"]), min(g["b"][1], b["minlon"]),
                      max(g["b"][2], b["maxlat"]), max(g["b"][3], b["maxlon"])]
            for k, v in tags.items():
                g["tags"].setdefault(k, v)
    for name, g in groups.items():
        est = g["diag"] * 1.15            # straight-line sizes underestimate; the exact length comes on click
        if est < MIN_PATH_KM:
            continue
        clat, clon = (g["b"][0] + g["b"][2]) / 2, (g["b"][1] + g["b"][3]) / 2
        wid = PATH_BASE + zlib.crc32(("%s|%.2f|%.2f" % (name, clat, clon)).encode("utf-8"))
        pad = 0.002
        recipe = {"kind": "path", "name": name, "bbox": [g["b"][0] - pad, g["b"][1] - pad, g["b"][2] + pad, g["b"][3] + pad]}
        walks.append(make_light(wid, "osm-path", name, recipe, g["tags"], est, True, (clat, clon)))
    return walks


def _arcgis_get(url, params, label):
    try:
        r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=25)
        data = r.json()
    except (requests.RequestException, ValueError) as ex:
        raise RuntimeError(_short_error(url, ex))
    if r.status_code != 200 or "error" in data:
        raise RuntimeError("%s error: %s" % (label, (data.get("error") or {}).get("message") or r.status_code))
    return data


COARSE = {"outSR": 4326, "maxAllowableOffset": 0.003, "geometryPrecision": 4}   # a rough line is enough to put a dot on the map


def start_of(f):
    """(lat, lon) of the first point of an ArcGIS line feature, or None."""
    for path in ((f.get("geometry") or {}).get("paths") or []):
        if path:
            return (path[0][1], path[0][0])
    return None


def list_doc(s, w, n, e):
    params = {
        "where": "1=1", "geometry": "%f,%f,%f,%f" % (w, s, e, n), "geometryType": "esriGeometryEnvelope",
        "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "true", **COARSE,
        "outFields": "OBJECTID,name,introduction,difficulty,completionTime,"
                     "walkingAndTrampingWebPage,Shape__Length",
        "resultRecordCount": MAX_RESULTS, "f": "json",
    }
    return doc_lights(_arcgis_get(DOC_URL, params, "DOC").get("features", []))


def doc_lights(features):
    walks = []
    for f in features:
        p = _props(f)
        if p.get("objectid") is None:
            continue
        oid = int(p["objectid"])
        walks.append(make_light(
            DOC_BASE + oid, "doc", p.get("name") or "DOC track", {"kind": "doc", "oid": oid},
            {"operator": "Department of Conservation"}, _km(p), False, start_of(f),
            description=p.get("introduction"), difficulty=p.get("difficulty"), time_text=p.get("completiontime"),
            region=p.get("region"), link=p.get("walkingandtrampingwebpage") or p.get("staticlink"),
            alerts_url=p.get("walkingandtrampingwebpage") or p.get("staticlink")))
    return walks


NZ_WHERE = "Walk IN (1,2,3,4,5,6,98) AND (Status IS NULL OR Status NOT IN ('Decommisioned','Proposed'))"


def _nz_summary(props):
    def first(key):
        for pr in props:
            v = pr.get(key)
            if v not in (None, "", "Unknown"):
                return v
        return None
    grades = [pr.get("walk") for pr in props if pr.get("walk") in WALK_GRADE]
    mob = first("mobilityaccess")
    stats = {str(pr.get("status") or "").strip() for pr in props}
    return {"difficulty": WALK_GRADE[max(grades)] if grades else None,
            "description": first("description"),
            "link": first("weblink"), "region": first("locality") or first("localauthority"),
            "operator": first("managedby"), "authority": first("localauthority"),
            "dogs": DOG_TEXT.get(first("dogaccess")), "wheelchair": {"Yes": "yes", "No": "no"}.get(mob),
            "status": "Temporarily closed" if "TempClosed" in stats else None,
            "conditions": first("conditionsofuse")}


def nz_light_id(name, authority):
    return NZT_BASE + zlib.crc32(("%s|%s" % (name, authority or "")).lower().encode("utf-8"))


def list_nz(s, w, n, e):
    params = {
        "where": NZ_WHERE, "geometry": "%f,%f,%f,%f" % (w, s, e, n), "geometryType": "esriGeometryEnvelope",
        "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "true", **COARSE,
        "outFields": "OBJECTID,Name,MainTrail,Walk,Locality,LocalAuthority,Description,WebLink,"
                     "DogAccess,MobilityAccess,ManagedBy,Status,ConditionsOfUse,Shape__Length",
        "resultRecordCount": 2000, "f": "json",
    }
    return nz_lights(_arcgis_get(NZT_URL, params, "NZ tracks").get("features", []), [s, w, n, e])


def nz_lights(features, bbox):
    groups = {}
    for f in features:
        p = _props(f)
        name = str(p.get("name") or p.get("maintrail") or "").strip()
        if not name or name.lower() in GENERIC_NAMES:
            continue
        key = (name.lower(), str(p.get("localauthority") or "").lower())
        g = groups.setdefault(key, {"name": name, "props": [], "km": 0.0, "known": False, "start": None})
        g["props"].append(p)
        g["start"] = g["start"] or start_of(f)
        km = _km(p)
        if km:
            g["km"] += km
            g["known"] = True
    walks = []
    for g in groups.values():
        dist = g["km"] if g["known"] else None
        if dist is not None and dist < MIN_TRACK_KM:
            continue
        sm = _nz_summary(g["props"])
        walks.append(make_light(
            nz_light_id(g["name"], sm["authority"]), "nz-track", g["name"],
            {"kind": "nz", "name": g["name"], "bbox": bbox, "authority": sm["authority"]}, {"operator": sm["operator"]},
            dist, False, g["start"], description=sm["description"], difficulty=sm["difficulty"],
            link=sm["link"], region=sm["region"], dogs=sm["dogs"], wheelchair=sm["wheelchair"],
            status=sm["status"], conditions=sm["conditions"]))
    return walks


def overlaps_nz(s, w, n, e):
    return not (n < NZ_BOX[0] or s > NZ_BOX[1] or e < NZ_BOX[2] or w > NZ_BOX[3])


def search_all(s, w, n, e, sources):
    """Fetch the light lists from every chosen source at the same time."""
    jobs = []
    walks, problems = local_lists(s, w, n, e, sources)           # whatever the one-off download already holds
    done_here = local_covered(s, w, n, e)
    if ("routes" in sources or "paths" in sources) and "osm" not in done_here:
        jobs.append(("OpenStreetMap", lambda: list_osm(s, w, n, e, "routes" in sources, "paths" in sources)))
    if "doc" in sources and overlaps_nz(s, w, n, e) and "doc" not in done_here:
        jobs.append(("DOC", lambda: list_doc(s, w, n, e)))
    if "nz" in sources and overlaps_nz(s, w, n, e) and "nz" not in done_here:
        jobs.append(("NZ tracks", lambda: list_nz(s, w, n, e)))
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futures = [(label, pool.submit(fn)) for label, fn in jobs]
        for label, fut in futures:
            try:
                walks += fut.result()
            except RuntimeError as ex:
                problems.append("%s: %s" % (label, ex))
    walks = prefer_curated(walks)
    remember(walks)
    return walks, problems


def prefer_curated(walks):
    """Drop duplicates of the same name from the less curated sources."""
    doc_names = {x["name"].lower() for x in walks if x["source"] == "doc"}
    walks = [x for x in walks if not (x["source"] == "nz-track" and x["name"].lower() in doc_names)]
    curated = {x["name"].lower() for x in walks if x["source"] in ("doc", "nz-track")}
    return [x for x in walks if not (x["source"] == "osm-path" and x["name"].lower() in curated)]


def remember(walks):
    with _cache_lock:
        for wk in walks:
            old = _cache.get(wk["osm_id"])
            if not (old and old.get("chains")):          # keep a route that was already loaded
                _cache[wk["osm_id"]] = wk
        if len(_cache) > 3000:
            for k in list(_cache)[:1500]:
                _cache.pop(k, None)


def _esc_sql(v):
    return str(v).replace("'", "''")


def load_detail(wk):
    """Fetch the full route for one search result (on click or when adding it to the box)."""
    rec = wk.get("recipe") or {}
    kind = rec.get("kind")
    full = stored_detail(wk["osm_id"])
    if full:
        full["osm_id"] = wk["osm_id"]
        with _cache_lock:
            _cache[wk["osm_id"]] = full
        return full
    if kind == "relation":
        got = fetch_relations_by_ids([wk["osm_id"]])
        full = got[0] if got else None
    elif kind == "path":
        b = rec["bbox"]
        nm = rec["name"].replace("\\", "\\\\").replace('"', '\\"')
        q = ('[out:json][timeout:30];way["highway"~"^(%s)$"]["name"="%s"](%f,%f,%f,%f);out tags geom;'
             % (PATH_HIGHWAYS, nm, b[0], b[1], b[2], b[3]))
        got = [x for x in group_named_paths(overpass(q).get("elements", []), min_km=0) if x["name"] == rec["name"]]
        full = got[0] if got else None
    elif kind == "doc":
        data = _arcgis_get(DOC_URL, {"where": "OBJECTID=%d" % int(rec["oid"]), "outFields": "*", "outSR": 4326,
                                      "returnGeometry": "true", "f": "geojson"}, "DOC")
        got = parse_doc(data)
        full = got[0] if got else None
    elif kind == "nz":
        b, nm = rec["bbox"], _esc_sql(rec["name"])
        auth = " AND LocalAuthority='%s'" % _esc_sql(rec["authority"]) if rec.get("authority") else ""
        data = _arcgis_get(NZT_URL, {
            "where": "(Name='%s' OR MainTrail='%s') AND %s%s" % (nm, nm, NZ_WHERE, auth),
            "geometry": "%f,%f,%f,%f" % (b[1], b[0], b[3], b[2]), "geometryType": "esriGeometryEnvelope",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "outFields": "*", "outSR": 4326,
            "resultRecordCount": 2000, "f": "geojson"}, "NZ tracks")
        got = [x for x in parse_nz_tracks(data) if x["name"].lower() == rec["name"].lower()]
        full = got[0] if got else None
    if not full:
        raise RuntimeError("Could not load the route for \"%s\". The source may have changed; search again." % wk["name"])
    full["osm_id"] = wk["osm_id"]           # keep the id the list used
    full["recipe"] = rec
    for k in ("description", "link", "region", "difficulty", "time_text", "dogs", "wheelchair", "status", "alerts_url", "conditions"):
        if not full.get(k) and wk.get(k):
            full[k] = wk[k]
    with _cache_lock:
        _cache[wk["osm_id"]] = full
    try:
        kv_put("detail", wk["osm_id"], full)
    except Exception:                                       # the cache is a convenience, never a reason to fail
        pass
    return full


def main_chain(osm_id):
    """The longest track of a walk, from the box or from the search cache (loading it if needed)."""
    row = db().execute("SELECT geom FROM walks WHERE osm_id=?", (osm_id,)).fetchone()
    if row:
        return json.loads(row["geom"])[0], True
    wk = cached_walk(osm_id)
    if not wk:
        abort(404)
    if not wk.get("chains"):
        wk = load_detail(wk)                              # may raise RuntimeError
    return wk["chains"][0], False


def ensure_details(found):
    """Make sure every walk in the dict {id: walk} has its route, batching relation lookups."""
    need = [wk for wk in found.values() if not wk.get("chains")]
    rel = [wk["osm_id"] for wk in need if (wk.get("recipe") or {}).get("kind") == "relation"]
    if rel:
        for full in fetch_relations_by_ids(rel):
            full["recipe"] = {"kind": "relation"}
            found[full["osm_id"]] = full
    for wk in need:
        if (wk.get("recipe") or {}).get("kind") != "relation":
            found[wk["osm_id"]] = load_detail(wk)


def fetch_relations_by_ids(ids):
    q = "[out:json][timeout:60];rel(id:%s);out tags geom;" % ",".join(str(int(i)) for i in ids)
    walks = []
    for el in overpass(q).get("elements", []):
        if el.get("type") == "relation":
            wk = parse_relation(el)
            if wk:
                walks.append(wk)
    return walks


# --------------------------------------------------------------------------
# GPX
# --------------------------------------------------------------------------
def build_gpx(row, reverse=False):
    chains = json.loads(row["geom"])
    if reverse:
        chains = [c[::-1] for c in chains]
    source = row["source"] or "osm-route"
    try:
        tags = json.loads(row["tags"] or "{}")
    except ValueError:
        tags = {}
    base = (tags.get("base_source") if source == "custom" else source) or "osm-route"
    attribution = ATTRIBUTION.get(base, ATTRIBUTION["osm-route"])
    if source == "custom":
        attribution = "Customised by the user from another walk. Original data: " + attribution
    elif tags.get("customised"):
        attribution += " Customised by the user."
    desc_parts = []
    if row["description"]:
        desc_parts.append(row["description"])
    stats = ["%.1f km" % row["distance_km"]]
    if row["route_type"]:
        stats.append(row["route_type"].lower())
    if row["difficulty"]:
        stats.append("difficulty: %s" % row["difficulty"])
    if row["ascent_m"]:
        stats.append("ascent about %d m" % row["ascent_m"])
    desc_parts.append(", ".join(stats) + ". " + attribution)
    desc = " ".join(desc_parts)
    if base == "doc" and row["link"]:
        link = row["link"]
        link_text = "DOC page"
    elif base == "nz-track" and row["link"]:
        link, link_text = row["link"], "Track information"
    elif source == "custom" or base in ("import", "osm-path"):
        link, link_text = "https://www.openstreetmap.org/#map=15/%.5f/%.5f" % (row["lat"], row["lon"]), "Location"
    elif base == "import":
        link, link_text = "https://www.openstreetmap.org/#map=15/%.5f/%.5f" % (row["lat"], row["lon"]), "Location"
    elif source == "osm-path":
        link, link_text = "https://www.openstreetmap.org/#map=16/%.5f/%.5f" % (row["lat"], row["lon"]), "OpenStreetMap"
    else:
        link, link_text = "https://www.openstreetmap.org/relation/%d" % row["osm_id"], "OpenStreetMap relation"
    author = {"doc": "New Zealand Department of Conservation",
              "nz-track": "Herenga a Nuku Outdoor Access Commission",
              "import": "Original GPX author"}.get(base, "OpenStreetMap contributors")
    license_url = {"doc": "https://creativecommons.org/licenses/by/4.0/",
                   "nz-track": "https://www.herengaanuku.govt.nz/",
                   "import": "https://www.openstreetmap.org/copyright"}.get(
        base, "https://opendatacommons.org/licenses/odbl/")
    title = row["name"] + (" (reversed)" if reverse else "")
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="walkbox" xmlns="http://www.topografix.com/GPX/1/1">',
        "<metadata>",
        "<name>%s</name>" % escape(title),
        "<desc>%s</desc>" % escape(desc),
        '<link href="%s"><text>%s</text></link>' % (escape(link), escape(link_text)),
        '<copyright author="%s"><license>%s</license></copyright>' % (escape(author), license_url),
        "</metadata>",
        "<trk>",
        "<name>%s</name>" % escape(title),
        "<desc>%s</desc>" % escape(desc),
    ]
    for chain in chains:
        out.append("<trkseg>")
        for lat, lon in chain:
            out.append('<trkpt lat="%.7f" lon="%.7f"/>' % (lat, lon))
        out.append("</trkseg>")
    out += ["</trk>", "</gpx>"]
    return "\n".join(out)


def safe_filename(name, osm_id):
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" ._") or "walk"
    return "%s-%d.gpx" % (base[:80], osm_id)


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------
def public(walk, added=False, preview=True):
    """Shape a walk dict or DB row (as dict) for the browser."""
    ascent = walk.get("ascent_m")
    dist = walk.get("distance_km")
    mins = est_minutes(dist, ascent) if dist else None
    d = {
        "osm_id": walk["osm_id"], "name": walk["name"], "ref": walk.get("ref"),
        "network": walk.get("network"), "operator": walk.get("operator"),
        "description": walk.get("description"), "distance_km": dist, "approx": bool(walk.get("approx")),
        "lat": walk.get("lat"), "lon": walk.get("lon"), "added": added,
        "source": walk.get("source") or "osm-route",
        "source_label": SOURCE_LABEL.get(walk.get("source") or "osm-route", "OSM"),
        "difficulty": walk.get("difficulty"), "route_type": walk.get("route_type"),
        "ascent_m": ascent, "link": walk.get("link"), "region": walk.get("region"),
        "time_text": walk.get("time_text") or (("about " + fmt_minutes(mins)) if mins else None),
        "time_range": walk.get("time_text") or (
            ("%s – %s (est.)" % (fmt_minutes(int(round(mins * 0.75))), fmt_minutes(int(round(mins * 1.4))))) if mins else None),
        "dogs": walk.get("dogs"), "wheelchair": walk.get("wheelchair"), "status": walk.get("status"),
        "alerts_url": walk.get("alerts_url"), "conditions": walk.get("conditions"), "done": bool(walk.get("done")),
        "done_at": walk.get("done_at"),
        "place": walk.get("place"), "added_at": walk.get("added_at"),
        "my_rating": walk.get("my_rating"), "my_note": walk.get("my_note"),
    }
    if preview and walk.get("chains"):
        d["line"] = [simplify(c) for c in walk["chains"][:20]]
    return d


@app.route("/")
def index():
    return render_template("index.html", max_span=MAX_SPAN_DEG, routing_enabled=bool(VALHALLA_URL),
                           carto_key=os.environ.get("CARTO_KEY", "").strip(), search_limit=SEARCH_LIMIT)


@app.route("/healthz")
def healthz():
    return "ok"


def nearest_first(walks, centre, limit):
    """Order walks by distance from the map centre and keep the nearest `limit` (0 = keep all)."""
    cy, cx = centre
    k = math.cos(math.radians(cy))

    def far(wk):
        if wk.get("lat") is None or wk.get("lon") is None:
            return 1e9                                 # no position: last
        return (wk["lat"] - cy) ** 2 + ((wk["lon"] - cx) * k) ** 2
    walks = sorted(walks, key=lambda wk: (far(wk), -(wk.get("distance_km") or 0)))
    return walks[:limit] if limit and limit > 0 else walks


@app.post("/api/search")
def api_search():
    b = request.get_json(silent=True) or {}
    try:
        s, w, n, e = (float(b[k]) for k in ("south", "west", "north", "east"))
    except (KeyError, TypeError, ValueError):
        return jsonify(error="south, west, north, east are required numbers"), 400
    if not (-90 <= s < n <= 90) or not (-180 <= w <= 180) or not (-180 <= e <= 180):
        return jsonify(error="Invalid map area"), 400
    if e < w:
        return jsonify(error="Please pan the map so it does not cross the date line"), 400
    centre = ((s + n) / 2, (w + e) / 2)                # the list is ordered by distance from here
    if (n - s) > MAX_SPAN_DEG or (e - w) > MAX_SPAN_DEG:
        return jsonify(error="Zoom in further: the search area is too large (max about %d km)."
                       % int(MAX_SPAN_DEG * 111)), 400
    sources = [x for x in (b.get("sources") or ALL_SOURCES) if x in ALL_SOURCES]
    if not sources:
        return jsonify(error="Choose at least one source"), 400
    t0 = time.time()
    s, w, n, e = snap_box(s, w, n, e)
    srcs = ",".join(sorted(sources))
    key = "%.3f,%.3f,%.3f,%.3f|%s" % (s, w, n, e, srcs)
    hit = kv_get("search", key) or covering_search(s, w, n, e, srcs)
    if hit:
        walks, problems = hit["walks"], []
        with _cache_lock:                              # make sure the results can still be opened
            for wk in walks:
                _cache.setdefault(wk["osm_id"], wk)
    else:
        walks, problems = search_all(s, w, n, e, sources)
        if not problems:                               # never remember a partial or failed search
            kv_put("search", key, {"walks": walks})
            kv_put_many("light", [(wk["osm_id"], wk) for wk in walks if not wk.get("chains")])
    if not walks and problems:
        return jsonify(error="Could not search: " + " | ".join(problems)), 502
    have = {r["osm_id"] for r in db().execute("SELECT osm_id FROM walks")}
    order = {"doc": 0, "nz-track": 1, "osm-route": 2, "osm-path": 3}
    walks.sort(key=lambda x: (order.get(x["source"], 9), -(x["distance_km"] or 0)))
    total = len(walks)
    walks = nearest_first(walks, centre, SEARCH_LIMIT)
    return jsonify(results=[public(wk, wk["osm_id"] in have) for wk in walks],
                   warnings=problems, capped=total >= MAX_RESULTS or len(walks) < total, truncated=len(walks) < total,
                   total=total, ms=int((time.time() - t0) * 1000))


@app.post("/api/walks")
def api_add():
    body = request.get_json(silent=True) or {}
    try:
        ids = [int(i) for i in body.get("ids", [])][:100]
    except (TypeError, ValueError):
        return jsonify(error="ids must be numbers"), 400
    if not ids:
        return jsonify(error="No walks selected"), 400
    found = {}
    for i in ids:
        wk = cached_walk(i)
        if wk:
            found[i] = wk
    missing = [i for i in ids if i not in found]
    expired = [i for i in missing if i >= PATH_BASE]
    if expired:
        return jsonify(error="Those results have expired. Search the area again, then add them."), 409
    if missing:
        try:
            for wk in fetch_relations_by_ids(missing):
                found[wk["osm_id"]] = wk
        except RuntimeError as ex:
            return jsonify(error="OpenStreetMap server problem: %s" % ex), 502
    try:
        ensure_details(found)
    except RuntimeError as ex:
        return jsonify(error="Could not fetch the route: %s" % ex), 502
    try:
        flip = {int(i) for i in body.get("reverse", [])}
    except (TypeError, ValueError):
        flip = set()
    for wid in flip & set(found):                       # walks shown the other way round are saved that way
        found[wid] = dict(found[wid], chains=[c[::-1] for c in found[wid]["chains"]])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = db()
    existing = {r["osm_id"] for r in conn.execute(
        "SELECT osm_id FROM walks WHERE osm_id IN (%s)" % ",".join("?" * len(found)), list(found)
    )} if found else set()
    added = 0
    for wk in found.values():
        if wk["osm_id"] in existing:
            continue
        if wk.get("ascent_m") is None:
            wk["ascent_m"] = elevation_gain(wk["chains"][0])
        cur = conn.execute(
            "INSERT OR IGNORE INTO walks (osm_id,name,ref,network,operator,description,distance_km,"
            "lat,lon,place,tags,geom,added_at,source,difficulty,route_type,ascent_m,link,region,time_text,"
            "dogs,wheelchair,status,alerts_url,conditions)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wk["osm_id"], wk["name"], wk["ref"], wk["network"], wk["operator"], wk["description"],
             wk["distance_km"], wk["lat"], wk["lon"], body.get("place"),
             json.dumps(wk["tags"]), json.dumps(wk["chains"]), now, wk["source"], wk["difficulty"],
             wk["route_type"], wk["ascent_m"], wk["link"], wk["region"], wk["time_text"],
             wk.get("dogs"), wk.get("wheelchair"), wk.get("status"), wk.get("alerts_url"), wk.get("conditions")),
        )
        added += cur.rowcount
    conn.commit()
    return jsonify(added=added, skipped=len(ids) - added)


@app.post("/api/detail")
def api_detail():
    try:
        wid = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return jsonify(error="id must be a number"), 400
    row = db().execute("SELECT * FROM walks WHERE osm_id=?", (wid,)).fetchone()
    if row:
        d = dict(row)
        d["chains"] = json.loads(d["geom"])
        return jsonify(public(d, True))
    wk = cached_walk(wid)
    if not wk:
        return jsonify(error="That result has expired. Search the area again."), 409
    if not wk.get("chains"):
        try:
            wk = load_detail(wk)
        except RuntimeError as ex:
            return jsonify(error=str(ex)), 502
    return jsonify(public(wk, False))


@app.get("/api/library")
def api_library():
    q = (request.args.get("q") or "").strip()
    min_km = request.args.get("min_km", type=float)
    max_km = request.args.get("max_km", type=float)
    sort = request.args.get("sort", "added")
    sql = "SELECT * FROM walks WHERE 1=1"
    args = []
    for term in q.split():
        like = "%" + term.replace("%", "").replace("_", "") + "%"
        sql += (" AND (name LIKE ? OR ref LIKE ? OR operator LIKE ? OR network LIKE ?"
                " OR description LIKE ? OR place LIKE ? OR region LIKE ? OR difficulty LIKE ?"
                " OR my_note LIKE ?)")
        args += [like] * 9
    if min_km is not None:
        sql += " AND distance_km >= ?"
        args.append(min_km)
    if max_km is not None:
        sql += " AND distance_km <= ?"
        args.append(max_km)
    order = {"added": "added_at DESC", "name": "name COLLATE NOCASE ASC",
             "longest": "distance_km DESC", "shortest": "distance_km ASC"}.get(sort, "added_at DESC")
    rows = db().execute(sql + " ORDER BY " + order + " LIMIT 500", args).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["chains"] = json.loads(d["geom"])
        items.append(public(d, added=True))
    return jsonify(results=items)


@app.get("/walks/<int:osm_id>.gpx")
def download_gpx(osm_id):
    row = db().execute("SELECT * FROM walks WHERE osm_id=?", (osm_id,)).fetchone()
    if not row:
        abort(404)
    rev = request.args.get("reverse") == "1"
    fname = safe_filename(row["name"] + (" reversed" if rev else ""), osm_id)
    return Response(
        build_gpx(row, reverse=rev), mimetype="application/gpx+xml",
        headers={"Content-Disposition": 'attachment; filename="%s"' % fname},
    )


@app.get("/walks.zip")
def download_zip():
    try:
        id_list = [int(i) for i in request.args.get("ids", "").split(",") if i.strip()]
    except ValueError:
        abort(400)
    conn = db()
    if id_list:
        marks = ",".join("?" * len(id_list))
        rows = conn.execute("SELECT * FROM walks WHERE osm_id IN (%s)" % marks, id_list).fetchall()
    else:
        rows = conn.execute("SELECT * FROM walks").fetchall()
    if not rows:
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        used = set()
        for r in rows:
            z.writestr(safe_filename(r["name"], r["osm_id"]), build_gpx(r))
            used.add(r["source"] or "osm-route")
            if r["source"] == "custom":
                try:
                    used.add(json.loads(r["tags"] or "{}").get("base_source") or "osm-route")
                except ValueError:
                    pass
        z.writestr("ATTRIBUTION.txt", "\n".join("Route data: " + ATTRIBUTION.get(u, ATTRIBUTION["osm-route"]) for u in sorted(used)) + "\n")
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="walkbox-walks.zip"'})


# --------------------------------------------------------------------------
# Turn-by-turn routing (self-hosted Valhalla, free, New Zealand extract)
# --------------------------------------------------------------------------
def decode_polyline6(encoded):
    """Decode Valhalla's precision-6 encoded polyline into [(lat, lon), ...]."""
    coords, index, lat, lon = [], 0, 0, 0
    while index < len(encoded):
        for axis in (0, 1):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        coords.append((lat / 1e6, lon / 1e6))
    return coords


def valhalla_call(path, payload):
    if not VALHALLA_URL:
        raise RuntimeError("Routing is not set up (no VALHALLA_URL).")
    try:
        r = requests.post(VALHALLA_URL + path, json=payload, timeout=60,
                          headers={"User-Agent": USER_AGENT})
    except requests.RequestException as ex:
        raise RuntimeError("The routing engine is not reachable yet. If this is the first start it is "
                           "still building the New Zealand map tiles (this can take a while). (%s)"
                           % _short_error(VALHALLA_URL, ex))
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200:
        msg = data.get("error") or "HTTP %s" % r.status_code
        code = data.get("error_code")
        if code in (170, 171, 442, 443):
            msg = "No walkable path found near those points. Routing covers New Zealand only."
        raise RuntimeError(msg)
    return data


def shape_route(data):
    trip = data.get("trip") or {}
    shape, steps = [], []
    for leg in trip.get("legs", []):
        pts = decode_polyline6(leg.get("shape", ""))
        offset = len(shape)
        for m in leg.get("maneuvers", []):
            i = m.get("begin_shape_index", 0)
            at = pts[i] if i < len(pts) else (pts[-1] if pts else None)
            steps.append({
                "text": m.get("instruction") or m.get("verbal_pre_transition_instruction") or "",
                "km": round(m.get("length", 0), 2), "min": round(m.get("time", 0) / 60.0, 1),
                "type": m.get("type"), "at": list(at) if at else None,
            })
        shape.extend(pts[1:] if offset and pts else pts)
    summ = trip.get("summary") or {}
    return {"distance_km": round(summ.get("length", 0), 2), "minutes": round(summ.get("time", 0) / 60.0),
            "shape": [list(p) for p in simplify(shape, 1500)], "steps": steps}


@app.get("/api/route/status")
def api_route_status():
    if not VALHALLA_URL:
        return jsonify(enabled=False, ready=False)
    try:
        r = requests.get(VALHALLA_URL + "/status", timeout=5)
        return jsonify(enabled=True, ready=r.status_code == 200)
    except requests.RequestException:
        return jsonify(enabled=True, ready=False)


def _pt(v):
    lat, lon = float(v[0]), float(v[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError
    return {"lat": lat, "lon": lon}


@app.post("/api/route")
def api_route():
    if not VALHALLA_URL:
        return jsonify(error="Routing is not set up on this server."), 404
    b = request.get_json(silent=True) or {}
    opts = {"units": "kilometers", "language": "en-US"}
    try:
        if b.get("mode") in ("to_start", "leg"):
            payload = {"locations": [_pt(b["from"]), _pt(b["to"])], "costing": "pedestrian",
                       "directions_options": opts}
            data = valhalla_call("/route", payload)
        elif b.get("mode") == "along":
            row = db().execute("SELECT geom FROM walks WHERE osm_id=?", (int(b["walk_id"]),)).fetchone()
            if not row:
                return jsonify(error="That walk is not in your box."), 404
            chain = simplify(json.loads(row["geom"])[0], 400)
            if b.get("reverse"):
                chain = chain[::-1]
            payload = {"shape": [{"lat": p[0], "lon": p[1]} for p in chain], "costing": "pedestrian",
                       "shape_match": "map_snap", "directions_options": opts}
            data = valhalla_call("/trace_route", payload)
        else:
            return jsonify(error="mode must be to_start or along"), 400
    except (KeyError, TypeError, ValueError, IndexError):
        return jsonify(error="Invalid request"), 400
    except RuntimeError as ex:
        return jsonify(error=str(ex)), 502
    return jsonify(shape_route(data))


# --------------------------------------------------------------------------
# Weather (Open-Meteo, free) and nearby places (OpenStreetMap)
# --------------------------------------------------------------------------
WMO = {0: ("Clear sky", "☀️"), 1: ("Mainly clear", "🌤️"), 2: ("Partly cloudy", "⛅"), 3: ("Overcast", "☁️"),
       45: ("Fog", "🌫️"), 48: ("Freezing fog", "🌫️"), 51: ("Light drizzle", "🌦️"), 53: ("Drizzle", "🌦️"),
       55: ("Heavy drizzle", "🌧️"), 56: ("Freezing drizzle", "🌧️"), 57: ("Freezing drizzle", "🌧️"),
       61: ("Light rain", "🌦️"), 63: ("Rain", "🌧️"), 65: ("Heavy rain", "🌧️"), 66: ("Freezing rain", "🌧️"),
       67: ("Freezing rain", "🌧️"), 71: ("Light snow", "🌨️"), 73: ("Snow", "🌨️"), 75: ("Heavy snow", "❄️"),
       77: ("Snow grains", "🌨️"), 80: ("Light showers", "🌦️"), 81: ("Showers", "🌧️"), 82: ("Heavy showers", "⛈️"),
       85: ("Snow showers", "🌨️"), 86: ("Heavy snow showers", "❄️"), 95: ("Thunderstorm", "⛈️"),
       96: ("Thunderstorm with hail", "⛈️"), 99: ("Thunderstorm with hail", "⛈️")}


def wmo(code):
    return WMO.get(code, ("Unknown", "❔"))


def fetch_weather(lat, lon):
    key = (round(lat, 2), round(lon, 2))
    now = time.time()
    hit = _weather_cache.get(key)
    if hit and now - hit[0] < 600:
        return hit[1]
    params = {
        "latitude": "%.4f" % lat, "longitude": "%.4f" % lon, "timezone": "auto", "forecast_days": 3,
        "wind_speed_unit": "kmh",
        "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_gusts_10m,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "precipitation_sum,wind_speed_10m_max,uv_index_max,sunrise,sunset",
    }
    try:
        r = requests.get(WEATHER_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=15)
        data = r.json()
    except (requests.RequestException, ValueError) as ex:
        raise RuntimeError(_short_error(WEATHER_URL, ex))
    if r.status_code != 200 or "current" not in data:
        raise RuntimeError("The weather service did not return a forecast.")
    cur, daily = data["current"], data.get("daily") or {}
    text, icon = wmo(cur.get("weather_code"))
    out = {"now": {"temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
                   "rain_mm": cur.get("precipitation"), "wind": cur.get("wind_speed_10m"),
                   "gust": cur.get("wind_gusts_10m"), "text": text, "icon": icon, "is_day": cur.get("is_day")},
           "days": [], "attribution": "Weather data by Open-Meteo.com"}
    dates = daily.get("time") or []
    for i, d in enumerate(dates):
        def at(k, i=i):
            v = daily.get(k) or []
            return v[i] if i < len(v) else None
        t, ic = wmo(at("weather_code"))
        out["days"].append({
            "date": d, "text": t, "icon": ic, "tmax": at("temperature_2m_max"), "tmin": at("temperature_2m_min"),
            "rain_prob": at("precipitation_probability_max"), "rain_mm": at("precipitation_sum"),
            "wind_max": at("wind_speed_10m_max"), "uv": at("uv_index_max"),
            "sunrise": (at("sunrise") or "")[-5:] or None, "sunset": (at("sunset") or "")[-5:] or None})
    _weather_cache[key] = (now, out)
    if len(_weather_cache) > 500:
        _weather_cache.clear()
    return out


@app.get("/api/weather")
def api_weather():
    try:
        lat, lon = request.args.get("lat", type=float), request.args.get("lon", type=float)
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError
    except ValueError:
        return jsonify(error="lat and lon are required"), 400
    if not rate_ok("weather", 60):
        return jsonify(error="Too many weather lookups just now. Try again in a minute."), 429
    try:
        return jsonify(fetch_weather(lat, lon))
    except RuntimeError as ex:
        return jsonify(error="Weather unavailable: %s" % ex), 502


# kind -> (label, icon, how far from the route it may be, in metres)
PLACE_KINDS = {
    "food": ("Food and coffee", "☕", 250), "water": ("Drinking water", "💧", 120),
    "toilets": ("Toilets", "🚻", 120), "rest": ("Rest spots", "🪑", 60), "view": ("Viewpoints", "👀", 120),
}
FOOD_AMENITIES = {"cafe": "Café", "restaurant": "Restaurant", "fast_food": "Takeaway", "food_court": "Food court",
                  "ice_cream": "Ice cream", "pub": "Pub", "bar": "Bar"}


def classify_place(tags):
    am, shop = tags.get("amenity"), tags.get("shop")
    if am in FOOD_AMENITIES:
        return "food", FOOD_AMENITIES[am]
    if shop in ("bakery", "convenience", "supermarket"):
        return "food", {"bakery": "Bakery", "convenience": "Dairy / convenience store", "supermarket": "Supermarket"}[shop]
    if am == "drinking_water":
        return "water", "Drinking water"
    if am == "toilets":
        return "toilets", "Toilets"
    if am == "shelter":
        return "rest", "Shelter"
    if am == "bench":
        return "rest", "Bench"
    if tags.get("leisure") == "picnic_table" or tags.get("tourism") == "picnic_site":
        return "rest", "Picnic spot"
    if tags.get("tourism") == "viewpoint":
        return "view", "Viewpoint"
    return None, None


def along_and_off(lat, lon, chain, cum):
    """Nearest point on a route: (metres along from the start, metres away from the line)."""
    kx, ky = math.cos(math.radians(lat)) * 111320.0, 110540.0
    best_off, best_along = float("inf"), 0.0
    for i in range(len(chain) - 1):
        ax, ay = (chain[i][1] - lon) * kx, (chain[i][0] - lat) * ky
        bx, by = (chain[i + 1][1] - lon) * kx, (chain[i + 1][0] - lat) * ky
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / l2)) if l2 else 0.0
        off = math.hypot(ax + t * dx, ay + t * dy)
        if off < best_off:
            best_off, best_along = off, cum[i] + t * (cum[i + 1] - cum[i])
    return best_along, best_off


def nearest_finder(chain, cum, reach_m):
    """A fast version of along_and_off for many points that only care about those within reach_m of the route:
    the route is cut into short stretches with a padded box each, so a point is only measured against stretches near it."""
    pad_y = reach_m / 110540.0
    stretches, step = [], 8
    for i in range(0, len(chain) - 1, step):
        part = chain[i:i + step + 1]
        la = [p[0] for p in part]; lo = [p[1] for p in part]
        pad_x = reach_m / (math.cos(math.radians(la[0])) * 111320.0)
        stretches.append((min(la) - pad_y, max(la) + pad_y, min(lo) - pad_x, max(lo) + pad_x, i, min(i + step + 1, len(chain))))

    def find(lat, lon):
        best_off, best_along = float("inf"), 0.0
        kx, ky = math.cos(math.radians(lat)) * 111320.0, 110540.0
        for la0, la1, lo0, lo1, a, b in stretches:
            if lat < la0 or lat > la1 or lon < lo0 or lon > lo1:
                continue
            for i in range(a, b - 1):
                ax, ay = (chain[i][1] - lon) * kx, (chain[i][0] - lat) * ky
                bx, by = (chain[i + 1][1] - lon) * kx, (chain[i + 1][0] - lat) * ky
                dx, dy = bx - ax, by - ay
                l2 = dx * dx + dy * dy
                t = max(0.0, min(1.0, -(ax * dx + ay * dy) / l2)) if l2 else 0.0
                off = math.hypot(ax + t * dx, ay + t * dy)
                if off < best_off:
                    best_off, best_along = off, cum[i] + t * (cum[i + 1] - cum[i])
        return best_along, best_off
    return find


def poi_saved():
    try:
        return db().execute("SELECT 1 FROM poi LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def places_local_ok(s_, w_, n_, e_):
    """True when the places read from the NZ file are saved and the walk lies inside New Zealand."""
    if not (NZ_BOX[0] <= s_ and n_ <= NZ_BOX[1] and NZ_BOX[2] <= w_ and e_ <= NZ_BOX[3]):
        return False
    try:                                                  # the saved places stay valid while a monthly rebuild runs
        return db().execute("SELECT 1 FROM poi LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def _places_overpass(bb):
    q = ("[out:json][timeout:25];("
         'nwr["amenity"~"^(cafe|restaurant|fast_food|food_court|ice_cream|pub|bar|drinking_water|toilets|shelter|bench)$"]%s;'
         'nwr["leisure"="picnic_table"]%s;nwr["tourism"~"^(picnic_site|viewpoint)$"]%s;'
         'nwr["shop"~"^(bakery|convenience|supermarket)$"]%s;);out tags center 1500;' % (bb, bb, bb, bb))
    for el in overpass(q).get("elements", []):
        tags = el.get("tags", {})
        kind, label = classify_place(tags)
        if not kind:
            continue
        lat = el.get("lat") if el.get("lat") is not None else (el.get("center") or {}).get("lat")
        lon = el.get("lon") if el.get("lon") is not None else (el.get("center") or {}).get("lon")
        yield ((el.get("type"), el.get("id")), kind, label, tags.get("name"), lat, lon,
               tags.get("opening_hours"), tags.get("website") or tags.get("contact:website"))


def _places_local(s_, w_, n_, e_):
    for r in db().execute("SELECT id, kind, label, name, lat, lon, hours, website FROM poi "
                          "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?", (s_, n_, w_, e_)):
        yield (("poi", r["id"]), r["kind"], r["label"], r["name"], r["lat"], r["lon"], r["hours"], r["website"])


def fetch_places(chain):
    """Food, water, toilets, rest spots and viewpoints near a route: from Walkbox's own copy of the NZ file when it
    has been read, otherwise (outside NZ, or before the store is built) from the live OpenStreetMap servers."""
    ch = simplify(chain, 400)
    pad = 0.0025
    s_, n_ = min(p[0] for p in ch) - pad, max(p[0] for p in ch) + pad
    w_, e_ = min(p[1] for p in ch) - pad, max(p[1] for p in ch) + pad
    cum = [0.0]
    for i in range(1, len(ch)):
        cum.append(cum[-1] + haversine_m(ch[i - 1], ch[i]))
    total_km = cum[-1] / 1000.0
    local = places_local_ok(s_, w_, n_, e_)
    cands = _places_local(s_, w_, n_, e_) if local else _places_overpass("(%f,%f,%f,%f)" % (s_, w_, n_, e_))
    out, seen = [], set()
    near = nearest_finder(ch, cum, max(v[2] for v in PLACE_KINDS.values()))
    for key, kind, label, name, lat, lon, hours, website in cands:
        if lat is None or lon is None:
            continue
        along, off = near(lat, lon)
        if off > PLACE_KINDS[kind][2]:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "label": label, "name": name, "lat": round(lat, 6), "lon": round(lon, 6),
                    "off_m": int(round(off)), "along_km": round(along / 1000.0, 2),
                    "hours": hours, "website": website})
    out.sort(key=lambda x: x["along_km"])
    per_kind = {}
    trimmed = []
    for pl in out:                      # keep the list readable: at most 40 of each kind
        per_kind[pl["kind"]] = per_kind.get(pl["kind"], 0) + 1
        if per_kind[pl["kind"]] <= 40:
            trimmed.append(pl)
    counts = {k: sum(1 for x in out if x["kind"] == k) for k in PLACE_KINDS}
    return {"places": trimmed, "counts": counts, "total_km": round(total_km, 2), "from": "store" if local else "live",
            "kinds": {k: {"label": v[0], "icon": v[1]} for k, v in PLACE_KINDS.items()}}


@app.get("/api/walks/<int:osm_id>/places")
def api_places(osm_id):
    try:
        chain, _ = main_chain(osm_id)
    except RuntimeError as ex:
        return jsonify(error=str(ex)), 502
    saved = kv_get("places", "%s:%s" % (osm_id, len(chain)))
    if saved:
        return jsonify(saved)
    if not rate_ok("places", 20):
        return jsonify(error="Too many lookups just now. Try again in a minute."), 429
    try:
        res = fetch_places(chain)
    except Exception as ex:
        app.logger.warning("places lookup failed: %r", ex)
        hint = "" if poi_saved() else " Walkbox's own places are not saved yet: rebuild the OpenStreetMap store on the Data tab to make this instant."
        return jsonify(error="Nearby places unavailable: %s.%s" % (ex if isinstance(ex, RuntimeError) else "unexpected answer from the map server", hint)), 502
    kv_put("places", "%s:%s" % (osm_id, len(chain)), res)
    return jsonify(res)


# --------------------------------------------------------------------------
# Photos (Wikimedia Commons, free, no key) and personal rating/notes
# --------------------------------------------------------------------------
def rate_ok(kind, limit):
    now = time.time()
    with _rate_lock:
        calls = [t for t in _rate_calls.get(kind, []) if now - t < 60]
        ok = len(calls) < limit
        if ok:
            calls.append(now)
        _rate_calls[kind] = calls
        return ok


def _plain(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def commons_photos(chain, limit=12):
    """Openly licensed, geotagged photos near the start, middle and end of a walk."""
    pts = list(dict.fromkeys(tuple(x) for x in (chain[0], chain[len(chain) // 2], chain[-1])))     # one request per distinct place
    seen, found = set(), []
    for order, (lat, lon) in enumerate(pts):
        params = {
            "action": "query", "format": "json", "generator": "geosearch",
            "ggscoord": "%.5f|%.5f" % (lat, lon), "ggsradius": 800, "ggsnamespace": 6, "ggslimit": 10,
            "prop": "imageinfo", "iiprop": "url|mime|size|extmetadata", "iiurlwidth": 400,
            "iiextmetadatafilter": "Artist|LicenseShortName|ImageDescription",
        }
        try:
            r = requests.get(COMMONS_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
            pages = (r.json().get("query") or {}).get("pages") or {}
        except (requests.RequestException, ValueError) as ex:
            raise RuntimeError(_short_error(COMMONS_URL, ex))
        for pg in pages.values():
            info = (pg.get("imageinfo") or [None])[0]
            if not info or pg.get("pageid") in seen:
                continue
            thumb = info.get("thumburl") or ""
            host = thumb.split("/")[2] if thumb.startswith("https://") else ""
            if host != "upload.wikimedia.org" or info.get("mime") not in ("image/jpeg", "image/png"):
                continue
            if (info.get("width") or 0) < 500:
                continue
            meta = info.get("extmetadata") or {}
            seen.add(pg.get("pageid"))
            found.append((pg.get("index", 99), order, {
                "thumb": thumb, "page": info.get("descriptionurl"),
                "title": _plain(pg.get("title", "")).replace("File:", "")[:120],
                "caption": _plain((meta.get("ImageDescription") or {}).get("value"))[:160],
                "by": _plain((meta.get("Artist") or {}).get("value"))[:80] or "Unknown",
                "license": _plain((meta.get("LicenseShortName") or {}).get("value")) or "See page",
            }))
    found.sort(key=lambda x: (x[0], x[1]))
    return [f[2] for f in found[:limit]]


@app.get("/api/walks/<int:osm_id>/photos")
def api_photos(osm_id):
    row = db().execute("SELECT geom FROM walks WHERE osm_id=?", (osm_id,)).fetchone()
    if row:
        chain = json.loads(row["geom"])[0]
    else:                                                    # a walk that is only open on screen: its route is in the cache
        wk = cached_walk(osm_id)
        if not wk or not wk.get("chains"):
            abort(404)
        chain = wk["chains"][0]
    hit = kv_get("photos", osm_id)
    if hit is not None:
        return jsonify(photos=hit)
    if not rate_ok("photos", PHOTOS_PER_MIN):
        return jsonify(error="Too many photo lookups just now. Try again in a minute."), 429
    try:
        found = commons_photos(chain)
    except RuntimeError as ex:
        return jsonify(error="Photo service problem: %s" % ex), 502
    kv_put("photos", osm_id, found)
    return jsonify(photos=found)


@app.get("/api/photo_near")
def api_photo_near():
    """One picture for a result card: the best openly licensed photo near the walk's start."""
    try:
        lat, lon = float(request.args["lat"]), float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify(error="lat and lon are required"), 400
    key = "%.3f,%.3f" % (lat, lon)
    hit = kv_get("photos", "near:" + key)
    if hit is None:
        if not rate_ok("cardphoto", 90):
            return jsonify(photo=None)
        try:
            found = commons_photos([(lat, lon)] * 3, limit=1)
        except RuntimeError:
            return jsonify(photo=None)
        hit = found[:1]
        kv_put("photos", "near:" + key, hit)
    return jsonify(photo=hit[0] if hit else None)


def sample_profile(chain, n=160):
    """Evenly spaced points along a route, with their distance (m) from the start."""
    cum = [0.0]
    for i in range(1, len(chain)):
        cum.append(cum[-1] + haversine_m(chain[i - 1], chain[i]))
    total = cum[-1]
    if total <= 0:
        raise RuntimeError("This walk has no length.")
    n = max(2, min(n, int(total / 10) + 2))
    pts, j = [], 0
    for k in range(n):
        d = total * k / (n - 1)
        while j < len(cum) - 2 and cum[j + 1] < d:
            j += 1
        seg = (cum[j + 1] - cum[j]) or 1.0
        t = min(1.0, max(0.0, (d - cum[j]) / seg))
        pts.append((chain[j][0] + t * (chain[j + 1][0] - chain[j][0]),
                    chain[j][1] + t * (chain[j + 1][1] - chain[j][1]), d))
    return pts, total


def fetch_elevations(pts):
    elev = []
    for i in range(0, len(pts), 100):
        batch = pts[i:i + 100]
        try:
            r = requests.get(
                ELEVATION_URL,
                params={"latitude": ",".join("%.5f" % p[0] for p in batch),
                        "longitude": ",".join("%.5f" % p[1] for p in batch)},
                headers={"User-Agent": USER_AGENT}, timeout=20)
            got = r.json().get("elevation") if r.status_code == 200 else None
        except (requests.RequestException, ValueError) as ex:
            raise RuntimeError(_short_error(ELEVATION_URL, ex))
        if not got or len(got) != len(batch) or any(e is None for e in got):
            raise RuntimeError("The elevation service did not return data.")
        elev += got
    return elev


def build_profile(chain):
    pts, total = sample_profile(chain)
    elev = fetch_elevations(pts)
    return {"total_km": round(total / 1000.0, 3), "dist_km": [round(p[2] / 1000.0, 3) for p in pts],
            "elev_m": [round(e, 1) for e in elev], "min": round(min(elev), 1), "max": round(max(elev), 1)}


def profile_for(osm_id, chain, in_box):
    """The elevation profile of a walk, from the cache when there is one. May raise RuntimeError."""
    row = db().execute("SELECT profile FROM walks WHERE osm_id=?", (osm_id,)).fetchone() if in_box else None
    if row and row["profile"]:
        return json.loads(row["profile"])
    if not in_box:
        saved = kv_get("profile", "%s:%s" % (osm_id, len(chain)))
        if saved:
            return saved
    prof = build_profile(chain)
    if in_box:
        conn = db()
        conn.execute("UPDATE walks SET profile=? WHERE osm_id=?", (json.dumps(prof), osm_id))
        conn.commit()
    else:
        kv_put("profile", "%s:%s" % (osm_id, len(chain)), prof)
    return prof


@app.get("/api/walks/<int:osm_id>/profile")
def api_profile(osm_id):
    row = db().execute("SELECT profile FROM walks WHERE osm_id=?", (osm_id,)).fetchone()
    if row and row["profile"]:
        return jsonify(json.loads(row["profile"]))
    try:
        chain, in_box = main_chain(osm_id)
    except RuntimeError as ex:
        return jsonify(error=str(ex)), 502
    if not in_box and kv_get("profile", "%s:%s" % (osm_id, len(chain))):
        return jsonify(kv_get("profile", "%s:%s" % (osm_id, len(chain))))
    if not rate_ok("profile", 60):
        return jsonify(error="Too many elevation lookups just now. Try again in a minute."), 429
    try:
        return jsonify(profile_for(osm_id, chain, in_box))
    except RuntimeError as ex:
        return jsonify(error="Elevation profile unavailable: %s" % ex), 502


@app.put("/api/walks/<int:osm_id>/done")
def api_done(osm_id):
    done = bool((request.get_json(silent=True) or {}).get("done", True))
    conn = db()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds") if done else None
    cur = conn.execute("UPDATE walks SET done=?, done_at=? WHERE osm_id=?", (1 if done else 0, stamp, osm_id))
    conn.commit()
    if not cur.rowcount:
        return jsonify(error="Add the walk to your box first"), 404
    return jsonify(ok=True, done=done, done_at=stamp)


@app.put("/api/walks/<int:osm_id>/note")
def api_note(osm_id):
    b = request.get_json(silent=True) or {}
    rating = b.get("rating")
    if rating is not None:
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            return jsonify(error="Rating must be 1 to 5"), 400
        if not 1 <= rating <= 5:
            return jsonify(error="Rating must be 1 to 5"), 400
    note = str(b.get("note") or "").strip()[:2000] or None
    conn = db()
    cur = conn.execute("UPDATE walks SET my_rating=?, my_note=? WHERE osm_id=?", (rating, note, osm_id))
    conn.commit()
    if not cur.rowcount:
        abort(404)
    return jsonify(ok=True, my_rating=rating, my_note=note)


# --------------------------------------------------------------------------
# Surface of the route (from OpenStreetMap) and an estimated difficulty rating
# --------------------------------------------------------------------------
SURFACES = {   # class: (label, colour)
    "paved": ("Paved", "#37474f"), "gravel": ("Gravel", "#a1887f"), "dirt": ("Dirt", "#8d6e63"),
    "grass": ("Grass", "#7cb342"), "sand": ("Sand", "#e6b85c"), "rock": ("Rock", "#8a8f94"),
    "wood": ("Boardwalk", "#c58b5a"), "steps": ("Steps", "#d05a5a"), "unknown": ("Unknown", "#f2f2f2"),
}
SURFACE_OF = {}
for _cls, _vals in {
    "paved": "asphalt paved concrete concrete:plates concrete:lanes paving_stones sett cobblestone unhewn_cobblestone bricks brick metal tartan rubber acrylic",
    "gravel": "gravel fine_gravel compacted pebblestone shingle crushed_limestone woodchips",
    "dirt": "dirt ground earth mud soil unpaved clay",
    "grass": "grass grass_paver",
    "sand": "sand",
    "rock": "rock bare_rock stone scree",
    "wood": "wood boardwalk planks",
}.items():
    for _v in _vals.split():
        SURFACE_OF[_v] = _cls
FOOT_HIGHWAYS = {"footway", "path", "steps", "pedestrian", "track", "bridleway", "cycleway"}
PAVED_ROADS = {"residential", "service", "unclassified", "living_street", "tertiary", "secondary", "primary", "road", "tertiary_link"}
SAC_POINTS = {"hiking": 0.0, "mountain_hiking": 1.0, "demanding_mountain_hiking": 2.0, "alpine_hiking": 2.5,
              "demanding_alpine_hiking": 3.0, "difficult_alpine_hiking": 3.0}
ROUGHNESS = {"rock": 1.0, "steps": 1.0, "sand": 0.6, "dirt": 0.3, "grass": 0.3, "gravel": 0.15, "wood": 0.0, "paved": 0.0, "unknown": 0.0}


def surface_class(tags):
    hw = tags.get("highway")
    if hw == "steps":
        return "steps"
    surf = (tags.get("surface") or "").split(";")[0].strip().lower()
    if tags.get("bridge") == "boardwalk" and not surf:
        return "wood"
    if surf in SURFACE_OF:
        return SURFACE_OF[surf]
    if surf in ("unpaved", "ground"):
        return "dirt"
    if not surf and hw in PAVED_ROADS:
        return "paved"                                   # ordinary streets with no surface tag are paved
    return "unknown"


def fetch_surface_ways(chain):
    """OSM ways along a route: their tags and geometry."""
    ch = simplify(chain, 220)
    total_m = chain_length_m(chain)
    spacing = total_m / max(1, len(ch) - 1)
    radius = int(min(60, max(25, spacing * 0.45)))
    coords = ",".join("%.5f,%.5f" % (p[0], p[1]) for p in ch)
    q = ('[out:json][timeout:25];way(around:%d,%s)["highway"]'
         '["highway"!~"^(motorway|motorway_link|trunk|trunk_link|proposed|construction|raceway|bus_guideway)$"];'
         'out tags geom 4000;' % (radius, coords))
    ways = []
    for el in overpass(q).get("elements", []):
        if el.get("type") == "way" and el.get("geometry"):
            ways.append({"tags": el.get("tags", {}), "pts": [(p["lat"], p["lon"]) for p in el["geometry"]]})
    return ways, radius


def match_surfaces(chain, ways, max_off=22.0, step_m=8.0):
    """Give every stretch of the route a surface class by finding the nearest mapped way. Returns runs as fractions of the route."""
    cum = [0.0]
    for i in range(1, len(chain)):
        cum.append(cum[-1] + haversine_m(chain[i - 1], chain[i]))
    total = cum[-1]
    if total <= 0:
        return [], {}, {"sac": None}
    lat0, lon0 = chain[0]
    kx, ky = math.cos(math.radians(lat0)) * 111320.0, 110540.0
    proj = lambda p: ((p[1] - lon0) * kx, (p[0] - lat0) * ky)
    cell = 40.0
    grid, meta = {}, []
    for wi, w in enumerate(ways):
        cls = surface_class(w["tags"])
        foot = w["tags"].get("highway") in FOOT_HIGHWAYS
        meta.append((cls, foot, w["tags"].get("sac_scale")))
        xy = [proj(p) for p in w["pts"]]
        for a, b in zip(xy, xy[1:]):
            for cx in range(int(min(a[0], b[0]) // cell), int(max(a[0], b[0]) // cell) + 1):
                for cy in range(int(min(a[1], b[1]) // cell), int(max(a[1], b[1]) // cell) + 1):
                    grid.setdefault((cx, cy), []).append((wi, a, b))
    n = max(2, int(total / step_m))
    samples, j = [], 0
    for k in range(n):
        d = total * k / (n - 1)
        while j < len(cum) - 2 and cum[j + 1] < d:
            j += 1
        seg = (cum[j + 1] - cum[j]) or 1.0
        t = min(1.0, max(0.0, (d - cum[j]) / seg))
        samples.append(proj((chain[j][0] + t * (chain[j + 1][0] - chain[j][0]), chain[j][1] + t * (chain[j + 1][1] - chain[j][1]))))
    classes, sacs = [], []
    for x, y in samples:
        best, best_i = None, None
        for cx in range(int(x // cell) - 1, int(x // cell) + 2):
            for cy in range(int(y // cell) - 1, int(y // cell) + 2):
                for wi, a, b in grid.get((cx, cy), ()):
                    dx, dy = b[0] - a[0], b[1] - a[1]
                    l2 = dx * dx + dy * dy
                    t = max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / l2)) if l2 else 0.0
                    dist = math.hypot(a[0] + t * dx - x, a[1] + t * dy - y)
                    score = dist + (0.0 if meta[wi][1] else 6.0)      # a footpath beats a road beside it
                    if dist <= max_off and (best is None or score < best):
                        best, best_i = score, wi
        classes.append(meta[best_i][0] if best_i is not None else "unknown")
        sacs.append(meta[best_i][2] if best_i is not None else None)
    # runs; very short blips are absorbed by their neighbours
    runs = []
    for k, c in enumerate(classes):
        if runs and runs[-1][0] == c:
            runs[-1][2] = k + 1
        else:
            runs.append([c, k, k + 1])
    min_len = max(2, int(15.0 / step_m))
    merged = []
    for r in runs:
        if merged and (r[2] - r[1] < min_len):
            merged[-1][2] = r[2]
        else:
            merged.append(r)
    out = []
    for c, a, b in merged:
        if out and out[-1][0] == c:
            out[-1][2] = b
        else:
            out.append([c, a, b])
    runs_f = [{"cls": c, "a": round(a / n, 4), "b": round(min(1.0, b / n), 4)} for c, a, b in out]
    if runs_f:
        runs_f[0]["a"], runs_f[-1]["b"] = 0.0, 1.0
    totals = {}
    for r in runs_f:
        totals[r["cls"]] = totals.get(r["cls"], 0.0) + (r["b"] - r["a"]) * total
    best_sac = max((x for x in sacs if x in SAC_POINTS), key=lambda x: SAC_POINTS[x], default=None)
    return runs_f, totals, {"sac": best_sac}


def max_grade_pct(profile):
    """Steepest stretch of about 100 m or more, in percent (the elevation data is coarse, so this is approximate)."""
    d, e = profile["dist_km"], profile["elev_m"]
    best = 0.0
    for i in range(len(d)):
        for j in range(i + 1, len(d)):
            run = (d[j] - d[i]) * 1000.0
            if run >= 100.0:
                best = max(best, abs(e[j] - e[i]) / run * 100.0)
                break
    return min(best, 45.0)


GRADE_WORDS = [("easiest", 1.0), ("path", 1.0), ("easy", 1.5), ("short walk", 1.5), ("walking track", 3.0),
               ("intermediate", 4.5), ("moderate", 4.5), ("tramping track", 5.5), ("advanced", 6.5), ("hard", 6.5),
               ("challenging", 8.0), ("route", 8.0), ("expert", 8.5), ("extreme", 9.0)]


def difficulty_rating(km, ascent_m, grade_pct, totals, total_m, sac=None, source_grade=None):
    """A 0-10 estimate from distance, climbing, steepness, rough surfaces and any official grade."""
    reasons = []
    dist_pts = min(3.0, km / 4.0)
    climb_pts = min(3.0, (ascent_m or 0) / 100.0)
    steep_pts = 2.5 if grade_pct >= 30 else 1.5 if grade_pct >= 20 else 0.5 if grade_pct >= 12 else 0.0
    rough = sum(ROUGHNESS.get(c, 0.0) * m for c, m in totals.items()) / total_m if total_m else 0.0
    rough_pts = min(2.0, rough * 3.0)
    sac_pts = SAC_POINTS.get(sac, 0.0)
    score = dist_pts + climb_pts + steep_pts + rough_pts + sac_pts
    reasons.append("%.1f km long" % km)
    reasons.append("%d m of climbing%s" % (round(ascent_m or 0), (", steepest about %d%%" % round(grade_pct)) if grade_pct >= 8 else ""))
    known = {c: m for c, m in totals.items() if c != "unknown"}
    if known and total_m and sum(known.values()) / total_m > 0.3:
        top = sorted(known.items(), key=lambda kv: -kv[1])[:2]
        reasons.append("surface: " + ", ".join("%s %d%%" % (SURFACES[c][0].lower(), round(100 * m / total_m)) for c, m in top))
    if sac and sac_pts:
        reasons.append("marked as %s in OpenStreetMap" % sac.replace("_", " "))
    if source_grade:
        floor = max([v for w, v in GRADE_WORDS if w in source_grade.lower()] or [0.0])
        if floor:
            reasons.append("official grade: %s" % source_grade)
            score = max(score, floor)
    score = round(min(10.0, score), 1)
    label = "Easy" if score < 3.5 else "Moderate" if score < 6.0 else "Hard" if score < 8.0 else "Very hard"
    return {"label": label, "score": score, "reasons": reasons, "max_grade_pct": round(grade_pct, 1), "ascent_m": round(ascent_m or 0)}


def build_terrain(chain, source_grade, known_ascent=None, cached_profile=None, surfaces=True):
    notes = []
    total_m = chain_length_m(chain)
    if not surfaces:                                   # only the difficulty from distance, climbing and grade: nothing is known about the surface yet
        runs, totals, extra = [], {}, {"sac": None}
    else:
        try:
            ways, _ = fetch_surface_ways(chain)
            runs, totals, extra = match_surfaces(chain, ways)
        except Exception as ex:                       # whatever went wrong, the walk still opens with an honest note
            app.logger.warning("surface lookup failed: %r", ex)
            runs, totals, extra = [{"cls": "unknown", "a": 0.0, "b": 1.0}], {"unknown": total_m}, {"sac": None}
            notes.append("Surface data unavailable: %s" % (ex if isinstance(ex, RuntimeError) else "unexpected answer from the map server"))
    prof = cached_profile
    if prof is None:
        try:
            prof = build_profile(simplify(chain, 1500))
        except RuntimeError as ex:
            notes.append("Elevation unavailable: %s" % ex)
    if prof:
        asc = ascentOf_py(prof["elev_m"])
        grade = max_grade_pct(prof)
    else:
        asc, grade = (known_ascent or 0), 0.0
    diff = difficulty_rating(total_m / 1000.0, asc, grade, totals, total_m, extra["sac"], source_grade)
    classes = {c: {"label": SURFACES[c][0], "colour": SURFACES[c][1], "m": round(m), "pct": round(100.0 * m / total_m) if total_m else 0}
               for c, m in totals.items()}
    return {"runs": runs, "classes": classes, "total_m": round(total_m), "difficulty": diff, "notes": notes, "partial": not surfaces,
            "attribution": "Surfaces from OpenStreetMap (ODbL). Where a path is not tagged the colour is white (unknown)."}


def ascentOf_py(elev):
    return sum(d for d in (b - a for a, b in zip(elev, elev[1:])) if d > 1.0)


@app.get("/api/walks/<int:osm_id>/terrain")
def api_terrain(osm_id):
    row = db().execute("SELECT terrain, profile, difficulty, geom FROM walks WHERE osm_id=?", (osm_id,)).fetchone()
    if row and row["terrain"]:
        return jsonify(json.loads(row["terrain"]))
    try:
        chain, in_box = main_chain(osm_id)
    except RuntimeError as ex:
        return jsonify(error=str(ex)), 502
    key = "%s:%s" % (osm_id, len(chain))
    if not in_box:
        saved = kv_get("terrain", key)
        if saved:
            return jsonify(saved)
    cached_wk = cached_walk(osm_id) or {}
    grade = (row["difficulty"] if row else cached_wk.get("difficulty"))
    prof = json.loads(row["profile"]) if row and row["profile"] else kv_get("profile", key)
    if request.args.get("quick") != "1" and not rate_ok("terrain", 20):
        return jsonify(error="Too many lookups just now. Try again in a minute."), 429
    if request.args.get("quick") == "1":              # the difficulty from distance, climbing and grade alone: no OpenStreetMap needed
        if prof is None:
            try:
                prof = profile_for(osm_id, chain, in_box)
            except RuntimeError:
                prof = None
        return jsonify(build_terrain(chain, grade, cached_profile=prof, surfaces=False))
    res = build_terrain(chain, grade, cached_profile=prof)
    if in_box and not res["notes"]:
        conn = db()
        conn.execute("UPDATE walks SET terrain=? WHERE osm_id=?", (json.dumps(res), osm_id))
        conn.commit()
    elif not in_box and not res["notes"]:
        kv_put("terrain", key, res)
    return jsonify(res)


# --------------------------------------------------------------------------
# Customised walks (edited in the browser, saved as a new walk or over the original)
# --------------------------------------------------------------------------
def _chain_from(v, limit=6000):
    chain = [(float(p[0]), float(p[1])) for p in v]
    if not (2 <= len(chain) <= limit) or any(not (-90 <= a <= 90 and -180 <= o <= 180) for a, o in chain):
        raise ValueError
    return chain


@app.post("/api/profile")
def api_profile_chain():
    """Elevation profile for a route that is being edited (not saved yet)."""
    try:
        chain = _chain_from((request.get_json(silent=True) or {})["chain"])
    except (KeyError, TypeError, ValueError, IndexError):
        return jsonify(error="Invalid route"), 400
    if chain_length_m(chain) < 20:
        return jsonify(error="The route is too short for a profile."), 400
    if not rate_ok("profile", 60):
        return jsonify(error="Too many elevation lookups just now. Try again in a minute."), 429
    try:
        return jsonify(build_profile(simplify(chain, 1500)))
    except RuntimeError as ex:
        return jsonify(error="Elevation profile unavailable: %s" % ex), 502


@app.post("/api/customise")
def api_customise():
    b = request.get_json(silent=True) or {}
    try:
        chain = _chain_from(b["chain"])
        base_id = int(b["base_id"])
    except (KeyError, TypeError, ValueError, IndexError):
        return jsonify(error="Invalid route"), 400
    if chain_length_m(chain) < 20:
        return jsonify(error="The route is too short to save."), 400
    mode = b.get("mode")
    if mode not in ("new", "replace"):
        return jsonify(error="mode must be new or replace"), 400
    rating = b.get("rating")
    if rating is not None:
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            return jsonify(error="Rating must be 1 to 5"), 400
        if not 1 <= rating <= 5:
            return jsonify(error="Rating must be 1 to 5"), 400
    name = str(b.get("name") or "").strip()[:200] or "Custom walk"
    desc = str(b.get("description") or "").strip()[:2000] or None
    diff = str(b.get("difficulty") or "").strip()[:40] or None
    note = str(b.get("note") or "").strip()[:2000] or None
    conn = db()
    base = conn.execute("SELECT * FROM walks WHERE osm_id=?", (base_id,)).fetchone()
    cached = cached_walk(base_id) or {}
    if mode == "replace" and not base:
        return jsonify(error="Only a walk that is already in your box can be replaced. Save it as a new walk instead."), 400
    if base:
        base_tags = json.loads(base["tags"] or "{}")
        base_source = (base_tags.get("base_source") if base["source"] == "custom" else base["source"]) or "osm-route"
        base_name, link, region, operator = base["name"], base["link"], base["region"], base["operator"]
    else:
        base_source = cached.get("source") or "osm-route"
        base_name, link, region, operator = cached.get("name"), cached.get("link"), cached.get("region"), cached.get("operator")
    pts = chain
    lat, lon = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    km = round(chain_length_m(chain) / 1000.0, 2)
    rtype = route_type_of([chain])
    ascent = elevation_gain(chain)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if mode == "replace":
        tags = dict(base_tags)
        tags["customised"] = True
        tags["base_source"] = base_source
        conn.execute(
            "UPDATE walks SET name=?, description=?, difficulty=?, distance_km=?, lat=?, lon=?, geom=?, tags=?,"
            " route_type=?, ascent_m=?, profile=NULL, terrain=NULL, time_text=NULL, my_rating=?, my_note=? WHERE osm_id=?",
            (name, desc, diff, km, lat, lon, json.dumps([chain]), json.dumps(tags), rtype, ascent, rating, note, base_id))
        wid = base_id
    else:
        wid = CUSTOM_BASE + int(time.time() * 1000) % 10 ** 9
        while conn.execute("SELECT 1 FROM walks WHERE osm_id=?", (wid,)).fetchone():
            wid += 1
        tags = {"base_source": base_source, "customised_from": base_id, "base_name": base_name}
        conn.execute(
            "INSERT INTO walks (osm_id,name,ref,network,operator,description,distance_km,lat,lon,place,tags,geom,added_at,"
            "source,difficulty,route_type,ascent_m,link,region,time_text,my_rating,my_note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wid, name, None, None, operator, desc, km, lat, lon, None, json.dumps(tags), json.dumps([chain]), now,
             "custom", diff, rtype, ascent, link, region, None, rating, note))
    conn.commit()
    d = dict(conn.execute("SELECT * FROM walks WHERE osm_id=?", (wid,)).fetchone())
    d["chains"] = json.loads(d["geom"])
    return jsonify(public(d, True), ), 200


# --------------------------------------------------------------------------
# GPX import
# --------------------------------------------------------------------------
def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_gpx_bytes(data, filename):
    """Parse a GPX file you downloaded yourself into a walk dict."""
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("unsafe XML (DOCTYPE/ENTITY) is not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as ex:
        raise ValueError("not a valid GPX file (%s)" % ex)
    if _local(root.tag) != "gpx":
        raise ValueError("not a GPX file")
    name = desc = None
    chains, eles = [], []

    def text_of(el, child):
        for c in el:
            if _local(c.tag) == child and c.text and c.text.strip():
                return c.text.strip()
        return None

    for el in root:
        tag = _local(el.tag)
        if tag == "metadata":
            name = name or text_of(el, "name")
            desc = desc or text_of(el, "desc")
        elif tag == "trk":
            name = name or text_of(el, "name")
            desc = desc or text_of(el, "desc")
            for seg in el:
                if _local(seg.tag) != "trkseg":
                    continue
                pts, ele = [], []
                for pt in seg:
                    if _local(pt.tag) != "trkpt":
                        continue
                    try:
                        pts.append((float(pt.get("lat")), float(pt.get("lon"))))
                    except (TypeError, ValueError):
                        continue
                    ev = text_of(pt, "ele")
                    try:
                        ele.append(float(ev) if ev is not None else None)
                    except ValueError:
                        ele.append(None)
                if len(pts) >= 2:
                    chains.append(pts)
                    eles.append(ele)
    if not chains:  # fall back to planned routes
        for el in root:
            if _local(el.tag) == "rte":
                name = name or text_of(el, "name")
                pts = []
                for pt in el:
                    if _local(pt.tag) == "rtept":
                        try:
                            pts.append((float(pt.get("lat")), float(pt.get("lon"))))
                        except (TypeError, ValueError):
                            pass
                if len(pts) >= 2:
                    chains.append(pts)
                    eles.append([None] * len(pts))
    if not chains:
        raise ValueError("no track or route points found")
    order = sorted(range(len(chains)), key=lambda i: chain_length_m(chains[i]), reverse=True)
    chains = [chains[i] for i in order]
    eles = [eles[i] for i in order]
    name = name or re.sub(r"\.gpx$", "", filename or "Imported walk", flags=re.I) or "Imported walk"
    first = chains[0][0]
    wid = IMPORT_BASE + zlib.crc32(("%s|%.5f|%.5f|%d" % (name, first[0], first[1], len(chains[0]))).encode("utf-8"))
    wk = make_walk(wid, "import", name[:200], chains, {}, description=desc)
    top = eles[0]
    if top and sum(1 for e in top if e is not None) >= 0.8 * len(top):
        vals = [e for e in top if e is not None]
        wk["ascent_m"] = round(sum(b - a for a, b in zip(vals, vals[1:]) if b - a > 1.0), 0)
    return wk


@app.post("/api/import")
def api_import():
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="Choose one or more .gpx files"), 400
    conn = db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added, skipped, errors = 0, 0, []
    for f in files[:50]:
        data = f.read(MAX_IMPORT_BYTES + 1)
        if len(data) > MAX_IMPORT_BYTES:
            errors.append("%s: file is too large" % f.filename)
            continue
        try:
            wk = parse_gpx_bytes(data, f.filename)
        except ValueError as ex:
            errors.append("%s: %s" % (f.filename, ex))
            continue
        if wk["ascent_m"] is None:
            wk["ascent_m"] = elevation_gain(wk["chains"][0])
        cur = conn.execute(
            "INSERT OR IGNORE INTO walks (osm_id,name,ref,network,operator,description,distance_km,"
            "lat,lon,place,tags,geom,added_at,source,difficulty,route_type,ascent_m,link,region,time_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wk["osm_id"], wk["name"], None, None, None, wk["description"], wk["distance_km"],
             wk["lat"], wk["lon"], None, "{}", json.dumps(wk["chains"]), now, "import", None,
             wk["route_type"], wk["ascent_m"], None, None, None),
        )
        if cur.rowcount:
            added += 1
        else:
            skipped += 1
    conn.commit()
    return jsonify(added=added, skipped=skipped, errors=errors)


@app.delete("/api/walks/<int:osm_id>")
def api_delete(osm_id):
    conn = db()
    conn.execute("DELETE FROM walks WHERE osm_id=?", (osm_id,))
    conn.commit()
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# One-off NZ downloads (the "Data" tab)
# --------------------------------------------------------------------------
# Items Walkbox downloads itself, in the order they run. The first two are small and quick.
SEED_RUN_ORDER = ["store_doc", "store_nz", "osm_extract", "store_osm"]      # the OSM store is built from the extract, so the file comes first
SEED_LABELS = {
    "osm_extract": ("Raw OSM extract", "The whole-NZ OpenStreetMap file (about 400 MB), saved to the data folder. Fetched again every month."),
    "valhalla": ("Valhalla routing tiles", "Built inside the walkbox-valhalla container (about 1–2 GB). Walkbox shows its progress."),
    "overpass_db": ("Self-hosted Overpass database", "Optional: only shown when OVERPASS_LOCAL_URL points at your own Overpass container."),
    "store_osm": ("Walkbox's own store: OpenStreetMap walks", "Routes and named paths for all NZ, plus cafés, toilets, water and other places along them, built from the extract above and kept in Walkbox's database (about 100 MB). Rebuilt every month."),
    "store_doc": ("DOC walking experiences", "Every DOC walk with its route (about 5 MB)."),
    "store_nz": ("National tracks (council and other)", "Every council and other national track with its route (about 60 MB)."),
}
SEED_SHOW_ORDER = ["osm_extract", "valhalla", "overpass_db", "store_osm", "store_doc", "store_nz"]
SEED_SERVICES = ("valhalla", "overpass_db")
_seed_thread_lock = threading.Lock()
_seed_thread = {"t": None}


class SeedCancelled(Exception):
    pass


def raw_db():
    """A database connection for the background worker (outside any web request)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def seed_set(conn, item, status=None, done=None, total=None, msg=None):
    row = conn.execute("SELECT * FROM seed_state WHERE item=?", (item,)).fetchone()
    cur = dict(row) if row else {"status": "idle", "done": 0, "total": 0, "msg": "", "finished": None}
    cur["status"] = status or cur["status"]
    cur["done"] = cur["done"] if done is None else done
    cur["total"] = cur["total"] if total is None else total
    cur["msg"] = cur["msg"] if msg is None else msg
    fin = time.time() if cur["status"] in ("done", "error", "cancelled") else cur["finished"]
    conn.execute("INSERT OR REPLACE INTO seed_state (item,status,done,total,msg,finished,updated) VALUES (?,?,?,?,?,?,?)",
                 (item, cur["status"], cur["done"], cur["total"], cur["msg"], fin, time.time()))
    conn.commit()


def seed_status(conn, item):
    row = conn.execute("SELECT status FROM seed_state WHERE item=?", (item,)).fetchone()
    return row["status"] if row else None


def seed_check(conn, item, done=None, total=None, msg=None):
    """Record progress; stop if the person pressed Cancel."""
    if seed_status(conn, item) == "cancelling":
        raise SeedCancelled()
    seed_set(conn, item, done=done, total=total, msg=msg)


def _arcgis_post(url, params, label, tries=3):
    last = None
    for attempt in range(tries):
        try:
            r = requests.post(url, data=params, headers={"User-Agent": USER_AGENT}, timeout=90)
            data = r.json()
            if r.status_code == 200 and "error" not in data:
                return data
            last = "%s error: %s" % (label, (data.get("error") or {}).get("message") or r.status_code)
        except (requests.RequestException, ValueError) as ex:
            last = _short_error(url, ex)
        time.sleep(min(2 * (attempt + 1), 6) if SEED_PAUSE else 0)
    raise RuntimeError(last)


def _bbox_of(chains):
    pts = [p for c in chains for p in c]
    return min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)


def store_put(conn, light, bbox, detail=None):
    conn.execute("INSERT OR REPLACE INTO store (osm_id, source, name, s, w, n, e, light, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                 (light["osm_id"], light["source"], light["name"], bbox[0], bbox[1], bbox[2], bbox[3],
                  json.dumps(light), json.dumps(detail) if detail else None))


def light_of(wk, recipe):
    return make_light(wk["osm_id"], wk["source"], wk["name"], recipe, wk.get("tags") or {}, wk.get("distance_km"),
                      False, (wk["lat"], wk["lon"]), description=wk.get("description"), difficulty=wk.get("difficulty"),
                      time_text=wk.get("time_text"), region=wk.get("region"), link=wk.get("link"),
                      dogs=wk.get("dogs"), wheelchair=wk.get("wheelchair"), status=wk.get("status"),
                      alerts_url=wk.get("alerts_url"), conditions=wk.get("conditions"))


def seed_doc(conn):
    count = _arcgis_post(DOC_URL, {"where": "1=1", "returnCountOnly": "true", "f": "json"}, "DOC")["count"]
    seed_check(conn, "store_doc", 0, count, "Downloading DOC walks…")
    off = stored = 0
    while off < count:
        data = _arcgis_post(DOC_URL, {"where": "1=1", "outFields": "*", "outSR": 4326, "returnGeometry": "true", "f": "geojson",
                                      "orderByFields": "OBJECTID", "resultOffset": off, "resultRecordCount": 100}, "DOC")
        feats = data.get("features", [])
        if not feats:
            break
        for wk in parse_doc(data):
            oid = wk["osm_id"] - DOC_BASE
            wk["recipe"] = {"kind": "doc", "oid": oid}
            store_put(conn, light_of(wk, wk["recipe"]), _bbox_of(wk["chains"]), wk)
            stored += 1
        conn.commit()
        off += len(feats)
        seed_check(conn, "store_doc", min(off, count), count, "Downloaded %d DOC walks" % stored)
        if SEED_PAUSE:
            time.sleep(0.3)
    seed_set(conn, "store_doc", "done", count, count, "%d DOC walks stored" % stored)


def seed_nz(conn):
    count = _arcgis_post(NZT_URL, {"where": NZ_WHERE, "returnCountOnly": "true", "f": "json"}, "NZ tracks")["count"]
    seed_check(conn, "store_nz", 0, count, "Listing tracks…")
    groups, last = {}, -1                                       # step 1: which pieces belong to which track (no geometry yet)
    while True:
        data = _arcgis_post(NZT_URL, {"where": "%s AND OBJECTID>%d" % (NZ_WHERE, last), "outFields": "OBJECTID,Name,MainTrail,LocalAuthority",
                                      "returnGeometry": "false", "orderByFields": "OBJECTID", "resultRecordCount": 2000, "f": "json"}, "NZ tracks")
        feats = data.get("features", [])
        for f in feats:
            p = _props(f)
            last = max(last, int(p["objectid"]))
            name = str(p.get("name") or p.get("maintrail") or "").strip()
            if not name or name.lower() in GENERIC_NAMES:
                continue
            key = (name.lower(), str(p.get("localauthority") or "").lower())
            groups.setdefault(key, {"name": name, "ids": []})["ids"].append(int(p["objectid"]))
        seed_check(conn, "store_nz", 0, count, "Listing tracks… %d found" % len(groups))
        if not feats:
            break
    batches, cur = [], []                                       # step 2: fetch the routes, whole tracks at a time
    for key in sorted(groups):
        cur.append(groups[key])
        if sum(len(g["ids"]) for g in cur) >= 150:
            batches.append(cur)
            cur = []
    if cur:
        batches.append(cur)
    done = stored = 0
    for batch in batches:
        ids = [i for g in batch for i in g["ids"]]
        data = _arcgis_post(NZT_URL, {"where": "OBJECTID IN (%s)" % ",".join(str(i) for i in ids), "outFields": "*", "outSR": 4326,
                                      "returnGeometry": "true", "f": "geojson", "resultRecordCount": 2000}, "NZ tracks")
        by_id = {}
        for f in data.get("features", []):
            oid = _props(f).get("objectid", f.get("id"))
            if oid is not None:
                by_id[int(oid)] = f
        for g in batch:
            feats = [by_id[i] for i in g["ids"] if i in by_id]
            got = [x for x in parse_nz_tracks({"features": feats}) if x["name"].lower() == g["name"].lower()]
            if not got:
                continue
            wk = got[0]
            sm = _nz_summary([{str(k).lower(): v for k, v in (f.get("properties") or {}).items()} for f in feats])
            wk["osm_id"] = nz_light_id(g["name"], sm["authority"])
            wk["recipe"] = {"kind": "nz", "name": g["name"], "bbox": list(_bbox_of(wk["chains"]))}
            store_put(conn, light_of(wk, wk["recipe"]), _bbox_of(wk["chains"]), wk)
            stored += 1
        conn.commit()
        done += len(ids)
        seed_check(conn, "store_nz", done, count, "%d tracks stored" % stored)
        if SEED_PAUSE:
            time.sleep(0.3)
    seed_set(conn, "store_nz", "done", count, count, "%d tracks stored" % stored)


def seed_cells():
    """The 0.5° squares that cover NZ. A square counts as "stored" once the OpenStreetMap store is built, so search can use it."""
    step, cells = SEED_CELL_DEG, []
    s = NZ_BOX[0]
    while s < NZ_BOX[1]:
        w = NZ_BOX[2]
        while w < NZ_BOX[3]:
            cells.append((round(s, 3), round(w, 3)))
            w += step
        s += step
    cells.sort(key=lambda c: (c[0] - -36.85) ** 2 + (c[1] - 174.76) ** 2)
    return cells


def cell_id(s, w):
    return "%.3f,%.3f" % (s, w)


# --- the raw OpenStreetMap file, and the walking store built from it ------------------------------------
def extract_path():
    name = OSM_EXTRACT_URL.rstrip("/").rsplit("/", 1)[-1] or "extract.osm.pbf"
    return os.path.join(DATA_DIR, "osm", name)


def extract_age_days():
    p = extract_path()
    return (time.time() - os.path.getmtime(p)) / 86400.0 if os.path.exists(p) else None


def extract_stale():
    age = extract_age_days()
    return bool(OSM_REFRESH_DAYS > 0 and age is not None and age > OSM_REFRESH_DAYS)


def cluster_boxes(boxes, pad=0.003):
    """Group boxes [s, w, n, e] that touch each other (within `pad` degrees, about 300 m). Returns lists of positions."""
    order = sorted(range(len(boxes)), key=lambda i: boxes[i][0])
    parent = list(range(len(boxes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    active = []
    for i in order:
        s, w, n, e = boxes[i]
        active = [j for j in active if boxes[j][2] + pad >= s]
        for j in active:
            b = boxes[j]
            if not (e < b[1] - pad or w > b[3] + pad):
                parent[find(i)] = find(j)
        active.append(i)
    groups = {}
    for i in range(len(boxes)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _scan_progress(phase, count):
    """(bar position out of 100, text) for a progress line from the reader process."""
    if phase == "start":
        return 2, "Opening the OSM file…"
    if phase == "relations":
        return min(8, 2 + count // 20000), "Looking through route relations (%d so far)…" % count
    if phase == "routes":
        return 10, "Found %d walking routes. Reading footpaths and tracks…" % count
    if phase == "ways":
        return min(35, 10 + int(25 * count / 1800000.0)), "Reading footpaths and tracks (%d thousand ways looked at)…" % (count // 1000)
    if phase == "waysdone":
        return 36, "Found %d walking ways. Placing them on the map…" % count
    if phase == "nodes":
        return 36 + int(22 * (1 - math.exp(-count / 60000000.0))), "Placing them on the map and noting places (%d million points looked at)…" % (count // 1000000)
    if phase == "nodesdone":
        return 58, "Placed %d points. Finishing the read…" % count
    if phase == "poisdone":
        return 59, "Found %d places (cafés, toilets, water…). Finishing the read…" % count
    return 60, "Read the file. Joining the ways into walks…"


def run_scan(conn, path):
    """Read the extract in a separate process, so a big read can never slow the website or take it down."""
    out = os.path.join(os.path.dirname(path), "scan.tmp")
    for stale in (out, out + ".part"):
        if os.path.exists(stale):
            os.remove(stale)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "osm_scan.py")
    proc = subprocess.Popen([sys.executable, script, path, out, ROUTE_TYPES, PATH_HIGHWAYS],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    t0, last, pos, msg, tail, buf = time.time(), 0.0, 2, "Opening the OSM file…", [], b""
    try:
        while True:
            ready, _, _ = select.select([proc.stdout], [], [], 2.0)
            if ready:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("T "):
                        parts = line.split()
                        pos, msg = _scan_progress(parts[1], int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0)
                    elif line and line != "OK":
                        tail = (tail + [line])[-4:]
            elif proc.poll() is not None:
                break
            if time.time() - last > 1.5:
                last = time.time()
                seed_check(conn, "store_osm", done=pos, total=100, msg="%s (%d s)" % (msg, time.time() - t0))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        try:
            proc.stdout.close()
        except OSError:
            pass
    if proc.returncode != 0:
        if proc.returncode < 0:
            raise RuntimeError("Reading the OSM file was stopped by the system (probably not enough memory). Close other apps on the NAS and press Download again.")
        raise RuntimeError("Reading the OSM file failed: %s" % (" ".join(tail) or "exit code %s" % proc.returncode)[:240])
    try:
        with open(out, "rb") as fh:
            return pickle.load(fh)
    finally:
        os.remove(out)


OSM_NEW_TABLE = ("CREATE TABLE store_osm_new (osm_id INTEGER PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL, "
                 "s REAL, w REAL, n REAL, e REAL, light TEXT NOT NULL, detail TEXT)")


def _line(pts):
    return [(pts[i], pts[i + 1]) for i in range(0, len(pts), 2)]


def store_scan(conn, scan):
    """Turn what the reader found into walks and swap them into the store in one go. Returns (routes, paths)."""
    ways = scan["ways"]
    conn.execute("DROP TABLE IF EXISTS store_osm_new")
    conn.execute(OSM_NEW_TABLE)
    conn.execute("DROP TABLE IF EXISTS poi_new")
    conn.execute("CREATE TABLE poi_new (id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, kind TEXT NOT NULL, "
                 "label TEXT NOT NULL, name TEXT, hours TEXT, website TEXT)")

    def put(light, bbox, detail):
        conn.execute("INSERT OR REPLACE INTO store_osm_new (osm_id, source, name, s, w, n, e, light, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                     (light["osm_id"], light["source"], light["name"], bbox[0], bbox[1], bbox[2], bbox[3],
                      json.dumps(light, separators=(",", ":")), json.dumps(detail, separators=(",", ":"))))

    # 1. official route relations: their ways joined end to end
    routes = paths = 0
    items = list(scan["routes"].items())
    for n, (rid, r) in enumerate(items):
        if not n % 50:
            seed_check(conn, "store_osm", done=60 + int(20 * n / max(1, len(items))), total=100,
                       msg="Joining routes into walks (%d of %d)…" % (n, len(items)))
        chains = chain_segments([_line(ways[w]["pts"]) for w in r["ways"] if w in ways])
        name = r["tags"].get("name") or r["tags"].get("ref") or "Unnamed route %d" % rid
        wk = make_walk(rid, "osm-route", name, chains, r["tags"]) if chains else None
        if not wk:
            continue
        wk["recipe"] = {"kind": "relation"}
        bbox = _bbox_of(wk["chains"])
        light = light_of(wk, wk["recipe"])
        light["bb"] = list(bbox)
        put(light, bbox, wk)
        routes += 1

    # 2. named footpaths: every way of one name that touches another becomes one walk
    groups = {}
    for wid, wy in ways.items():
        t = wy["tags"]
        name = ((t or {}).get("name") or "").strip()
        if not name or name.lower() in GENERIC_NAMES:
            continue
        lats, lons = wy["pts"][0::2], wy["pts"][1::2]
        groups.setdefault(name, []).append((wid, [min(lats), min(lons), max(lats), max(lons)]))
    for n, (name, members) in enumerate(groups.items()):
        if not n % 500:
            seed_check(conn, "store_osm", done=80 + int(18 * n / max(1, len(groups))), total=100,
                       msg="Joining footpaths into walks (%d of %d names)…" % (n, len(groups)))
        for idxs in cluster_boxes([b for _, b in members]):
            chains = chain_segments([_line(ways[members[i][0]]["pts"]) for i in idxs])
            if not chains or sum(chain_length_m(c) for c in chains) / 1000.0 < MIN_PATH_KM:
                continue
            tags = {}
            for i in idxs:
                for k, v in ways[members[i][0]]["tags"].items():
                    tags.setdefault(k, v)
            lat0, lon0 = chains[0][0]
            wid = PATH_BASE + zlib.crc32(("%s|%.2f|%.2f" % (name, lat0, lon0)).encode("utf-8"))
            wk = make_walk(wid, "osm-path", name, chains, tags)
            if not wk:
                continue
            b, pad = _bbox_of(wk["chains"]), 0.002
            box = [b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad]
            wk["recipe"] = {"kind": "path", "name": name, "bbox": box}
            put(light_of(wk, wk["recipe"]), box, wk)
            paths += 1
        if n % 200 == 0:
            conn.commit()

    # 3. places along the way: cafes, toilets, water, benches and so on
    pois, rows = 0, []
    for lat, lon, tags in scan.get("pois") or []:
        kind, label = classify_place(tags)
        if not kind:
            continue
        rows.append((lat, lon, kind, label, tags.get("name"), tags.get("opening_hours"),
                     tags.get("website") or tags.get("contact:website")))
        if len(rows) >= 5000:
            conn.executemany("INSERT INTO poi_new (lat, lon, kind, label, name, hours, website) VALUES (?,?,?,?,?,?,?)", rows)
            pois += len(rows)
            rows = []
    if rows:
        conn.executemany("INSERT INTO poi_new (lat, lon, kind, label, name, hours, website) VALUES (?,?,?,?,?,?,?)", rows)
        pois += len(rows)

    # 4. swap: the old OpenStreetMap rows stay in place and searchable until this moment
    seed_check(conn, "store_osm", done=99, total=100, msg="Saving…")
    conn.commit()
    conn.execute("DELETE FROM poi")
    conn.execute("INSERT INTO poi (lat, lon, kind, label, name, hours, website) "
                 "SELECT lat, lon, kind, label, name, hours, website FROM poi_new")
    conn.execute("DROP TABLE poi_new")
    conn.execute("DELETE FROM store WHERE source IN ('osm-route','osm-path')")
    conn.execute("INSERT INTO store (osm_id, source, name, s, w, n, e, light, detail) "
                 "SELECT osm_id, source, name, s, w, n, e, light, detail FROM store_osm_new")
    conn.execute("DROP TABLE store_osm_new")
    conn.execute("DELETE FROM seed_cells WHERE item='store_osm'")                  # everything in NZ is covered now
    conn.executemany("INSERT OR IGNORE INTO seed_cells (item, cell) VALUES ('store_osm', ?)", [(cell_id(s, w),) for s, w in seed_cells()])
    conn.commit()
    return routes, paths, pois


def seed_osm(conn):
    path = extract_path()
    if not os.path.exists(path):
        raise RuntimeError('The raw OSM extract is not on disk (its download may have failed, see "Raw OSM extract"). Press Download again.')
    seed_check(conn, "store_osm", 0, 100, "Opening the OSM file…")
    scan = run_scan(conn, path)
    try:
        routes, paths, pois = store_scan(conn, scan)
    except BaseException:
        conn.rollback()
        conn.execute("DROP TABLE IF EXISTS store_osm_new")
        conn.execute("DROP TABLE IF EXISTS poi_new")
        conn.commit()
        raise
    seed_set(conn, "store_osm", "done", 100, 100,
             "%d OpenStreetMap walks stored (%d routes, %d named paths) and %d places (cafés, toilets, water…) from a file %d days old"
             % (routes + paths, routes, paths, pois, int(extract_age_days() or 0)))


def seed_extract(conn):
    dest = extract_path()
    folder, name = os.path.dirname(dest), os.path.basename(dest)
    os.makedirs(folder, exist_ok=True)
    part, meta = dest + ".part", dest + ".part.meta"
    if os.path.exists(dest) and not extract_stale():
        size = os.path.getsize(dest)
        seed_set(conn, "osm_extract", "done", size, size,
                 "Already saved: %s (%.0f MB, %d days old)" % (name, size / 1e6, int(extract_age_days() or 0)))
        return
    # A stopped download carries on only if the server's file is still the same one (its ETag / date); otherwise it starts again.
    pos = os.path.getsize(part) if os.path.exists(part) else 0
    validator = open(meta).read().strip() if pos and os.path.exists(meta) else ""
    if pos and not validator:
        pos = 0
    headers = {"User-Agent": USER_AGENT}
    if pos:
        headers["Range"] = "bytes=%d-" % pos
        headers["If-Range"] = validator
    try:
        r = requests.get(OSM_EXTRACT_URL, headers=headers, stream=True, timeout=(20, 60))
    except requests.RequestException as ex:
        raise RuntimeError(_short_error(OSM_EXTRACT_URL, ex))
    if r.status_code == 416:                                   # the part file already holds everything
        os.replace(part, dest)
        size = os.path.getsize(dest)
        seed_set(conn, "osm_extract", "done", size, size, "Saved %s (%.0f MB)" % (name, size / 1e6))
        return
    if r.status_code not in (200, 206):
        raise RuntimeError("%s: HTTP %s" % (OSM_EXTRACT_URL.split("/")[2], r.status_code))
    if r.status_code == 200:
        pos = 0                                                # a new file, or the server ignored the resume request
        with open(meta, "w") as fh:
            fh.write(r.headers.get("ETag") or r.headers.get("Last-Modified") or "")
    total = pos + int(r.headers.get("Content-Length") or 0)
    free = shutil.disk_usage(folder).free
    if total and free < (total - pos) * 1.05:
        raise RuntimeError("Not enough free space in the data folder: need %.0f MB, have %.0f MB" % ((total - pos) / 1e6, free / 1e6))
    seed_check(conn, "osm_extract", pos, total, "Downloading %s…" % name)
    last = time.time()
    try:
        with open(part, "ab" if pos else "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                if not chunk:
                    continue
                fh.write(chunk)
                pos += len(chunk)
                if time.time() - last > 1.5:
                    last = time.time()
                    seed_check(conn, "osm_extract", pos, total, "Downloading %s: %.0f of %.0f MB" % (name, pos / 1e6, total / 1e6))
    except requests.RequestException as ex:
        raise RuntimeError("Download interrupted (%s). Press Download again to carry on from where it stopped." % _short_error(OSM_EXTRACT_URL, ex))
    if total and pos < total:
        raise RuntimeError("Download stopped early. Press Download again to carry on from where it stopped.")
    os.replace(part, dest)                                     # the new file replaces the old one only when it is complete
    if os.path.exists(meta):
        os.remove(meta)
    seed_set(conn, "osm_extract", "done", pos, pos, "Saved %s (%.0f MB) in the data folder" % (name, pos / 1e6))


# --- monthly renewal -------------------------------------------------------------------------------------
def refresh_due(conn):
    """True when the OpenStreetMap store is older than OSM_REFRESH_DAYS (and a renewal was not tried in the last day)."""
    if OSM_REFRESH_DAYS <= 0:
        return False
    row = conn.execute("SELECT status, finished FROM seed_state WHERE item='store_osm'").fetchone()
    if not row or row["status"] != "done" or not row["finished"]:
        return False
    if time.time() - row["finished"] < OSM_REFRESH_DAYS * 86400:
        return False
    tried = conn.execute("SELECT updated FROM seed_state WHERE item='osm_refresh'").fetchone()
    return not (tried and time.time() - tried["updated"] < 86400)


def refresh_start(conn):
    """Fetch a fresh file; when it has arrived the store is rebuilt from it (see _seed_loop). The old data keeps serving meanwhile."""
    seed_set(conn, "osm_refresh", "pending", msg="Monthly renewal of the OpenStreetMap data")
    seed_queue(conn, ["osm_extract"])


def refresh_after_extract(conn):
    if seed_status(conn, "osm_refresh") != "pending":
        return
    if seed_status(conn, "osm_extract") == "done":
        seed_queue(conn, ["store_osm"])
    seed_set(conn, "osm_refresh", "idle", msg="Last tried %s" % time.strftime("%Y-%m-%d"))


SEED_FUNCS = {"store_doc": seed_doc, "store_nz": seed_nz, "store_osm": seed_osm, "osm_extract": seed_extract}


def seed_run_one(conn, item):
    seed_set(conn, item, "running")
    try:
        SEED_FUNCS[item](conn)
    except SeedCancelled:
        seed_set(conn, item, "cancelled", msg="Cancelled. Press Download to carry on.")
    except Exception as ex:                                      # a failed item must never stop the others
        app.logger.warning("seed %s failed: %s", item, ex)
        seed_set(conn, item, "error", msg=str(ex)[:300])


def seed_queue(conn, items):
    queued = []
    items = set(items)
    if "store_osm" in items and not os.path.exists(extract_path()):
        items.add("osm_extract")                                  # the store is built from this file
    for item in SEED_RUN_ORDER:
        if item in items and seed_status(conn, item) not in ("queued", "running", "cancelling"):
            seed_set(conn, item, "queued", done=0, total=0, msg="Waiting in line…")
            queued.append(item)
    return queued


def seed_next(conn):
    for item in SEED_RUN_ORDER:
        if seed_status(conn, item) == "queued":
            return item
    return None


def seed_startup(conn):
    """On every start: carry on what a restart interrupted. On the very first start: queue everything for all of NZ."""
    for item in SEED_RUN_ORDER:
        if seed_status(conn, item) in ("running", "cancelling"):
            seed_set(conn, item, "queued", msg="Carrying on after a restart…")
    if os.environ.get("SEED_AUTOSTART", "1") == "1" and conn.execute("SELECT COUNT(*) FROM seed_state").fetchone()[0] == 0:
        seed_queue(conn, SEED_RUN_ORDER)


def _seed_loop():
    first = True
    while True:
        try:
            conn = raw_db()
            if first:
                first = False
                seed_startup(conn)
            item = seed_next(conn)
            if item:
                seed_run_one(conn, item)
                if item == "osm_extract":
                    refresh_after_extract(conn)
            elif refresh_due(conn):
                refresh_start(conn)
                item = "refresh"
            conn.close()
            if not item:
                time.sleep(5)
        except Exception as ex:                                  # keep the worker alive
            app.logger.warning("seed worker: %s", ex)
            time.sleep(15)


def seed_start_worker():
    if os.environ.get("SEED_RUNNER", "1") != "1":                 # switched off (the tests drive the jobs by hand)
        return
    with _seed_thread_lock:
        if _seed_thread["t"] is None or not _seed_thread["t"].is_alive():
            t = threading.Thread(target=_seed_loop, name="walkbox-seed", daemon=True)
            _seed_thread["t"] = t
            t.start()


# --- what the download already holds, used by search and by opening a walk -----------------------------
def store_done(item):
    return seed_status(db(), item) == "done"


def local_covered(s, w, n, e):
    """Which sources the local store can answer for this area: 'doc', 'nz', 'osm'."""
    out = set()
    if store_done("store_doc"):
        out.add("doc")
    if store_done("store_nz"):
        out.add("nz")
    cells = seed_cells_for(s, w, n, e)
    if cells:
        have = {r["cell"] for r in db().execute("SELECT cell FROM seed_cells WHERE item='store_osm'")}
        if all(c in have for c in cells):
            out.add("osm")
    return out


def seed_cells_for(s, w, n, e):
    if not overlaps_nz(s, w, n, e):
        return []
    step = SEED_CELL_DEG
    out = []
    r = math.floor((max(s, NZ_BOX[0]) - NZ_BOX[0]) / step + 1e-9)
    while NZ_BOX[0] + r * step < min(n, NZ_BOX[1]):
        c = math.floor((max(w, NZ_BOX[2]) - NZ_BOX[2]) / step + 1e-9)
        while NZ_BOX[2] + c * step < min(e, NZ_BOX[3]):
            out.append(cell_id(round(NZ_BOX[0] + r * step, 3), round(NZ_BOX[2] + c * step, 3)))
            c += 1
        r += 1
    return out


def local_lists(s, w, n, e, sources):
    covered = local_covered(s, w, n, e)
    kinds = []
    if "doc" in sources and "doc" in covered:
        kinds.append("doc")
    if "nz" in sources and "nz" in covered:
        kinds.append("nz-track")
    if "osm" in covered:
        if "routes" in sources:
            kinds.append("osm-route")
        if "paths" in sources:
            kinds.append("osm-path")
    if not kinds:
        return [], []
    rows = db().execute(
        "SELECT light FROM store WHERE source IN (%s) AND s <= ? AND n >= ? AND w <= ? AND e >= ? LIMIT 4000" % ",".join("?" * len(kinds)),
        kinds + [n, s, e, w]).fetchall()
    return [json.loads(r["light"]) for r in rows], []


def stored_detail(osm_id):
    row = db().execute("SELECT detail FROM store WHERE osm_id=? AND detail IS NOT NULL", (osm_id,)).fetchone()
    return json.loads(row["detail"]) if row else None


# --- live status of the two services that build themselves -------------------------------------------
_size_cache = {}


def folder_size(path):
    now = time.time()
    hit = _size_cache.get(path)
    if hit and now - hit[0] < 30:
        return hit[1]
    total = 0
    if os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    _size_cache[path] = (now, total)
    return total


def service_status(item):
    if item == "valhalla":
        if not VALHALLA_URL:
            return {"status": "off", "msg": "Not set up (no VALHALLA_URL)."}
        size = folder_size(VALHALLA_DIR)
        extra = " (%.0f MB on disk so far)" % (size / 1e6) if size else ""
        try:
            r = requests.get(VALHALLA_URL + "/status", headers={"User-Agent": USER_AGENT}, timeout=4)
            if getattr(r, "status_code", 0) == 200:
                return {"status": "done", "msg": "Ready: walking directions work" + extra}
        except requests.RequestException:
            pass
        return {"status": "running", "msg": "The container is downloading the map and building tiles" + extra + ". Watch its log for detail."}
    if not OVERPASS_LOCAL_URL:
        return {"status": "off", "msg": "Not set up (OVERPASS_LOCAL_URL is blank; Walkbox no longer needs its own Overpass)."}
    size = folder_size(OVERPASS_DIR)
    extra = " (%.1f GB on disk so far)" % (size / 1e9) if size else ""
    _local_ok["ts"] = 0
    if local_overpass_ready():
        return {"status": "done", "msg": "Ready: Walkbox now asks it first" + extra}
    return {"status": "running", "msg": "The container is downloading and importing NZ" + extra + ". It can take hours; Walkbox uses the public servers until it is ready."}


def seed_overview(conn):
    rows = {r["item"]: dict(r) for r in conn.execute("SELECT * FROM seed_state")}
    items = []
    for item in SEED_SHOW_ORDER:
        if item == "overpass_db" and not OVERPASS_LOCAL_URL:
            continue
        label, note = SEED_LABELS[item]
        d = {"id": item, "label": label, "note": note, "service": item in SEED_SERVICES,
             "status": "idle", "done": 0, "total": 0, "msg": ""}
        if item in SEED_SERVICES:
            d.update(service_status(item))
        elif item in rows:
            r = rows[item]
            d.update(status=r["status"], done=r["done"] or 0, total=r["total"] or 0, msg=r["msg"] or "")
            if item == "store_osm" and r["status"] == "done" and r["finished"] and OSM_REFRESH_DAYS > 0:
                due = time.strftime("%d %b %Y", time.localtime(r["finished"] + OSM_REFRESH_DAYS * 86400))
                d["msg"] = "%s. Renews itself around %s." % (d["msg"].rstrip("."), due)
        items.append(d)
    return items


@app.get("/api/seed")
def api_seed_status():
    items = seed_overview(db())
    return jsonify(items=items, busy=any(i["status"] in ("queued", "running", "cancelling") and not i["service"] for i in items))


@app.post("/api/seed")
def api_seed_start():
    b = request.get_json(silent=True) or {}
    wanted = [x for x in (b.get("items") or []) if x in SEED_FUNCS]
    seed_queue(db(), wanted)
    seed_start_worker()
    return api_seed_status()


@app.post("/api/seed/cancel")
def api_seed_cancel():
    conn = db()
    for item in SEED_RUN_ORDER:
        st = seed_status(conn, item)
        if st == "queued":
            seed_set(conn, item, "cancelled", msg="Cancelled.")
        elif st == "running":
            seed_set(conn, item, "cancelling", msg="Stopping…")
    return api_seed_status()


# --------------------------------------------------------------------------
# Search by walk name, anywhere in New Zealand (not limited to the map)
# --------------------------------------------------------------------------
FIND_LIMIT = 200


def name_tokens(q):
    return [x for x in re.findall(r"[^\W_]+", (q or "").lower()) if x][:5]


def _like_where(tokens, fields):
    return " AND ".join("(%s)" % " OR ".join("UPPER(%s) LIKE '%%%s%%'" % (f, _esc_sql(tok.upper())) for f in fields) for tok in tokens)


def _area(v):
    """[south, west, north, east] from a request, or None."""
    try:
        a = [float(x) for x in v]
        return a if len(a) == 4 and a[0] <= a[2] and a[1] <= a[3] else None
    except (TypeError, ValueError):
        return None


def _inside(wk, a):
    return wk.get("lat") is not None and wk.get("lon") is not None and a[0] <= wk["lat"] <= a[2] and a[1] <= wk["lon"] <= a[3]


def find_by_name(tokens, sources, area=None, exclude=()):
    """Walks whose name holds every word, from the stored download where there is one and live from DOC / the national tracks otherwise."""
    walks, problems, notes = [], [], []
    conn = db()
    kinds = []
    if "doc" in sources and store_done("store_doc"):
        kinds.append("doc")
    if "nz" in sources and store_done("store_nz"):
        kinds.append("nz-track")
    if "routes" in sources:
        kinds.append("osm-route")
    if "paths" in sources:
        kinds.append("osm-path")
    if kinds:
        like = " AND ".join("name LIKE ?" for _ in tokens)
        # the inner query walks the name index only (a few MB); the full rows are fetched just for the matches
        box, bargs = "", []
        if area:                                       # only walks whose outline touches this area
            box, bargs = " AND n >= ? AND s <= ? AND e >= ? AND w <= ?", [area[0], area[2], area[1], area[3]]
        rows = conn.execute("SELECT light FROM store WHERE osm_id IN (SELECT osm_id FROM store INDEXED BY store_name WHERE %s) "
                            "AND source IN (%s)%s LIMIT ?" % (like, ",".join("?" * len(kinds)), box),
                            ["%" + tok + "%" for tok in tokens] + kinds + bargs + [FIND_LIMIT * 3]).fetchall()
        walks += [json.loads(r["light"]) for r in rows]
    if ("routes" in sources or "paths" in sources) and not conn.execute("SELECT 1 FROM seed_cells WHERE item='store_osm' LIMIT 1").fetchone():
        notes.append("OpenStreetMap walks can be searched by name once their download on the Data tab has finished.")   # never built yet; a monthly renewal keeps the old walks searchable
    if "doc" in sources and not store_done("store_doc"):
        try:
            data = _arcgis_get(DOC_URL, {
                "where": _like_where(tokens, ["name"]), "returnGeometry": "true", **COARSE, "f": "json", "resultRecordCount": FIND_LIMIT,
                "outFields": "OBJECTID,name,introduction,difficulty,completionTime,walkingAndTrampingWebPage,Shape__Length"}, "DOC")
            walks += doc_lights(data.get("features", []))
        except RuntimeError as ex:
            problems.append("DOC: %s" % ex)
    if "nz" in sources and not store_done("store_nz"):
        try:
            data = _arcgis_get(NZT_URL, {
                "where": "%s AND (%s)" % (NZ_WHERE, _like_where(tokens, ["Name", "MainTrail"])), "returnGeometry": "true", **COARSE, "f": "json",
                "resultRecordCount": 2000,
                "outFields": "OBJECTID,Name,MainTrail,Walk,Locality,LocalAuthority,Description,WebLink,DogAccess,MobilityAccess,ManagedBy,Status,ConditionsOfUse,Shape__Length"}, "NZ tracks")
            nz_s, nz_n, nz_w, nz_e = NZ_BOX
            walks += nz_lights(data.get("features", []), [nz_s, nz_w, nz_n, nz_e])
        except RuntimeError as ex:
            problems.append("NZ tracks: %s" % ex)
    seen, out = set(), []
    for wk in prefer_curated(walks):
        if wk["osm_id"] not in seen and all(tok in wk["name"].lower() for tok in tokens):
            if (area and not _inside(wk, area)) or any(_inside(wk, x) for x in exclude):
                continue                               # belongs to another stage of the search (map, Auckland, rest of NZ)
            seen.add(wk["osm_id"])
            out.append(wk)
    q = " ".join(tokens)
    order = {"doc": 0, "nz-track": 1, "osm-route": 2, "osm-path": 3}
    out.sort(key=lambda x: (0 if x["name"].lower() == q else 1 if x["name"].lower().startswith(q) else 2,
                            order.get(x["source"], 9), -(x["distance_km"] or 0)))
    remember(out)
    return out[:FIND_LIMIT], problems, notes


@app.post("/api/find")
def api_find():
    b = request.get_json(silent=True) or {}
    tokens = name_tokens(b.get("q"))
    if not tokens or sum(len(x) for x in tokens) < 2:
        return jsonify(error="Type at least two letters of the walk's name"), 400
    sources = [x for x in (b.get("sources") or ALL_SOURCES) if x in ALL_SOURCES]
    if not sources:
        return jsonify(error="Choose at least one source"), 400
    t0 = time.time()
    area = _area(b.get("area"))
    exclude = [a for a in (_area(x) for x in (b.get("exclude") or [])[:3]) if a]
    walks, problems, notes = find_by_name(tokens, sources, area, exclude)
    if not walks and problems and not area:
        return jsonify(error="Could not search: " + " | ".join(problems)), 502
    have = {r["osm_id"] for r in db().execute("SELECT osm_id FROM walks")}
    return jsonify(results=[public(wk, wk["osm_id"] in have) for wk in walks], warnings=problems, notes=notes,
                   capped=len(walks) >= FIND_LIMIT, ms=int((time.time() - t0) * 1000))


if os.environ.get("SEED_RUNNER", "1") == "1":
    seed_start_worker()
