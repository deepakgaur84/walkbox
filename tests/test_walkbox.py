import io
import json
import os
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["VALHALLA_URL"] = "http://valhalla:8002"
os.environ["SEARCH_CACHE_SECONDS"] = "0"   # the search cache is switched on explicitly in its own test
import app as wb  # noqa: E402


def way_geom(*pts):
    return [{"lat": a, "lon": b} for a, b in pts]


def rel_way(*pts):
    return {"type": "way", "geometry": way_geom(*pts)}


# Beach walk made of 3 named footpath ways (A-B, B-C, C-D), ~1.1 km each in lat degrees
def seg(i):
    lat0 = -36.79 - i * 0.004
    return way_geom((lat0, 174.77), (lat0 - 0.004, 174.77))


def bounds_of(geom):
    lats, lons = [p["lat"] for p in geom], [p["lon"] for p in geom]
    return {"minlat": min(lats), "minlon": min(lons), "maxlat": max(lats), "maxlon": max(lons)}


REL_MEMBERS = {
    111: [rel_way((-41.0, 174.0), (-41.0, 174.01)), rel_way((-41.01, 174.02), (-41.0, 174.01)),
          rel_way((-41.01, 174.02), (-41.02, 174.03)), rel_way((-42.0, 175.0), (-42.0, 175.01))],
    222: [rel_way((-40, 170), (-40, 170.1))],
}
REL_TAGS = {
    111: {"name": "Test Loop & Ridge", "ref": "T1", "route": "hiking", "operator": "DOC",
          "description": "A <nice> walk", "sac_scale": "mountain_hiking"},
    222: {"route": "foot"},
}
WAYS = [  # (id, name, geometry)
    (1, "Takapuna Beach Walkway", seg(0)), (2, "Takapuna Beach Walkway", seg(1)), (3, "Takapuna Beach Walkway", seg(2)),
    (4, "Tiny Alley", way_geom((-36.8, 174.8), (-36.8005, 174.8))),
    (8, "Rocky Headland Track", seg(10)), (9, "Rocky Headland Track", seg(11)), (10, "Rocky Headland Track", seg(12)),
    (5, "Footpath", seg(5) + seg(6)),
]


def overpass_list():
    els = []
    for rid, members in REL_MEMBERS.items():
        geoms = [m["geometry"] for m in members]
        allp = [p for g in geoms for p in g]
        els.append({"type": "relation", "id": rid, "tags": REL_TAGS[rid], "bounds": bounds_of(allp)})
    for wid, name, geom in WAYS:
        els.append({"type": "way", "id": wid, "tags": {"name": name, "highway": "footway"}, "bounds": bounds_of(geom)})
    return {"elements": els}


around_log = []
around_mode = {"down": False}


def _way(wid, tags, *pts):
    return {"type": "way", "id": wid, "tags": dict({"highway": "footway"}, **tags), "geometry": [{"lat": a, "lon": b} for a, b in pts]}


# ways along the Takapuna walkway (lon 174.77, lat -36.790 -> -36.802): sand, boardwalk, asphalt; plus a street beside it
AROUND_WAYS = [
    _way(901, {"surface": "sand"}, (-36.790, 174.77), (-36.794, 174.77)),
    _way(902, {"bridge": "boardwalk"}, (-36.794, 174.77), (-36.798, 174.77)),
    _way(903, {"surface": "asphalt"}, (-36.798, 174.77), (-36.802, 174.77)),
    _way(904, {"highway": "residential"}, (-36.789, 174.7709), (-36.803, 174.7709)),
]


def fake_overpass(q):
    calls.append("overpass:" + q)
    if "out tags bb" in q:
        return overpass_list()
    if "rel(id:" in q:
        ids = [int(x) for x in q.split("rel(id:")[1].split(")")[0].split(",")]
        return {"elements": [{"type": "relation", "id": i, "tags": REL_TAGS[i], "members": REL_MEMBERS[i]}
                             for i in ids if i in REL_MEMBERS]}
    if '["name"="' in q:
        name = q.split('["name"="')[1].split('"]')[0]
        return {"elements": [{"type": "way", "id": wid, "tags": {"name": n, "highway": "footway"}, "geometry": g}
                             for wid, n, g in WAYS if n == name]}
    if "(around:" in q:
        around_log.append(q)
        if around_mode["down"]:
            raise RuntimeError("overpass-api.de: timed out")
        return {"elements": AROUND_WAYS}
    if "out tags center" in q:
        return PLACES_OVERPASS
    raise AssertionError("unexpected overpass query: " + q[:80])


DOC_DETAIL = {"features": [{
    "id": 7,
    "geometry": {"type": "LineString", "coordinates": [[174.0, -36.0], [174.0, -36.02], [174.01, -36.04]]},
    "properties": {"OBJECTID": 7, "name": "Test Kauri Track", "introduction": "Big trees.",
                   "difficulty": "Easy", "completionTime": "1 hr 30 min one way", "region": "Northland",
                   "walkingAndTrampingWebPage": "https://www.doc.govt.nz/x"},
}]}
DOC_LIST = {"features": [{"attributes": {
    "OBJECTID": 7, "name": "Test Kauri Track", "introduction": "Big trees.", "difficulty": "Easy",
    "completionTime": "1 hr 30 min one way", "region": "Northland",
    "walkingAndTrampingWebPage": "https://www.doc.govt.nz/x", "Shape__Length": 3200.0}}]}


def feat(i, name, walk, geom, **props):
    line = [[p["lon"], p["lat"]] for p in geom]
    base = {"OBJECTID": i, "Name": name, "Walk": walk, "Locality": "Takapuna", "LocalAuthority": "Auckland Council",
            "DogAccess": "OnLeash", "MobilityAccess": "Yes", "WebLink": "https://example.org/walkway",
            "Description": "Coastal boardwalk and sand.", "ManagedBy": "Auckland Council"}
    base.update(props)
    return {"id": i, "geometry": {"type": "LineString", "coordinates": line}, "properties": base}


NZ_ROWS = [
    (1, "Takapuna Beach Walkway", 2, seg(0), 445.0), (2, "Takapuna Beach Walkway", 3, seg(1), 445.0),
    (3, "Takapuna Beach Walkway", 2, seg(2), 445.0),
    (4, "Bit of Path", 1, way_geom((-36.8, 174.8), (-36.8008, 174.8)), 89.0),         # too short
    (5, "Test Kauri Track", 3, way_geom((-36.0, 174.0), (-36.05, 174.0)), 5500.0),   # duplicate of the DOC name
]
NZT_DETAIL = {"features": [feat(i, n, w, g) for i, n, w, g, _ in NZ_ROWS]}
NZT_LIST = {"features": [{"attributes": dict(feat(i, n, w, g)["properties"], Shape__Length=L)} for i, n, w, g, L in NZ_ROWS]}

PLACES_OVERPASS = {"elements": [
    {"type": "node", "id": 1, "lat": -36.7955, "lon": 174.7712, "tags": {"amenity": "cafe", "name": "Beach Café", "opening_hours": "Mo-Su 07:00-16:00"}},
    {"type": "node", "id": 2, "lat": -36.797, "lon": 174.77003, "tags": {"amenity": "bench"}},
    {"type": "node", "id": 3, "lat": -36.797, "lon": 174.7712, "tags": {"amenity": "bench"}},                 # 107 m off: too far for a bench
    {"type": "way", "id": 4, "center": {"lat": -36.80, "lon": 174.7699}, "tags": {"amenity": "toilets", "name": "Beach toilets"}},
    {"type": "node", "id": 5, "lat": -36.795, "lon": 174.7760, "tags": {"amenity": "drinking_water"}},         # ~530 m off
    {"type": "node", "id": 6, "lat": -36.801, "lon": 174.77, "tags": {"tourism": "viewpoint", "name": "Headland view"}},
    {"type": "node", "id": 7, "lat": -36.792, "lon": 174.77, "tags": {"amenity": "parking"}},                 # not a place we list
    {"type": "node", "id": 8, "lat": -36.7935, "lon": 174.7703, "tags": {"leisure": "picnic_table"}},
]}
WEATHER = {"current": {"temperature_2m": 17.4, "apparent_temperature": 16.1, "precipitation": 0.0, "weather_code": 61,
                       "wind_speed_10m": 14.2, "wind_gusts_10m": 27.9, "is_day": 1},
           "daily": {"time": ["2026-11-02", "2026-11-03", "2026-11-04"], "weather_code": [61, 3, 0],
                     "temperature_2m_max": [19.2, 18.0, 21.5], "temperature_2m_min": [12.1, 11.0, 12.9],
                     "precipitation_probability_max": [60, 20, 5], "precipitation_sum": [2.4, 0.0, 0.0],
                     "wind_speed_10m_max": [31.0, 22.0, 15.0], "uv_index_max": [5.1, 6.0, 8.2],
                     "sunrise": ["2026-11-02T06:10", "2026-11-03T06:09", "2026-11-04T06:07"],
                     "sunset": ["2026-11-02T19:55", "2026-11-03T19:56", "2026-11-04T19:57"]}}


def cpage(pid, idx, title, url, mime="image/jpeg", width=2000, artist="<a href='x'>Jo Photog</a>",
          lic="CC BY-SA 4.0", host="upload.wikimedia.org", desc="A <b>nice</b> view"):
    return {"pageid": pid, "index": idx, "title": "File:" + title, "imageinfo": [{
        "thumburl": "https://%s/thumb/%s" % (host, url), "descriptionurl": "https://commons.wikimedia.org/wiki/File:" + title,
        "mime": mime, "width": width,
        "extmetadata": {"Artist": {"value": artist}, "LicenseShortName": {"value": lic}, "ImageDescription": {"value": desc}}}]}


COMMONS = {"query": {"pages": {
    "1": cpage(1, 1, "Beach.jpg", "beach.jpg"),
    "2": cpage(2, 2, "Plan.svg", "plan.svg.png", mime="image/svg+xml"),     # wrong type
    "3": cpage(3, 3, "Tiny.jpg", "tiny.jpg", width=200),                    # too small
    "4": cpage(4, 4, "Evil.jpg", "evil.jpg", host="evil.example.com"),      # wrong host
    "5": cpage(5, 5, "Pier.jpg", "pier.jpg", artist="Unknown <i>author</i>"),
}}}


class Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code = data, code

    def json(self):
        return self._d


calls = []
get_log = []


def fake_get(url, **kw):
    calls.append(url)
    params = kw.get("params") or {}
    get_log.append((url, params))
    if "api.open-meteo.com/v1/forecast" in url:
        return Resp(WEATHER)
    if "open-meteo" in url or "elevation" in url:
        n = len(params["latitude"].split(","))
        return Resp({"elevation": [100 + (i % 10) * 3 for i in range(n)]})
    if "commons.wikimedia.org" in url:
        return Resp(COMMONS)
    light = params.get("returnGeometry") == "false"
    if "b5ADKIcWivL5vNaV" in url:
        return Resp(NZT_LIST if light else NZT_DETAIL)
    return Resp(DOC_LIST if light else DOC_DETAIL)


wb.overpass = fake_overpass
wb.requests.get = fake_get
c = wb.app.test_client()

# --- helpers
chains = wb.chain_segments([[(0, 0), (0, 1)], [(1, 1), (0, 1)], [(1, 1), (2, 2)]])
assert len(chains) == 1 and len(chains[0]) == 4
assert wb.route_type_of([[(0, 0), (0, 0.01), (0.01, 0.01), (0.0001, 0.0001)]]) == "Loop"
assert wb.route_type_of([[(0, 0), (0, 0.05)]]) == "Point to point"
assert wb.est_minutes(5, 100) == 70 and wb.fmt_minutes(70) == "1 h 10 min"

# --- search returns a light list: no route lines, no geometry downloads
body = {"south": -36.9, "west": 174.7, "north": -36.7, "east": 174.9}
r = c.post("/api/search", json=body)
assert r.status_code == 200, r.data
res = r.get_json()["results"]
by_name = {x["name"]: x for x in res}
assert all("line" not in x for x in res), "search must not return route geometry"
oq = [x for x in calls if x.startswith("overpass:")][-1]
assert "out tags bb" in oq and "out tags geom" not in oq and "->.r;" in oq and "->.p;" in oq
assert "[bbox:-36.900000,174.700000,-36.700000,174.900000]" in oq                       # one global bbox
assert '["route"="hiking"]' in oq and '["highway"="footway"]' in oq                      # exact matches, not regular expressions
assert '"highway"~' not in oq and '["route"~' not in oq
assert isinstance(r.get_json()["ms"], int)
assert all(p.get("returnGeometry") == "false" for u, p in get_log if "arcgis" in u or "b5ADKIcWivL5vNaV" in u)

assert "Test Loop & Ridge" in by_name and "Test Kauri Track" in by_name and "Takapuna Beach Walkway" in by_name, list(by_name)
assert "Tiny Alley" not in by_name and "Footpath" not in by_name and "Bit of Path" not in by_name
walkway = by_name["Takapuna Beach Walkway"]
assert walkway["source"] == "nz-track" and not walkway["approx"] and 1.3 < walkway["distance_km"] < 1.4, walkway
assert walkway["difficulty"] == "Walking track" and walkway["region"] == "Takapuna"
assert "Dogs: on lead only." in walkway["description"] and walkway["link"] == "https://example.org/walkway"
assert sum(1 for x in res if x["name"] == "Takapuna Beach Walkway") == 1       # OSM duplicate dropped
kauri = by_name["Test Kauri Track"]
assert kauri["source"] == "doc" and kauri["distance_km"] == 3.2 and kauri["difficulty"] == "Easy"
assert kauri["time_text"].startswith("1 hr") and sum(1 for x in res if x["name"] == "Test Kauri Track") == 1
loop = by_name["Test Loop & Ridge"]
assert loop["source"] == "osm-route" and loop["distance_km"] is None and loop["difficulty"] == "Moderate" and loop["ref"] == "T1"
assert loop["time_text"] is None and by_name["Unnamed route 222"]["osm_id"] == 222
rocky = by_name["Rocky Headland Track"]
assert rocky["source"] == "osm-path" and rocky["approx"] and rocky["distance_km"] >= 0.8
srcs = [x["source"] for x in res]
assert res[0]["source"] == "doc" and srcs.index("nz-track") < srcs.index("osm-route") < srcs.index("osm-path")

# source selection / validation
calls.clear()
r = c.post("/api/search", json=dict(body, sources=["doc"]))
assert [x["source"] for x in r.get_json()["results"]] == ["doc"] and not any(x.startswith("overpass:") for x in calls)
assert c.post("/api/search", json=dict(body, sources=["bogus"])).status_code == 400
calls.clear()
c.post("/api/search", json={"south": 51.4, "west": -0.3, "north": 51.6, "east": -0.1, "sources": ["doc"]})
assert not any("arcgis" in x for x in calls)                                   # DOC is skipped outside NZ
assert c.post("/api/search", json={"south": 0, "west": 0, "north": 5, "east": 5}).status_code == 400
assert c.post("/api/search", json={}).status_code == 400

# one source failing does not hide the others
def boom(q):
    raise RuntimeError("overpass-api.de: timed out")
wb.overpass = boom
j = c.post("/api/search", json=body).get_json()
assert j["warnings"] and any(x["source"] == "doc" for x in j["results"]) and not any(x["source"] == "osm-route" for x in j["results"])
wb.overpass = fake_overpass
c.post("/api/search", json=body)      # refill the cache

# --- repeated searches of the same area are served from a short-lived cache
wb.SEARCH_TTL = 900; wb._search_cache.clear()
ov = lambda: sum(1 for x in calls if x.startswith("overpass:") and "out tags bb" in x)
c.post("/api/search", json=body); n_ov = ov()
again = c.post("/api/search", json=body).get_json()
assert ov() == n_ov and len(again["results"]) == len(res)                                 # no new Overpass query
assert all("line" not in x for x in again["results"])
wb.overpass = boom; wb._search_cache.clear()
assert c.post("/api/search", json=dict(body, sources=["routes"])).status_code == 502      # a failed search is reported...
wb.overpass = fake_overpass
assert c.post("/api/search", json=dict(body, sources=["routes"])).status_code == 200      # ...and never cached
wb.SEARCH_TTL = 0; wb._search_cache.clear()
c.post("/api/search", json=body)

# --- detail on click: each source loads its own route, keeping the list's id
calls.clear()
d = c.post("/api/detail", json={"id": 111}).get_json()
assert d["osm_id"] == 111 and len(d["line"]) == 2 and d["distance_km"] > 1 and d["route_type"] and "Moderate" == d["difficulty"], d
assert any("rel(id:111)" in x for x in calls if x.startswith("overpass:"))
dn = c.post("/api/detail", json={"id": walkway["osm_id"]}).get_json()
assert dn["osm_id"] == walkway["osm_id"] and len(dn["line"]) == 1 and 1.3 < dn["distance_km"] < 1.4 and dn["difficulty"] == "Walking track"
dd = c.post("/api/detail", json={"id": kauri["osm_id"]}).get_json()
assert dd["osm_id"] == kauri["osm_id"] and dd["line"] and dd["link"] == "https://www.doc.govt.nz/x"
assert any("OBJECTID=7" in p.get("where", "") for u, p in get_log)
dr = c.post("/api/detail", json={"id": rocky["osm_id"]}).get_json()
assert dr["osm_id"] == rocky["osm_id"] and dr["name"] == "Rocky Headland Track" and dr["distance_km"] > 1.0 and dr["line"], dr
assert any('["name"="Rocky Headland Track"]' in x for x in calls if x.startswith("overpass:"))
nz_where = [p["where"] for u, p in get_log if "b5ADKIcWivL5vNaV" in u and "returnGeometry" not in p][-1]
assert "Name='Takapuna Beach Walkway'" in nz_where and "Walk IN" in nz_where
n_calls = len(calls)
c.post("/api/detail", json={"id": 111}); assert len(calls) == n_calls          # second click is served from the cache
assert c.post("/api/detail", json={"id": 424242}).status_code == 409
assert c.post("/api/detail", json={"id": "x"}).status_code == 400

# --- add fetches the routes for walks that were never clicked, computes ascent, dedupes
fresh = c.post("/api/search", json=body).get_json()["results"]
assert all("line" not in x for x in fresh)
nz_id = walkway["osm_id"]
ids = [111, nz_id, kauri["osm_id"], 222]
r = c.post("/api/walks", json={"ids": ids, "place": "Takapuna, Auckland"})
assert r.get_json()["added"] == 4, r.data
assert c.post("/api/walks", json={"ids": [111]}).get_json()["added"] == 0
assert c.post("/api/walks", json={"ids": [wb.PATH_BASE + 5]}).status_code == 409
by_name = {x["name"]: x for x in fresh}
res = fresh

# --- library
lib = lambda q: c.get("/api/library?" + q).get_json()["results"]
assert len(lib("")) == 4
assert [x["name"] for x in lib("q=kauri")] == ["Test Kauri Track"]
assert [x["name"] for x in lib("q=northland")] == ["Test Kauri Track"]
assert [x["name"] for x in lib("q=takapuna+walkway")] == ["Takapuna Beach Walkway"]
assert [x["name"] for x in lib("q=moderate")] == ["Test Loop & Ridge"]
assert lib("min_km=9999") == [] and len(lib("sort=name")) == 4
w = next(x for x in lib("q=walkway"))
assert w["ascent_m"] and w["ascent_m"] > 0 and "↑" not in w["time_text"] and w["added"]

# --- GPX per source
ns = {"g": "http://www.topografix.com/GPX/1/1"}
r = c.get("/walks/111.gpx")
root = ET.fromstring(r.data)
assert len(root.findall(".//g:trkseg", ns)) == 2 and "OpenStreetMap" in r.data.decode()
assert root.find("g:trk/g:name", ns).text == "Test Loop & Ridge"
doc_id = by_name["Test Kauri Track"]["osm_id"]
rd = c.get("/walks/%d.gpx" % doc_id).data.decode()
assert "Department of Conservation" in rd and "doc.govt.nz/x" in rd and "CC BY 4.0" in rd
ET.fromstring(rd.encode())
assert c.get("/walks/999.gpx").status_code == 404

# --- zip with per-source attribution
z = zipfile.ZipFile(io.BytesIO(c.get("/walks.zip").data))
names = z.namelist()
assert len(names) == 5 and "ATTRIBUTION.txt" in names, names  # zip taken before import
att = z.read("ATTRIBUTION.txt").decode()
assert "OpenStreetMap" in att and "Department of Conservation" in att
for n in names:
    if n.endswith(".gpx"):
        ET.fromstring(z.read(n))
assert c.get("/walks.zip?ids=111").status_code == 200

# --- old database (before new columns) is migrated in place
import sqlite3
old = os.path.join(tempfile.mkdtemp(), "old.db")
con = sqlite3.connect(old)
con.executescript(wb.SCHEMA)
con.execute("INSERT INTO walks VALUES (5,'Old walk',NULL,NULL,NULL,NULL,2.0,0,0,NULL,'{}','[[[0,0],[0,0.02]]]','2026-01-01')")
con.commit(); con.close()
wb.DB_PATH = old
lst = c.get("/api/library").get_json()["results"]
assert lst[0]["name"] == "Old walk" and lst[0]["source"] == "osm-route"
assert c.get("/walks/5.gpx").status_code == 200

# --- back to the main test database
wb.DB_PATH = os.environ["DB_PATH"]

# --- NZ track GPX attribution
nz_id = walkway["osm_id"]
g = c.get("/walks/%d.gpx" % nz_id).data.decode()
assert "Outdoor Access Commission" in g and "example.org/walkway" in g
ET.fromstring(g.encode())

# --- GPX import
GPX = b"""<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
<metadata><name>My Coast Walk</name><desc>Done on a sunny day</desc></metadata>
<trk><trkseg>""" + b"".join(
    b'<trkpt lat="%.5f" lon="174.77000"><ele>%d</ele></trkpt>' % (-36.80 - i * 0.002, 10 + (i % 2) * 5)
    for i in range(12)) + b"""</trkseg></trk></gpx>"""
RTE = b"""<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><rte><name>Planned</name>
<rtept lat="-36.8" lon="174.7"/><rtept lat="-36.81" lon="174.7"/></rte></gpx>"""
EVIL = b"""<?xml version="1.0"?><!DOCTYPE gpx [<!ENTITY a "aaaa">]><gpx version="1.1"/>"""


def upload(*pairs):
    return c.post("/api/import", data={"files": [(io.BytesIO(d), n) for n, d in pairs]},
                  content_type="multipart/form-data")


r = upload(("walk.gpx", GPX), ("planned.gpx", RTE), ("evil.gpx", EVIL), ("junk.gpx", b"hello"))
j = r.get_json()
assert r.status_code == 200 and j["added"] == 2 and len(j["errors"]) == 2, j
assert any("DOCTYPE" in e for e in j["errors"]) and any("not a valid GPX" in e for e in j["errors"])
imp = [x for x in lib("") if x["source"] == "import"]
mine = next(x for x in imp if x["name"] == "My Coast Walk")
assert mine["distance_km"] > 0.4 and mine["ascent_m"] == 30.0 and mine["description"] == "Done on a sunny day", mine
assert any(x["name"] == "Planned" for x in imp)  # route fallback
assert upload(("walk.gpx", GPX)).get_json()["skipped"] == 1  # duplicate
assert c.post("/api/import", data={}, content_type="multipart/form-data").status_code == 400
ig = c.get("/walks/%d.gpx" % mine["osm_id"]).data.decode()
assert "Imported by the user" in ig and "My Coast Walk" in ig
ET.fromstring(ig.encode())
assert [x["name"] for x in lib("q=sunny")] == ["My Coast Walk"]

# --- Photos from Wikimedia Commons
ph = c.get("/api/walks/%d/photos" % nz_id)
pj = ph.get_json()
assert ph.status_code == 200, pj
titles = [x["title"] for x in pj["photos"]]
assert titles == ["Beach.jpg", "Pier.jpg"], titles  # svg, tiny and non-Wikimedia host dropped, deduped across 3 points
first = pj["photos"][0]
assert first["by"] == "Jo Photog" and first["license"] == "CC BY-SA 4.0" and first["caption"] == "A nice view"
assert first["thumb"].startswith("https://upload.wikimedia.org/") and first["page"].startswith("https://commons.wikimedia.org/wiki/File:")
assert pj["photos"][1]["by"] == "Unknown author"
assert c.get("/api/walks/999999/photos").status_code == 404
wb._rate_calls.clear(); wb.PHOTOS_PER_MIN = 2
assert [c.get("/api/walks/%d/photos" % nz_id).status_code for _ in range(3)] == [200, 200, 429]
wb.PHOTOS_PER_MIN = 30; wb._rate_calls.clear()
real_get = wb.requests.get
def down(url, **kw):
    raise wb.requests.ConnectionError("Temporary failure in name resolution")
wb.requests.get = down
assert c.get("/api/walks/%d/photos" % nz_id).status_code == 502
wb.requests.get = real_get

# --- my rating and notes
n = c.put("/api/walks/%d/note" % nz_id, json={"rating": 4, "note": "  Windy but lovely, park at the surf club  "})
assert n.status_code == 200 and n.get_json()["my_rating"] == 4
w = next(x for x in lib("q=walkway") if x["osm_id"] == nz_id)
assert w["my_rating"] == 4 and w["my_note"] == "Windy but lovely, park at the surf club"
assert [x["osm_id"] for x in lib("q=surf+club")] == [nz_id]            # notes are searchable
assert c.put("/api/walks/%d/note" % nz_id, json={"rating": 6}).status_code == 400
assert c.put("/api/walks/%d/note" % nz_id, json={"rating": "x"}).status_code == 400
assert c.put("/api/walks/999999/note", json={"rating": 3}).status_code == 404
c.put("/api/walks/%d/note" % nz_id, json={"rating": None, "note": ""})  # clearing works
w = next(x for x in lib("q=walkway") if x["osm_id"] == nz_id)
assert w["my_rating"] is None and w["my_note"] is None
c.put("/api/walks/%d/note" % nz_id, json={"rating": 5, "note": "Great"})

# --- Valhalla routing (faked engine)
def enc6(points):
    out, plat, plon = [], 0, 0
    for lat, lon in points:
        la, lo = round(lat * 1e6), round(lon * 1e6)
        for v in (la - plat, lo - plon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63)); v >>= 5
            out.append(chr(v + 63))
        plat, plon = la, lo
    return "".join(out)


RPTS = [(-36.7900, 174.7700), (-36.7910, 174.7712), (-36.7925, 174.7720), (-36.7940, 174.7735)]
back = wb.decode_polyline6(enc6(RPTS))
assert all(abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6 for a, b in zip(back, RPTS))

VALHALLA_OK = {"trip": {"status": 0, "summary": {"length": 1.2, "time": 900}, "legs": [{
    "shape": enc6(RPTS), "maneuvers": [
        {"type": 1, "instruction": "Walk north on The Promenade.", "length": 0.3, "time": 200, "begin_shape_index": 0},
        {"type": 15, "instruction": "Turn left onto Hurstmere Road.", "length": 0.5, "time": 400, "begin_shape_index": 2},
        {"type": 4, "instruction": "You have arrived at your destination.", "length": 0, "time": 0, "begin_shape_index": 3}]}]}}
vcalls = []
vmode = {"resp": None, "exc": None}


def fake_vpost(url, **kw):
    vcalls.append((url, kw.get("json")))
    if vmode["exc"]:
        raise vmode["exc"]
    return vmode["resp"]


real_post = wb.requests.post
wb.requests.post = fake_vpost
vmode["resp"] = Resp(VALHALLA_OK)

start = first_chain_start = wb.json.loads(wb.sqlite3.connect(os.environ["DB_PATH"]).execute(
    "SELECT geom FROM walks WHERE osm_id=?", (nz_id,)).fetchone()[0])[0][0]
r = c.post("/api/route", json={"mode": "to_start", "from": [-36.79, 174.77], "to": start})
rj = r.get_json()
assert r.status_code == 200, rj
url, payload = vcalls[-1]
assert url == "http://valhalla:8002/route" and payload["costing"] == "pedestrian" and len(payload["locations"]) == 2
assert payload["locations"][0] == {"lat": -36.79, "lon": 174.77} and payload["directions_options"]["units"] == "kilometers"
assert rj["distance_km"] == 1.2 and rj["minutes"] == 15 and len(rj["shape"]) == 4 and len(rj["steps"]) == 3
assert rj["steps"][1]["text"] == "Turn left onto Hurstmere Road." and abs(rj["steps"][1]["at"][0] - RPTS[2][0]) < 1e-6

r = c.post("/api/route", json={"mode": "along", "walk_id": nz_id})
assert r.status_code == 200
url, payload = vcalls[-1]
assert url == "http://valhalla:8002/trace_route" and payload["shape_match"] == "map_snap" and 2 <= len(payload["shape"]) <= 400

assert c.post("/api/route", json={"mode": "along", "walk_id": 999999}).status_code == 404
assert c.post("/api/route", json={"mode": "nope"}).status_code == 400
assert c.post("/api/route", json={"mode": "to_start", "from": [200, 0], "to": [0, 0]}).status_code == 400
assert c.post("/api/route", json={"mode": "to_start", "from": "x", "to": [0, 0]}).status_code == 400

vmode["resp"] = Resp({"error_code": 171, "error": "No suitable edges near location"}, 400)
r = c.post("/api/route", json={"mode": "to_start", "from": [51.5, -0.1], "to": [51.51, -0.1]})
assert r.status_code == 502 and "New Zealand only" in r.get_json()["error"]
vmode["exc"] = wb.requests.ConnectionError("Connection refused")
r = c.post("/api/route", json={"mode": "to_start", "from": [-36.79, 174.77], "to": [-36.8, 174.78]})
assert r.status_code == 502 and "building the New Zealand map tiles" in r.get_json()["error"]
vmode["exc"] = None
wb.requests.post = real_post

assert c.get("/api/route/status").get_json() == {"enabled": True, "ready": True}
good_get = wb.requests.get
wb.requests.get = down
assert c.get("/api/route/status").get_json() == {"enabled": True, "ready": False}
wb.requests.get = good_get
assert b"const ROUTING = true" in c.get("/").data
wb.VALHALLA_URL = ""
assert c.post("/api/route", json={"mode": "to_start", "from": [0, 0], "to": [0, 1]}).status_code == 404
assert c.get("/api/route/status").get_json() == {"enabled": False, "ready": False}
assert b"const ROUTING = false" in c.get("/").data
wb.VALHALLA_URL = "http://valhalla:8002"

# --- Elevation profile for the map view
ecalls = []
orig_get = wb.requests.get
def counting_get(url, **kw):
    if "open-meteo" in url or "elevation" in url:
        ecalls.append(len(kw["params"]["latitude"].split(",")))
    return orig_get(url, **kw)
wb.requests.get = counting_get
pr = c.get("/api/walks/%d/profile" % nz_id)
pj = pr.get_json()
assert pr.status_code == 200, pj
assert len(pj["dist_km"]) == len(pj["elev_m"]) >= 2 and pj["dist_km"][0] == 0 and abs(pj["dist_km"][-1] - pj["total_km"]) < 0.01
assert pj["total_km"] > 1.0 and pj["min"] <= pj["max"] and all(b >= a for a, b in zip(pj["dist_km"], pj["dist_km"][1:]))
first_calls = len(ecalls)
assert first_calls >= 1 and all(n <= 100 for n in ecalls)                      # batches of at most 100 points
assert c.get("/api/walks/%d/profile" % nz_id).get_json() == pj and len(ecalls) == first_calls   # cached in the database
assert c.get("/api/walks/999999/profile").status_code == 404
# a walk that is only in the recent search results (not in the box) also works, and is not stored
res2 = c.post("/api/search", json=body).get_json()["results"]
rocky = next(x for x in res2 if x["name"] == "Rocky Headland Track")
assert c.get("/api/walks/%d/profile" % rocky["osm_id"]).status_code == 200
assert wb.sqlite3.connect(os.environ["DB_PATH"]).execute("SELECT COUNT(*) FROM walks WHERE osm_id=?", (rocky["osm_id"],)).fetchone()[0] == 0
# elevation outage -> clear error, nothing cached
wb._rate_calls.clear()
other = [x for x in res2 if x["source"] == "osm-route"][0]
wb.requests.get = down
r500 = c.get("/api/walks/%d/profile" % other["osm_id"])
assert r500.status_code == 502 and "Elevation profile unavailable" in r500.get_json()["error"]
wb.requests.get = orig_get
pts, tot = wb.sample_profile([(0, 0), (0, 0.01), (0.01, 0.01)], n=11)
assert len(pts) == 11 and abs(pts[0][2]) < 1e-9 and abs(pts[-1][2] - tot) < 1e-6 and abs(pts[5][0]) < 1e-6 and abs(pts[5][1] - 0.01) < 1e-3
page = c.get("/").data
assert b'id="elev"' in page and b'id="livebar"' in page and b"selectWalk" in page

# --- Weather
good_get = wb.requests.get
wr = c.get("/api/weather?lat=-36.79&lon=174.77")
wj = wr.get_json()
assert wr.status_code == 200, wj
assert wj["now"]["text"] == "Light rain" and wj["now"]["temp"] == 17.4 and wj["now"]["gust"] == 27.9 and len(wj["days"]) == 3
assert wj["days"][0]["sunset"] == "19:55" and wj["days"][0]["rain_prob"] == 60 and wj["days"][2]["text"] == "Clear sky"
assert wj["days"][0]["tmin"] == 12.1 and wj["days"][0]["uv"] == 5.1 and wj["attribution"].startswith("Weather data by Open-Meteo")
forecasts = lambda: sum(1 for u in calls if "v1/forecast" in u)
n0 = forecasts(); assert c.get("/api/weather?lat=-36.79&lon=174.77").status_code == 200 and forecasts() == n0   # cached for 10 minutes
assert c.get("/api/weather").status_code == 400 and c.get("/api/weather?lat=999&lon=0").status_code == 400
wb.requests.get = down
r502 = c.get("/api/weather?lat=-41.3&lon=174.8")
assert r502.status_code == 502 and "Weather unavailable" in r502.get_json()["error"]
wb.requests.get = good_get
assert wb.wmo(61) == ("Light rain", "🌦️") and wb.wmo(12345)[0] == "Unknown"

# --- Nearby places (food, water, toilets, rest spots, viewpoints)
assert wb.classify_place({"amenity": "cafe"}) == ("food", "Café")
assert wb.classify_place({"shop": "bakery"})[0] == "food" and wb.classify_place({"amenity": "parking"}) == (None, None)
assert wb.classify_place({"leisure": "picnic_table"}) == ("rest", "Picnic spot") and wb.classify_place({"tourism": "viewpoint"})[0] == "view"
line = [(0.0, 0.0), (0.01, 0.0)]; cum2 = [0.0, wb.haversine_m(line[0], line[1])]
al, off = wb.along_and_off(0.005, 0.0009, line, cum2)
assert abs(al - cum2[1] / 2) < 5 and 95 < off < 105
pc = lambda: sum(1 for x in calls if x.startswith("overpass:") and "out tags center" in x)
pj = c.get("/api/walks/%d/places" % nz_id).get_json()
assert pj["counts"] == {"food": 1, "water": 0, "toilets": 1, "rest": 2, "view": 1}, pj["counts"]
byname = {x["name"]: x for x in pj["places"] if x["name"]}
cafe = byname["Beach Café"]
assert cafe["kind"] == "food" and cafe["hours"] == "Mo-Su 07:00-16:00" and 90 < cafe["off_m"] < 130 and 0.4 < cafe["along_km"] < 0.8, cafe
assert byname["Beach toilets"]["kind"] == "toilets" and byname["Headland view"]["kind"] == "view"   # a way's centre point works too
assert all(x["kind"] != "rest" or x["off_m"] <= 60 for x in pj["places"])                          # the far bench is left out
alongs = [x["along_km"] for x in pj["places"]]
assert alongs == sorted(alongs) and all(0 <= a_ <= pj["total_km"] + 0.01 for a_ in alongs)
assert pj["kinds"]["food"]["icon"] == "☕" and pj["kinds"]["water"]["label"] == "Drinking water"
n1 = pc(); assert c.get("/api/walks/%d/places" % nz_id).get_json() == pj and pc() == n1                # cached for an hour
assert c.get("/api/walks/999999/places").status_code == 404
assert c.get("/api/walks/%d/places" % rocky["osm_id"]).status_code == 200                           # a search result that was never opened
wb._places_cache.clear(); wb.overpass = boom
e502 = c.get("/api/walks/%d/places" % nz_id)
assert e502.status_code == 502 and "Nearby places unavailable" in e502.get_json()["error"]
wb.overpass = fake_overpass; wb._places_cache.clear()
page = c.get("/").data
assert b'id="info"' in page and b"renderPlaces" in page and b"openWalk" in page

# --- Swap start / end: reversed GPX and reversed turn-by-turn
fw = c.get("/walks/%d.gpx" % nz_id); rw = c.get("/walks/%d.gpx?reverse=1" % nz_id)
f_pts = ET.fromstring(fw.data).findall(".//g:trkpt", ns); r_pts = ET.fromstring(rw.data).findall(".//g:trkpt", ns)
assert len(f_pts) == len(r_pts) > 2
assert (f_pts[0].get("lat"), f_pts[0].get("lon")) == (r_pts[-1].get("lat"), r_pts[-1].get("lon"))      # start and end swapped
assert (f_pts[-1].get("lat"), f_pts[-1].get("lon")) == (r_pts[0].get("lat"), r_pts[0].get("lon"))
assert "(reversed)" in rw.data.decode() and "(reversed)" not in fw.data.decode()
assert "reversed" in rw.headers["Content-Disposition"] and "reversed" not in fw.headers["Content-Disposition"]
assert c.get("/walks/%d.gpx?reverse=0" % nz_id).data == fw.data
real_post2 = wb.requests.post
sent = []
wb.requests.post = lambda url, **kw: (sent.append(kw["json"]) or Resp(VALHALLA_OK))
c.post("/api/route", json={"mode": "along", "walk_id": nz_id})
c.post("/api/route", json={"mode": "along", "walk_id": nz_id, "reverse": True})
fwd, rev = sent[-2]["shape"], sent[-1]["shape"]
assert fwd[0] == rev[-1] and fwd[-1] == rev[0] and len(fwd) == len(rev) > 2
wb.requests.post = real_post2
page = c.get("/").data
assert b'id="revbtn"' in page and b"reverseWalk" in page and b"reverse=1" in page

# --- walks shown the other way round (e.g. to start at the searched place) are saved that way
gl = c.post("/api/search", json=dict(body, sources=["paths"])).get_json()["results"]
rk = next(x for x in gl if x["name"] == "Rocky Headland Track")
fwd_line = c.post("/api/detail", json={"id": rk["osm_id"]}).get_json()["line"][0]
assert c.post("/api/walks", json={"ids": [rk["osm_id"]], "reverse": [rk["osm_id"]]}).get_json()["added"] == 1
saved = next(x for x in c.get("/api/library?q=rocky").get_json()["results"] if x["osm_id"] == rk["osm_id"])["line"][0]
assert saved[0] == fwd_line[-1] and saved[-1] == fwd_line[0], "the box copy starts where the reversed view started"
assert c.delete("/api/walks/%d" % rk["osm_id"]).get_json()["ok"]
assert b"shouldReverseForOrigin" in c.get("/").data and b"autoSwapped" in c.get("/").data

# --- Customised walks: save as new / replace, elevation for an unsaved route, leg routing
base_chain = wb.json.loads(wb.sqlite3.connect(os.environ["DB_PATH"]).execute("SELECT geom FROM walks WHERE osm_id=?", (nz_id,)).fetchone()[0])[0]
trimmed = base_chain[1:-1]
cust = {"base_id": nz_id, "mode": "new", "chain": trimmed, "name": "Takapuna Beach Walkway (mine)", "description": "Starts at the cafe",
        "difficulty": "Easy", "rating": 4, "note": "Best at sunrise"}
cr = c.post("/api/customise", json=cust)
cj = cr.get_json()
assert cr.status_code == 200 and cj["source"] == "custom" and cj["source_label"] == "Custom" and cj["added"], cj
assert cj["osm_id"] >= wb.CUSTOM_BASE and cj["osm_id"] != nz_id and cj["name"].endswith("(mine)")
assert cj["distance_km"] < c.get("/api/library?q=takapuna+beach+walkway").get_json()["results"][0]["distance_km"] + 0.01
assert cj["my_rating"] == 4 and cj["my_note"] == "Best at sunrise" and cj["difficulty"] == "Easy" and cj["description"] == "Starts at the cafe"
assert cj["link"] == "https://example.org/walkway" and cj["region"] == "Takapuna" and cj["route_type"] and cj["ascent_m"]   # carried over / computed
orig = next(x for x in c.get("/api/library?q=takapuna").get_json()["results"] if x["osm_id"] == nz_id)
assert orig["name"] == "Takapuna Beach Walkway" and orig["source"] == "nz-track"                                          # the original is untouched
assert [x["name"] for x in c.get("/api/library?q=sunrise").get_json()["results"]] == ["Takapuna Beach Walkway (mine)"]    # notes are searchable
gp = c.get("/walks/%d.gpx" % cj["osm_id"]).data.decode()
assert "Customised by the user" in gp and "Outdoor Access Commission" in gp and "example.org/walkway" not in gp.split("<trk>")[1]
assert len(ET.fromstring(gp.encode()).findall(".//g:trkpt", ns)) == len(trimmed)
zatt = zipfile.ZipFile(io.BytesIO(c.get("/walks.zip").data)).read("ATTRIBUTION.txt").decode()
assert "Herenga" in zatt and "Customised by the user" in zatt
c2 = c.post("/api/customise", json=dict(cust, base_id=cj["osm_id"], name="Mine again")).get_json()    # customising a custom walk keeps the original credit
assert c2["osm_id"] != cj["osm_id"] and "Herenga" in c.get("/walks/%d.gpx" % c2["osm_id"]).data.decode()
# replace the original in the box
wb.sqlite3.connect(os.environ["DB_PATH"]).execute("UPDATE walks SET profile='{}' WHERE osm_id=222").connection.commit()
rep_chain = [(-40.0, 170.0), (-40.0, 170.02), (-40.01, 170.03)]
rr = c.post("/api/customise", json={"base_id": 222, "mode": "replace", "chain": rep_chain, "name": "Renamed 222"}).get_json()
assert rr["osm_id"] == 222 and rr["name"] == "Renamed 222" and rr["source"] == "osm-route" and 2.5 < rr["distance_km"] < 4.5, rr
assert wb.sqlite3.connect(os.environ["DB_PATH"]).execute("SELECT profile FROM walks WHERE osm_id=222").fetchone()[0] is None   # old elevation profile dropped
assert "Customised by the user." in c.get("/walks/222.gpx").data.decode() and "OpenStreetMap" in c.get("/walks/222.gpx").data.decode()
assert [x["name"] for x in c.get("/api/library?q=renamed").get_json()["results"]] == ["Renamed 222"]
# not allowed / invalid
assert c.post("/api/customise", json={"base_id": rocky["osm_id"], "mode": "replace", "chain": rep_chain}).status_code == 400   # only box walks can be replaced
okn = c.post("/api/customise", json={"base_id": rocky["osm_id"], "mode": "new", "chain": rep_chain, "name": "From a search result"})
assert okn.status_code == 200 and okn.get_json()["source"] == "custom"
for bad in [dict(cust, chain=[(0, 0)]), dict(cust, chain=[(0, 0), (0, 0.00001)]), dict(cust, chain=[(95, 0), (0, 1)]), dict(cust, chain="x"),
            dict(cust, mode="nope"), dict(cust, rating=9), dict(cust, chain=[(0.0, i * 1e-4) for i in range(6001)]), {"mode": "new"}]:
    assert c.post("/api/customise", json=bad).status_code == 400, bad
for wid in (cj["osm_id"], c2["osm_id"], okn.get_json()["osm_id"]):
    assert c.delete("/api/walks/%d" % wid).get_json()["ok"]
# elevation profile for a route that is not saved
wb._rate_calls.clear()
pc_ = c.post("/api/profile", json={"chain": base_chain})
assert pc_.status_code == 200 and len(pc_.get_json()["elev_m"]) >= 2 and pc_.get_json()["total_km"] > 1
assert c.post("/api/profile", json={"chain": [(0, 0), (0, 0.00001)]}).status_code == 400 and c.post("/api/profile", json={"chain": "x"}).status_code == 400
wb.requests.get = down
assert c.post("/api/profile", json={"chain": base_chain}).status_code == 502
wb.requests.get = good_get
# a leg between two edit points is a plain pedestrian route
wb.requests.post = fake_vpost
vmode["resp"] = Resp(VALHALLA_OK); vmode["exc"] = None
lg = c.post("/api/route", json={"mode": "leg", "from": [-36.79, 174.77], "to": [-36.80, 174.78]})
assert lg.status_code == 200 and vcalls[-1][0].endswith("/route") and len(lg.get_json()["shape"]) == 4
wb.requests.post = real_post

page = c.get("/").data
assert b'id="editbtn"' in page and b"startEdit" in page and b"edSave" in page and b"nearestLeg" in page

# --- Surface of the route and estimated difficulty
sc = wb.surface_class
assert sc({"highway": "footway", "surface": "sand"}) == "sand" and sc({"highway": "path", "surface": "fine_gravel"}) == "gravel"
assert sc({"highway": "steps"}) == "steps" and sc({"highway": "footway", "bridge": "boardwalk"}) == "wood"
assert sc({"highway": "residential"}) == "paved" and sc({"highway": "path"}) == "unknown" and sc({"highway": "track", "surface": "ground"}) == "dirt"
assert sc({"highway": "footway", "surface": "asphalt;concrete"}) == "paved" and sc({"highway": "path", "surface": "bare_rock"}) == "rock"
# matching: a street 9 m beside a mapped footpath must not win
straight = [(-36.790, 174.77), (-36.802, 174.77)]
runs, totals, extra = wb.match_surfaces(straight, [dict(tags=w["tags"], pts=[(p["lat"], p["lon"]) for p in w["geometry"]]) for w in AROUND_WAYS])
assert [r["cls"] for r in runs] == ["sand", "wood", "paved"], runs
assert runs[0]["a"] == 0.0 and runs[-1]["b"] == 1.0 and all(abs(a["b"] - b["a"]) < 1e-9 for a, b in zip(runs, runs[1:]))
assert abs(runs[0]["b"] - 1 / 3) < 0.03 and abs(runs[1]["b"] - 2 / 3) < 0.03
assert abs(sum(totals.values()) - wb.chain_length_m(straight)) < 5
none_runs, none_tot, _ = wb.match_surfaces(straight, [])
assert none_runs == [{"cls": "unknown", "a": 0.0, "b": 1.0}]                                             # nothing mapped: all unknown
far = [dict(tags={"highway": "footway", "surface": "sand"}, pts=[(-36.790, 174.7712), (-36.802, 174.7712)])]   # 110 m away: too far
assert wb.match_surfaces(straight, far)[0][0]["cls"] == "unknown"
tiny = [dict(tags={"highway": "footway", "surface": "sand"}, pts=[(-36.790, 174.77), (-36.796, 174.77)]),      # a 5 m blip of steps is absorbed
        dict(tags={"highway": "steps"}, pts=[(-36.79300, 174.77), (-36.79304, 174.77)])]
assert [r["cls"] for r in wb.match_surfaces(straight, tiny)[0]][:1] == ["sand"] and "steps" not in [r["cls"] for r in wb.match_surfaces(straight, tiny)[0]]
# the steepest stretch
assert round(wb.max_grade_pct({"dist_km": [0, .05, .1, .2, .3], "elev_m": [0, 0, 0, 10, 10]}), 1) == 10.0
assert wb.max_grade_pct({"dist_km": [0, 5], "elev_m": [0, 3000]}) == 45.0 and wb.max_grade_pct({"dist_km": [0, .05], "elev_m": [0, 9]}) == 0.0
# difficulty scoring across typical walks
dr = wb.difficulty_rating
flat = dr(2.6, 2, 1, {"paved": 2600}, 2600)
assert flat["label"] == "Easy" and flat["score"] < 1.5, flat
coast = dr(8.4, 49, 6, {"paved": 5000, "sand": 1500, "unknown": 1900}, 8400)
assert coast["label"] == "Easy" and 2 < coast["score"] < 3.5, coast
longer = dr(15.0, 257, 9, {"paved": 9000, "sand": 3000, "unknown": 3000}, 15000)
assert longer["label"] == "Moderate", longer
mountain = dr(10.0, 800, 28, {"rock": 4000, "dirt": 3000, "unknown": 3000}, 10000, sac="mountain_hiking")
assert mountain["label"] in ("Hard", "Very hard") and mountain["score"] > 7, mountain
assert dr(1.0, 5, 0, {"paved": 1000}, 1000, source_grade="Challenging tramping track")["label"] in ("Hard", "Very hard")   # an official grade sets a floor
assert dr(1.0, 5, 0, {"paved": 1000}, 1000, source_grade="Short walk")["label"] == "Easy"
assert any("official grade" in x for x in dr(1.0, 5, 0, {}, 1000, source_grade="Walking track")["reasons"])
assert any("surface:" in x and "paved" in x for x in coast["reasons"]) and any("climbing" in x for x in coast["reasons"])
assert all(0 <= x["score"] <= 10 for x in (flat, coast, longer, mountain)) and dr(100, 5000, 45, {"rock": 1e5}, 1e5, sac="difficult_alpine_hiking")["score"] == 10.0

# the endpoint: a walk in the box
around_log.clear(); wb._rate_calls.clear()
tr = c.get("/api/walks/%d/terrain" % nz_id)
tj = tr.get_json()
assert tr.status_code == 200, tj
assert [r["cls"] for r in tj["runs"]] == ["sand", "wood", "paved"] and tj["difficulty"]["label"] in ("Easy", "Moderate", "Hard", "Very hard")
assert tj["classes"]["sand"]["label"] == "Sand" and tj["classes"]["wood"]["label"] == "Boardwalk" and tj["classes"]["paved"]["colour"].startswith("#")
assert abs(sum(v["pct"] for v in tj["classes"].values()) - 100) <= 2 and tj["total_m"] > 1000 and tj["notes"] == []
assert tj["difficulty"]["reasons"] and "OpenStreetMap" in tj["attribution"] and len(around_log) == 1
assert "(around:" in around_log[0] and "restrict" not in around_log[0] and '"highway"!~"^(motorway' in around_log[0]       # no motorways, one corridor query
assert wb.sqlite3.connect(os.environ["DB_PATH"]).execute("SELECT terrain FROM walks WHERE osm_id=?", (nz_id,)).fetchone()[0]    # saved with the walk
assert c.get("/api/walks/%d/terrain" % nz_id).get_json() == tj and len(around_log) == 1                                  # no second lookup
# a search result that is not in the box is cached in memory only
tj2 = c.get("/api/walks/%d/terrain" % rocky["osm_id"]).get_json()
assert "runs" in tj2 and len(around_log) == 2
assert wb.sqlite3.connect(os.environ["DB_PATH"]).execute("SELECT COUNT(*) FROM walks WHERE osm_id=?", (rocky["osm_id"],)).fetchone()[0] == 0
c.get("/api/walks/%d/terrain" % rocky["osm_id"]); assert len(around_log) == 2
assert c.get("/api/walks/999999/terrain").status_code == 404
# OpenStreetMap or the elevation service down: still answers, says so, and does not remember the failure
wb._terrain_cache.clear(); around_mode["down"] = True
dj = c.get("/api/walks/%d/terrain" % rocky["osm_id"]).get_json()
assert dj["runs"] == [{"cls": "unknown", "a": 0.0, "b": 1.0}] and any("Surface data unavailable" in n for n in dj["notes"]) and dj["difficulty"]["label"]
around_mode["down"] = False
dj2 = c.get("/api/walks/%d/terrain" % rocky["osm_id"]).get_json()
n_around = len(around_log); assert dj2["notes"] == [] and dj2["runs"]
c.get("/api/walks/%d/terrain" % rocky["osm_id"]); assert len(around_log) == n_around                      # a good answer is remembered
wb._terrain_cache.clear(); wb._profile_mem.clear(); wb.requests.get = down
ej = c.get("/api/walks/%d/terrain" % rocky["osm_id"]).get_json()
assert any("Elevation unavailable" in n for n in ej["notes"]) and ej["runs"] and ej["difficulty"]["label"]
wb.requests.get = good_get; wb._terrain_cache.clear()
# replacing a walk drops its saved surface data
assert c.post("/api/customise", json={"base_id": nz_id, "mode": "replace", "chain": base_chain[1:-1], "name": "Takapuna Beach Walkway"}).status_code == 200
assert wb.sqlite3.connect(os.environ["DB_PATH"]).execute("SELECT terrain FROM walks WHERE osm_id=?", (nz_id,)).fetchone()[0] is None
assert b'id="legend"' in c.get("/").data and b"paintTerrain" in c.get("/").data and b"showDifficulty" in c.get("/").data

# --- page + delete
wb.DB_PATH = os.environ["DB_PATH"]
page = c.get("/").data
assert b"Walkbox" in page and b"srcDoc" in page and b"srcNz" in page and b"importBtn" in page
assert b"Photos & my notes" in page and b"Navigate" in page
assert b"gphoto" not in page and b"googleapis" not in page and b"GOOGLE_PLACES" not in page  # paid Places API stays removed
assert c.delete("/api/walks/111").get_json()["ok"]
assert len(lib("")) == 3 + 2
print("ALL TESTS PASSED")
