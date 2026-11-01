"""Walkbox: find walking routes anywhere (OpenStreetMap), add them to a
searchable local library, and download them as GPX files.

Data: (c) OpenStreetMap contributors, ODbL. https://www.openstreetmap.org/copyright
"""
import io
import json
import math
import os
import re
import sqlite3
import threading
import time
import zipfile
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
DB_PATH = os.environ.get("DB_PATH", "/data/walkbox.db")
MAX_SPAN_DEG = float(os.environ.get("MAX_SPAN_DEG", "0.5"))  # max map width/height searched
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "150"))
ROUTE_TYPES = os.environ.get("ROUTE_TYPES", "hiking|foot|walking")
USER_AGENT = os.environ.get("USER_AGENT", "walkbox/1.0 (self-hosted walk library)")
ATTRIBUTION = "(c) OpenStreetMap contributors, ODbL (https://www.openstreetmap.org/copyright)"

app = Flask(__name__)
_overpass_lock = threading.Lock()
_cache = {}  # osm_id -> parsed walk (from recent searches)
_cache_lock = threading.Lock()


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


def db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.executescript(SCHEMA)
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
    """Join way geometries end-to-end into continuous chains.

    Ways of a route relation share end nodes, so exact endpoint matching works.
    Returns chains sorted longest first; gaps in the relation give several chains.
    """
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
# Overpass
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
    """POST a query to Overpass, one at a time, trying fallback servers."""
    errors = []
    with _overpass_lock:
        for url in OVERPASS_URLS:
            try:
                r = requests.post(
                    url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=90
                )
                if r.status_code == 200:
                    return r.json()
                errors.append("%s: HTTP %s" % (url.split("/")[2], r.status_code))
            except (requests.RequestException, ValueError) as e:
                errors.append(_short_error(url, e))
            time.sleep(1)
    raise RuntimeError("; ".join(errors) or "No Overpass server configured")


def parse_relation(el):
    """Turn an Overpass relation element (with geometry) into a walk dict."""
    tags = el.get("tags", {})
    segs = []
    for m in el.get("members", []):
        if m.get("type") == "way" and m.get("geometry"):
            segs.append([(p["lat"], p["lon"]) for p in m["geometry"]])
    chains = chain_segments(segs)
    if not chains:
        return None
    dist_km = sum(chain_length_m(c) for c in chains) / 1000.0
    pts = [p for c in chains for p in c]
    lat = sum(p[0] for p in pts) / len(pts)
    lon = sum(p[1] for p in pts) / len(pts)
    osm_id = el["id"]
    name = tags.get("name") or tags.get("ref") or "Unnamed route %d" % osm_id
    return {
        "osm_id": osm_id,
        "name": name,
        "ref": tags.get("ref"),
        "network": tags.get("network"),
        "operator": tags.get("operator"),
        "description": tags.get("description") or tags.get("note"),
        "distance_km": round(dist_km, 2),
        "lat": lat,
        "lon": lon,
        "tags": tags,
        "chains": chains,
    }


def search_bbox(s, w, n, e):
    q = (
        '[out:json][timeout:60];'
        'rel["type"="route"]["route"~"^(%s)$"](%f,%f,%f,%f);'
        "out tags geom %d;" % (ROUTE_TYPES, s, w, n, e, MAX_RESULTS)
    )
    data = overpass(q)
    walks = []
    for el in data.get("elements", []):
        if el.get("type") != "relation":
            continue
        walk = parse_relation(el)
        if walk:
            walks.append(walk)
    with _cache_lock:
        for wk in walks:
            _cache[wk["osm_id"]] = wk
        if len(_cache) > 2000:  # keep memory bounded
            for k in list(_cache)[:1000]:
                _cache.pop(k, None)
    return walks


def fetch_by_ids(ids):
    q = "[out:json][timeout:60];rel(id:%s);out tags geom;" % ",".join(str(int(i)) for i in ids)
    data = overpass(q)
    walks = []
    for el in data.get("elements", []):
        if el.get("type") == "relation":
            wk = parse_relation(el)
            if wk:
                walks.append(wk)
    return walks


# --------------------------------------------------------------------------
# GPX
# --------------------------------------------------------------------------
def build_gpx(row):
    chains = json.loads(row["geom"])
    tags = json.loads(row["tags"] or "{}")
    desc_parts = []
    if row["description"]:
        desc_parts.append(row["description"])
    desc_parts.append("%.1f km walking route. %s" % (row["distance_km"], ATTRIBUTION))
    desc = " ".join(desc_parts)
    osm_url = "https://www.openstreetmap.org/relation/%d" % row["osm_id"]
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="walkbox" xmlns="http://www.topografix.com/GPX/1/1">',
        "<metadata>",
        "<name>%s</name>" % escape(row["name"]),
        "<desc>%s</desc>" % escape(desc),
        '<link href="%s"><text>OpenStreetMap relation %d</text></link>' % (escape(osm_url), row["osm_id"]),
        "<copyright author=\"OpenStreetMap contributors\"><license>https://opendatacommons.org/licenses/odbl/</license></copyright>",
        "</metadata>",
        "<trk>",
        "<name>%s</name>" % escape(row["name"]),
        "<desc>%s</desc>" % escape(desc),
    ]
    for chain in chains:
        out.append("<trkseg>")
        for lat, lon in chain:
            out.append('<trkpt lat="%.7f" lon="%.7f"/>' % (lat, lon))
        out.append("</trkseg>")
    out.append("</trk>")
    out.append("</gpx>")
    return "\n".join(out)


def safe_filename(name, osm_id):
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" ._") or "walk"
    return "%s-%d.gpx" % (base[:80], osm_id)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def public(walk, added=False, preview=True):
    d = {
        "osm_id": walk["osm_id"],
        "name": walk["name"],
        "ref": walk.get("ref"),
        "network": walk.get("network"),
        "operator": walk.get("operator"),
        "description": walk.get("description"),
        "distance_km": walk["distance_km"],
        "lat": walk.get("lat"),
        "lon": walk.get("lon"),
        "added": added,
    }
    if preview:
        d["line"] = [simplify(c) for c in walk["chains"][:20]]
    return d


@app.route("/")
def index():
    return render_template("index.html", max_span=MAX_SPAN_DEG)


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
    if e < w:  # area crosses the antimeridian; not supported
        return jsonify(error="Please pan the map so it does not cross the date line"), 400
    if (n - s) > MAX_SPAN_DEG or (e - w) > MAX_SPAN_DEG:
        return jsonify(error="Zoom in further: the search area is too large (max about %d km)." % int(MAX_SPAN_DEG * 111)), 400
    try:
        walks = search_bbox(s, w, n, e)
    except RuntimeError as ex:
        return jsonify(error="OpenStreetMap server problem: %s" % ex), 502
    have = {r["osm_id"] for r in db().execute("SELECT osm_id FROM walks")}
    walks.sort(key=lambda x: x["distance_km"], reverse=True)
    return jsonify(results=[public(wk, wk["osm_id"] in have) for wk in walks],
                   capped=len(walks) >= MAX_RESULTS)


@app.post("/api/walks")
def api_add():
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    try:
        ids = [int(i) for i in ids][:100]
    except (TypeError, ValueError):
        return jsonify(error="ids must be numbers"), 400
    if not ids:
        return jsonify(error="No walks selected"), 400
    with _cache_lock:
        found = {i: _cache[i] for i in ids if i in _cache}
    missing = [i for i in ids if i not in found]
    if missing:
        try:
            for wk in fetch_by_ids(missing):
                found[wk["osm_id"]] = wk
        except RuntimeError as ex:
            return jsonify(error="OpenStreetMap server problem: %s" % ex), 502
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = db()
    added = 0
    for wk in found.values():
        cur = conn.execute(
            "INSERT OR IGNORE INTO walks (osm_id,name,ref,network,operator,description,distance_km,"
            "lat,lon,place,tags,geom,added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wk["osm_id"], wk["name"], wk["ref"], wk["network"], wk["operator"], wk["description"],
             wk["distance_km"], wk["lat"], wk["lon"], (request.get_json() or {}).get("place"),
             json.dumps(wk["tags"]), json.dumps(wk["chains"]), now),
        )
        added += cur.rowcount
    conn.commit()
    return jsonify(added=added, skipped=len(ids) - added)


@app.get("/api/library")
def api_library():
    q = (request.args.get("q") or "").strip()
    min_km = request.args.get("min_km", type=float)
    max_km = request.args.get("max_km", type=float)
    sort = request.args.get("sort", "added")
    sql = ("SELECT osm_id,name,ref,network,operator,description,distance_km,lat,lon,place,added_at,geom "
           "FROM walks WHERE 1=1")
    args = []
    for term in q.split():
        like = "%" + term.replace("%", "").replace("_", "") + "%"
        sql += " AND (name LIKE ? OR ref LIKE ? OR operator LIKE ? OR network LIKE ? OR description LIKE ? OR place LIKE ?)"
        args += [like] * 6
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
        chains = json.loads(r["geom"])
        items.append({
            "osm_id": r["osm_id"], "name": r["name"], "ref": r["ref"], "network": r["network"],
            "operator": r["operator"], "description": r["description"], "distance_km": r["distance_km"],
            "lat": r["lat"], "lon": r["lon"], "place": r["place"], "added_at": r["added_at"],
            "added": True, "line": [simplify(c) for c in chains[:20]],
        })
    return jsonify(results=items)


@app.get("/walks/<int:osm_id>.gpx")
def download_gpx(osm_id):
    row = db().execute("SELECT * FROM walks WHERE osm_id=?", (osm_id,)).fetchone()
    if not row:
        abort(404)
    return Response(
        build_gpx(row),
        mimetype="application/gpx+xml",
        headers={"Content-Disposition": 'attachment; filename="%s"' % safe_filename(row["name"], osm_id)},
    )


@app.get("/walks.zip")
def download_zip():
    ids = request.args.get("ids", "")
    try:
        id_list = [int(i) for i in ids.split(",") if i.strip()]
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
        for r in rows:
            z.writestr(safe_filename(r["name"], r["osm_id"]), build_gpx(r))
        z.writestr("ATTRIBUTION.txt", "Route data: " + ATTRIBUTION + "\n")
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="walkbox-walks.zip"'})


@app.delete("/api/walks/<int:osm_id>")
def api_delete(osm_id):
    conn = db()
    conn.execute("DELETE FROM walks WHERE osm_id=?", (osm_id,))
    conn.commit()
    return jsonify(ok=True)
