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
import re
import sqlite3
import threading
import time
import zlib
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
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
MAX_SPAN_DEG = float(os.environ.get("MAX_SPAN_DEG", "0.5"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "150"))
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
GENERIC_NAMES = {"footpath", "path", "track", "walkway", "footway", "pavement", "steps",
                 "pedestrian", "boardwalk", "trail", "walking track", "unnamed"}

WALK_GRADE = {1: "Path", 2: "Short walk", 3: "Walking track", 4: "Tramping track",
              5: "Challenging tramping track", 6: "Route"}
DOG_TEXT = {"OffLeash": "off lead", "OnLeash": "on lead only", "Permit": "permit required",
            "ProhibitedAtTimes": "prohibited at times", "Prohibited": "not allowed"}

ATTRIBUTION = {
    "osm-route": "(c) OpenStreetMap contributors, ODbL (https://www.openstreetmap.org/copyright)",
    "osm-path": "(c) OpenStreetMap contributors, ODbL (https://www.openstreetmap.org/copyright)",
    "nz-track": "Source: Herenga a Nuku Outdoor Access Commission national tracks dataset, compiled "
                "from councils, LINZ and DOC. Licence not stated by the service; check the managing "
                "organisation before reuse.",
    "import": "Imported by the user from a GPX file. Check the original source's terms before sharing.",
    "doc": "Source: New Zealand Department of Conservation, CC BY 4.0 "
           "(https://creativecommons.org/licenses/by/4.0/)",
}
SOURCE_LABEL = {"osm-route": "OSM route", "osm-path": "OSM path", "doc": "DOC",
                "nz-track": "NZ tracks", "import": "Imported"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
_overpass_lock = threading.Lock()
_cache = {}  # walk id -> parsed walk (from recent searches)
_cache_lock = threading.Lock()
_rate_lock = threading.Lock()
_weather_cache = {}
_search_cache = {}
SEARCH_TTL = int(os.environ.get("SEARCH_CACHE_SECONDS", "900"))
_places_cache = {}
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
"""
EXTRA_COLUMNS = {
    "source": "TEXT", "difficulty": "TEXT", "route_type": "TEXT", "ascent_m": "REAL",
    "link": "TEXT", "region": "TEXT", "time_text": "TEXT",
    "my_rating": "INTEGER", "my_note": "TEXT", "profile": "TEXT",
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
            time_text=p.get("completiontime"), region=p.get("region"), link=link,
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
        props = grp["props"]

        def first(key):
            for pr in props:
                v = pr.get(key)
                if v not in (None, "", "Unknown"):
                    return v
            return None

        grades = [pr.get("walk") for pr in props if pr.get("walk") in WALK_GRADE]
        difficulty = WALK_GRADE[max(grades)] if grades else None
        facts = []
        dog = first("dogaccess")
        if dog in DOG_TEXT:
            facts.append("Dogs: %s." % DOG_TEXT[dog])
        if first("mobilityaccess") == "Yes":
            facts.append("Suitable for mobility access.")
        desc = " ".join(x for x in [first("description"), " ".join(facts)] if x) or None
        lat0, lon0 = chains[0][0]
        wid = NZT_BASE + zlib.crc32(("%s|%.2f|%.2f" % (grp["name"], lat0, lon0)).encode("utf-8"))
        wk = make_walk(
            wid, "nz-track", grp["name"], chains, {"operator": first("managedby")},
            description=desc, difficulty=difficulty, link=first("weblink"),
            region=first("locality") or first("localauthority"),
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


def overpass(query):
    errors = []
    with _overpass_lock:
        for url in OVERPASS_URLS:
            try:
                r = requests.post(url, data={"data": query},
                                  headers={"User-Agent": USER_AGENT}, timeout=45)
                if r.status_code == 200:
                    return r.json()
                errors.append("%s: HTTP %s" % (url.split("/")[2], r.status_code))
            except (requests.RequestException, ValueError) as e:
                errors.append(_short_error(url, e))
            time.sleep(1)
    raise RuntimeError("; ".join(errors) or "No Overpass server configured")


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


def list_osm(s, w, n, e, want_routes, want_paths):
    """Cheap OSM search: names, tags and bounding boxes only (no geometry).

    Exact tag matches (not regular expressions) and one global bbox keep the query fast on busy servers.
    """
    head = "[out:json][timeout:25][bbox:%f,%f,%f,%f];" % (s, w, n, e)
    rel_types = [t for t in ROUTE_TYPES.split("|") if t]
    way_types = [t for t in PATH_HIGHWAYS.split("|") if t]
    parts = [head]
    if want_routes:
        parts.append("(%s)->.r;" % "".join('rel["type"="route"]["route"="%s"];' % t for t in rel_types))
    if want_paths:
        parts.append("(%s)->.p;" % "".join(
            'way["highway"="%s"]["name"]["access"!~"^(private|no)$"]["foot"!="no"];' % t for t in way_types))
    if want_routes:
        parts.append(".r out tags bb %d;" % MAX_RESULTS)
    if want_paths:
        parts.append(".p out tags bb %d;" % MAX_PATH_WAYS)
    walks, groups = [], {}
    for el in overpass("".join(parts)).get("elements", []):
        b = el.get("bounds")
        if not b:
            continue
        tags = el.get("tags", {})
        if el.get("type") == "relation" and want_routes:
            name = tags.get("name") or tags.get("ref") or "Unnamed route %d" % el["id"]
            walks.append(make_light(el["id"], "osm-route", name, {"kind": "relation"}, tags,
                                    centre=((b["minlat"] + b["maxlat"]) / 2, (b["minlon"] + b["maxlon"]) / 2)))
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


def list_doc(s, w, n, e):
    params = {
        "where": "1=1", "geometry": "%f,%f,%f,%f" % (w, s, e, n), "geometryType": "esriGeometryEnvelope",
        "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false",
        "outFields": "OBJECTID,name,introduction,difficulty,completionTime,region,"
                     "walkingAndTrampingWebPage,staticLink,Shape__Length",
        "resultRecordCount": MAX_RESULTS, "f": "json",
    }
    walks = []
    for f in _arcgis_get(DOC_URL, params, "DOC").get("features", []):
        p = _props(f)
        if p.get("objectid") is None:
            continue
        oid = int(p["objectid"])
        walks.append(make_light(
            DOC_BASE + oid, "doc", p.get("name") or "DOC track", {"kind": "doc", "oid": oid},
            {"operator": "Department of Conservation"}, _km(p), False, None,
            description=p.get("introduction"), difficulty=p.get("difficulty"), time_text=p.get("completiontime"),
            region=p.get("region"), link=p.get("walkingandtrampingwebpage") or p.get("staticlink")))
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
    facts = []
    dog = first("dogaccess")
    if dog in DOG_TEXT:
        facts.append("Dogs: %s." % DOG_TEXT[dog])
    if first("mobilityaccess") == "Yes":
        facts.append("Suitable for mobility access.")
    return {"difficulty": WALK_GRADE[max(grades)] if grades else None,
            "description": " ".join(x for x in [first("description"), " ".join(facts)] if x) or None,
            "link": first("weblink"), "region": first("locality") or first("localauthority"),
            "operator": first("managedby"), "authority": first("localauthority")}


def nz_light_id(name, authority):
    return NZT_BASE + zlib.crc32(("%s|%s" % (name, authority or "")).lower().encode("utf-8"))


def list_nz(s, w, n, e):
    params = {
        "where": NZ_WHERE, "geometry": "%f,%f,%f,%f" % (w, s, e, n), "geometryType": "esriGeometryEnvelope",
        "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false",
        "outFields": "OBJECTID,Name,MainTrail,Walk,Locality,LocalAuthority,Description,WebLink,"
                     "DogAccess,MobilityAccess,ManagedBy,Shape__Length",
        "resultRecordCount": 2000, "f": "json",
    }
    groups = {}
    for f in _arcgis_get(NZT_URL, params, "NZ tracks").get("features", []):
        p = _props(f)
        name = str(p.get("name") or p.get("maintrail") or "").strip()
        if not name or name.lower() in GENERIC_NAMES:
            continue
        key = (name.lower(), str(p.get("localauthority") or "").lower())
        g = groups.setdefault(key, {"name": name, "props": [], "km": 0.0, "known": False})
        g["props"].append(p)
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
            {"kind": "nz", "name": g["name"], "bbox": [s, w, n, e]}, {"operator": sm["operator"]},
            dist, False, None, description=sm["description"], difficulty=sm["difficulty"],
            link=sm["link"], region=sm["region"]))
    return walks


def overlaps_nz(s, w, n, e):
    return not (n < NZ_BOX[0] or s > NZ_BOX[1] or e < NZ_BOX[2] or w > NZ_BOX[3])


def search_all(s, w, n, e, sources):
    """Fetch the light lists from every chosen source at the same time."""
    jobs = []
    if "routes" in sources or "paths" in sources:
        jobs.append(("OpenStreetMap", lambda: list_osm(s, w, n, e, "routes" in sources, "paths" in sources)))
    if "doc" in sources and overlaps_nz(s, w, n, e):
        jobs.append(("DOC", lambda: list_doc(s, w, n, e)))
    if "nz" in sources and overlaps_nz(s, w, n, e):
        jobs.append(("NZ tracks", lambda: list_nz(s, w, n, e)))
    walks, problems = [], []
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futures = [(label, pool.submit(fn)) for label, fn in jobs]
        for label, fut in futures:
            try:
                walks += fut.result()
            except RuntimeError as ex:
                problems.append("%s: %s" % (label, ex))
    # Prefer curated sources: drop duplicates of the same name from less curated ones.
    doc_names = {x["name"].lower() for x in walks if x["source"] == "doc"}
    walks = [x for x in walks if not (x["source"] == "nz-track" and x["name"].lower() in doc_names)]
    curated = {x["name"].lower() for x in walks if x["source"] in ("doc", "nz-track")}
    walks = [x for x in walks if not (x["source"] == "osm-path" and x["name"].lower() in curated)]
    with _cache_lock:
        for wk in walks:
            old = _cache.get(wk["osm_id"])
            if not (old and old.get("chains")):          # keep a route that was already loaded
                _cache[wk["osm_id"]] = wk
        if len(_cache) > 3000:
            for k in list(_cache)[:1500]:
                _cache.pop(k, None)
    return walks, problems


def _esc_sql(v):
    return str(v).replace("'", "''")


def load_detail(wk):
    """Fetch the full route for one search result (on click or when adding it to the box)."""
    rec = wk.get("recipe") or {}
    kind = rec.get("kind")
    full = None
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
        data = _arcgis_get(NZT_URL, {
            "where": "(Name='%s' OR MainTrail='%s') AND %s" % (nm, nm, NZ_WHERE),
            "geometry": "%f,%f,%f,%f" % (b[1], b[0], b[3], b[2]), "geometryType": "esriGeometryEnvelope",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "outFields": "*", "outSR": 4326,
            "resultRecordCount": 2000, "f": "geojson"}, "NZ tracks")
        got = [x for x in parse_nz_tracks(data) if x["name"].lower() == rec["name"].lower()]
        full = got[0] if got else None
    if not full:
        raise RuntimeError("Could not load the route for \"%s\". The source may have changed; search again." % wk["name"])
    full["osm_id"] = wk["osm_id"]           # keep the id the list used
    full["recipe"] = rec
    for k in ("description", "link", "region", "difficulty", "time_text"):
        if not full.get(k) and wk.get(k):
            full[k] = wk[k]
    with _cache_lock:
        _cache[wk["osm_id"]] = full
    return full


def main_chain(osm_id):
    """The longest track of a walk, from the box or from the search cache (loading it if needed)."""
    row = db().execute("SELECT geom FROM walks WHERE osm_id=?", (osm_id,)).fetchone()
    if row:
        return json.loads(row["geom"])[0], True
    with _cache_lock:
        wk = _cache.get(osm_id)
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
    attribution = ATTRIBUTION.get(source, ATTRIBUTION["osm-route"])
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
    if source == "doc" and row["link"]:
        link = row["link"]
        link_text = "DOC page"
    elif source == "nz-track" and row["link"]:
        link, link_text = row["link"], "Track information"
    elif source == "import":
        link, link_text = "https://www.openstreetmap.org/#map=15/%.5f/%.5f" % (row["lat"], row["lon"]), "Location"
    elif source == "osm-path":
        link, link_text = "https://www.openstreetmap.org/#map=16/%.5f/%.5f" % (row["lat"], row["lon"]), "OpenStreetMap"
    else:
        link, link_text = "https://www.openstreetmap.org/relation/%d" % row["osm_id"], "OpenStreetMap relation"
    author = {"doc": "New Zealand Department of Conservation",
              "nz-track": "Herenga a Nuku Outdoor Access Commission",
              "import": "Original GPX author"}.get(source, "OpenStreetMap contributors")
    license_url = {"doc": "https://creativecommons.org/licenses/by/4.0/",
                   "nz-track": "https://www.herengaanuku.govt.nz/",
                   "import": "https://www.openstreetmap.org/copyright"}.get(
        source, "https://opendatacommons.org/licenses/odbl/")
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
        "place": walk.get("place"), "added_at": walk.get("added_at"),
        "my_rating": walk.get("my_rating"), "my_note": walk.get("my_note"),
    }
    if preview and walk.get("chains"):
        d["line"] = [simplify(c) for c in walk["chains"][:20]]
    return d


@app.route("/")
def index():
    return render_template("index.html", max_span=MAX_SPAN_DEG, routing_enabled=bool(VALHALLA_URL))


@app.route("/healthz")
def healthz():
    return "ok"


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
    if (n - s) > MAX_SPAN_DEG or (e - w) > MAX_SPAN_DEG:
        return jsonify(error="Zoom in further: the search area is too large (max about %d km)."
                       % int(MAX_SPAN_DEG * 111)), 400
    sources = [x for x in (b.get("sources") or ALL_SOURCES) if x in ALL_SOURCES]
    if not sources:
        return jsonify(error="Choose at least one source"), 400
    t0 = time.time()
    key = (round(s, 3), round(w, 3), round(n, 3), round(e, 3), tuple(sorted(sources)))
    hit = _search_cache.get(key)
    if hit and time.time() - hit[0] < SEARCH_TTL:
        walks, problems = hit[1], hit[2]
        with _cache_lock:                              # make sure the results can still be opened
            for wk in walks:
                _cache.setdefault(wk["osm_id"], wk)
    else:
        walks, problems = search_all(s, w, n, e, sources)
        if not problems:                               # never remember a partial or failed search
            _search_cache[key] = (time.time(), walks, problems)
            if len(_search_cache) > 200:
                _search_cache.clear()
    if not walks and problems:
        return jsonify(error="Could not search: " + " | ".join(problems)), 502
    have = {r["osm_id"] for r in db().execute("SELECT osm_id FROM walks")}
    order = {"doc": 0, "nz-track": 1, "osm-route": 2, "osm-path": 3}
    walks.sort(key=lambda x: (order.get(x["source"], 9), -(x["distance_km"] or 0)))
    return jsonify(results=[public(wk, wk["osm_id"] in have) for wk in walks],
                   warnings=problems, capped=len(walks) >= MAX_RESULTS, ms=int((time.time() - t0) * 1000))


@app.post("/api/walks")
def api_add():
    body = request.get_json(silent=True) or {}
    try:
        ids = [int(i) for i in body.get("ids", [])][:100]
    except (TypeError, ValueError):
        return jsonify(error="ids must be numbers"), 400
    if not ids:
        return jsonify(error="No walks selected"), 400
    with _cache_lock:
        found = {i: _cache[i] for i in ids if i in _cache}
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
            "lat,lon,place,tags,geom,added_at,source,difficulty,route_type,ascent_m,link,region,time_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wk["osm_id"], wk["name"], wk["ref"], wk["network"], wk["operator"], wk["description"],
             wk["distance_km"], wk["lat"], wk["lon"], body.get("place"),
             json.dumps(wk["tags"]), json.dumps(wk["chains"]), now, wk["source"], wk["difficulty"],
             wk["route_type"], wk["ascent_m"], wk["link"], wk["region"], wk["time_text"]),
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
    with _cache_lock:
        wk = _cache.get(wid)
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
        if b.get("mode") == "to_start":
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


def fetch_places(chain):
    """Food, water, toilets, rest spots and viewpoints near a route, from OpenStreetMap."""
    ch = simplify(chain, 400)
    pad = 0.0025
    s_, n_ = min(p[0] for p in ch) - pad, max(p[0] for p in ch) + pad
    w_, e_ = min(p[1] for p in ch) - pad, max(p[1] for p in ch) + pad
    bb = "(%f,%f,%f,%f)" % (s_, w_, n_, e_)
    q = ("[out:json][timeout:25];("
         'nwr["amenity"~"^(cafe|restaurant|fast_food|food_court|ice_cream|pub|bar|drinking_water|toilets|shelter|bench)$"]%s;'
         'nwr["leisure"="picnic_table"]%s;nwr["tourism"~"^(picnic_site|viewpoint)$"]%s;'
         'nwr["shop"~"^(bakery|convenience|supermarket)$"]%s;);out tags center 1500;' % (bb, bb, bb, bb))
    cum = [0.0]
    for i in range(1, len(ch)):
        cum.append(cum[-1] + haversine_m(ch[i - 1], ch[i]))
    total_km = cum[-1] / 1000.0
    out, seen = [], set()
    for el in overpass(q).get("elements", []):
        tags = el.get("tags", {})
        kind, label = classify_place(tags)
        if not kind:
            continue
        lat = el.get("lat") if el.get("lat") is not None else (el.get("center") or {}).get("lat")
        lon = el.get("lon") if el.get("lon") is not None else (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        along, off = along_and_off(lat, lon, ch, cum)
        if off > PLACE_KINDS[kind][2]:
            continue
        key = (el.get("type"), el.get("id"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "label": label, "name": tags.get("name"), "lat": round(lat, 6), "lon": round(lon, 6),
                    "off_m": int(round(off)), "along_km": round(along / 1000.0, 2),
                    "hours": tags.get("opening_hours"), "website": tags.get("website") or tags.get("contact:website")})
    out.sort(key=lambda x: x["along_km"])
    per_kind = {}
    trimmed = []
    for pl in out:                      # keep the list readable: at most 40 of each kind
        per_kind[pl["kind"]] = per_kind.get(pl["kind"], 0) + 1
        if per_kind[pl["kind"]] <= 40:
            trimmed.append(pl)
    counts = {k: sum(1 for x in out if x["kind"] == k) for k in PLACE_KINDS}
    return {"places": trimmed, "counts": counts, "total_km": round(total_km, 2),
            "kinds": {k: {"label": v[0], "icon": v[1]} for k, v in PLACE_KINDS.items()}}


@app.get("/api/walks/<int:osm_id>/places")
def api_places(osm_id):
    hit = _places_cache.get(osm_id)
    if hit and time.time() - hit[0] < 3600:
        return jsonify(hit[1])
    try:
        chain, _ = main_chain(osm_id)
    except RuntimeError as ex:
        return jsonify(error=str(ex)), 502
    if not rate_ok("places", 20):
        return jsonify(error="Too many lookups just now. Try again in a minute."), 429
    try:
        res = fetch_places(chain)
    except RuntimeError as ex:
        return jsonify(error="Nearby places unavailable: %s" % ex), 502
    _places_cache[osm_id] = (time.time(), res)
    if len(_places_cache) > 300:
        _places_cache.clear()
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
    pts = [chain[0], chain[len(chain) // 2], chain[-1]]
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
    if not row:
        abort(404)
    if not rate_ok("photos", PHOTOS_PER_MIN):
        return jsonify(error="Too many photo lookups just now. Try again in a minute."), 429
    try:
        chain = json.loads(row["geom"])[0]
        return jsonify(photos=commons_photos(chain))
    except RuntimeError as ex:
        return jsonify(error="Photo service problem: %s" % ex), 502


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


@app.get("/api/walks/<int:osm_id>/profile")
def api_profile(osm_id):
    row = db().execute("SELECT profile FROM walks WHERE osm_id=?", (osm_id,)).fetchone()
    if row and row["profile"]:
        return jsonify(json.loads(row["profile"]))
    try:
        chain, in_box = main_chain(osm_id)
    except RuntimeError as ex:
        return jsonify(error=str(ex)), 502
    if not rate_ok("profile", 60):
        return jsonify(error="Too many elevation lookups just now. Try again in a minute."), 429
    try:
        prof = build_profile(chain)
    except RuntimeError as ex:
        return jsonify(error="Elevation profile unavailable: %s" % ex), 502
    if in_box:
        conn = db()
        conn.execute("UPDATE walks SET profile=? WHERE osm_id=?", (json.dumps(prof), osm_id))
        conn.commit()
    return jsonify(prof)


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
