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
import app as wb  # noqa: E402


def way_geom(*pts):
    return [{"lat": a, "lon": b} for a, b in pts]


def rel_way(*pts):
    return {"type": "way", "geometry": way_geom(*pts)}


# Beach walk made of 3 named footpath ways (A-B, B-C, C-D), ~1.1 km each in lat degrees
def seg(i):
    lat0 = -36.79 - i * 0.004
    return way_geom((lat0, 174.77), (lat0 - 0.004, 174.77))


OVERPASS = {"elements": [
    {"type": "relation", "id": 111, "tags": {"name": "Test Loop & Ridge", "ref": "T1", "route": "hiking",
                                              "operator": "DOC", "description": "A <nice> walk",
                                              "sac_scale": "mountain_hiking"},
     "members": [rel_way((-41.0, 174.0), (-41.0, 174.01)),
                 rel_way((-41.01, 174.02), (-41.0, 174.01)),
                 rel_way((-41.01, 174.02), (-41.02, 174.03)),
                 rel_way((-42.0, 175.0), (-42.0, 175.01))]},
    {"type": "relation", "id": 222, "tags": {"route": "foot"}, "members": [rel_way((-40, 170), (-40, 170.1))]},
    {"type": "relation", "id": 333, "tags": {"name": "Empty"}, "members": []},
    # named paths: one walk made of three ways; one short path; one generic name
    {"type": "way", "id": 1, "tags": {"name": "Takapuna Beach Walkway", "highway": "footway"}, "geometry": seg(0)},
    {"type": "way", "id": 2, "tags": {"name": "Takapuna Beach Walkway", "highway": "footway"}, "geometry": seg(1)},
    {"type": "way", "id": 3, "tags": {"name": "Takapuna Beach Walkway", "highway": "path"}, "geometry": seg(2)},
    {"type": "way", "id": 4, "tags": {"name": "Tiny Alley", "highway": "footway"},
     "geometry": way_geom((-36.8, 174.8), (-36.8005, 174.8))},
    {"type": "way", "id": 8, "tags": {"name": "Rocky Headland Track", "highway": "path"}, "geometry": seg(10)},
    {"type": "way", "id": 9, "tags": {"name": "Rocky Headland Track", "highway": "path"}, "geometry": seg(11)},
    {"type": "way", "id": 10, "tags": {"name": "Rocky Headland Track", "highway": "path"}, "geometry": seg(12)},
    {"type": "way", "id": 5, "tags": {"name": "Footpath", "highway": "footway"}, "geometry": seg(5) + seg(6)},
]}

DOC = {"features": [{
    "id": 7,
    "geometry": {"type": "LineString", "coordinates": [[174.0, -36.0], [174.0, -36.02], [174.01, -36.04]]},
    "properties": {"OBJECTID": 7, "name": "Test Kauri Track", "introduction": "Big trees.",
                   "difficulty": "Easy", "completionTime": "1 hr 30 min one way", "region": "Northland",
                   "walkingAndTrampingWebPage": "https://www.doc.govt.nz/x"},
}]}


def feat(i, name, walk, geom, **props):
    line = [[p["lon"], p["lat"]] for p in geom]
    base = {"OBJECTID": i, "Name": name, "Walk": walk, "Locality": "Takapuna",
            "DogAccess": "OnLeash", "MobilityAccess": "Yes", "WebLink": "https://example.org/walkway",
            "Description": "Coastal boardwalk and sand.", "ManagedBy": "Auckland Council"}
    base.update(props)
    return {"id": i, "geometry": {"type": "LineString", "coordinates": line}, "properties": base}


NZT = {"features": [
    feat(1, "Takapuna Beach Walkway", 2, seg(0)),
    feat(2, "Takapuna Beach Walkway", 3, seg(1)),
    feat(3, "Takapuna Beach Walkway", 2, seg(2)),
    feat(4, "Bit of Path", 1, way_geom((-36.8, 174.8), (-36.8008, 174.8))),   # too short
    feat(5, "Test Kauri Track", 3, way_geom((-36.0, 174.0), (-36.05, 174.0))),  # duplicate of DOC name
]}


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


def fake_get(url, **kw):
    calls.append(url)
    if "open-meteo" in url or "elevation" in url:
        n = len(kw["params"]["latitude"].split(","))
        return Resp({"elevation": [100 + (i % 10) * 3 for i in range(n)]})
    if "commons.wikimedia.org" in url:
        return Resp(COMMONS)
    if "b5ADKIcWivL5vNaV" in url:
        return Resp(NZT)
    return Resp(DOC)


wb.overpass = lambda q: (calls.append("overpass:" + q) or OVERPASS)
wb.requests.get = fake_get
c = wb.app.test_client()

# --- helpers
chains = wb.chain_segments([[(0, 0), (0, 1)], [(1, 1), (0, 1)], [(1, 1), (2, 2)]])
assert len(chains) == 1 and len(chains[0]) == 4
assert wb.route_type_of([[(0, 0), (0, 0.01), (0.01, 0.01), (0.0001, 0.0001)]]) == "Loop"
assert wb.route_type_of([[(0, 0), (0, 0.05)]]) == "Point to point"
assert wb.est_minutes(5, 100) == 70 and wb.fmt_minutes(70) == "1 h 10 min"

# --- search (all sources, NZ area)
body = {"south": -36.9, "west": 174.7, "north": -36.7, "east": 174.9}
r = c.post("/api/search", json=body)
assert r.status_code == 200, r.data
res = r.get_json()["results"]
by_name = {x["name"]: x for x in res}
assert "Test Loop & Ridge" in by_name and "Test Kauri Track" in by_name
assert "Takapuna Beach Walkway" in by_name, list(by_name)
assert "Tiny Alley" not in by_name and "Footpath" not in by_name  # too short / generic
assert not any(x["name"] == "Empty" for x in res)
walkway = by_name["Takapuna Beach Walkway"]
assert walkway["source"] == "nz-track" and walkway["distance_km"] > 1.0 and len(walkway["line"]) == 1, walkway
assert walkway["difficulty"] == "Walking track" and walkway["region"] == "Takapuna", walkway  # hardest grade wins
assert "Dogs: on lead only." in walkway["description"] and walkway["link"] == "https://example.org/walkway"
assert sum(1 for x in res if x["name"] == "Takapuna Beach Walkway") == 1  # OSM duplicate dropped
assert "Bit of Path" not in by_name  # too short
assert sum(1 for x in res if x["name"] == "Test Kauri Track") == 1 and by_name["Test Kauri Track"]["source"] == "doc"
assert by_name["Rocky Headland Track"]["source"] == "osm-path"
assert by_name["Test Kauri Track"]["source"] == "doc" and by_name["Test Kauri Track"]["difficulty"] == "Easy"
assert by_name["Test Kauri Track"]["time_text"].startswith("1 hr")
assert by_name["Test Loop & Ridge"]["difficulty"] == "Moderate"  # from sac_scale
assert res[0]["source"] == "doc"  # DOC listed first
srcs = [x["source"] for x in res]
assert srcs.index("nz-track") < srcs.index("osm-route") < srcs.index("osm-path")

# single query to Overpass combines relations and paths
oq = [x for x in calls if x.startswith("overpass:")][-1]
assert "->.r;" in oq and "->.p;" in oq and ".r out tags geom" in oq and ".p out tags geom" in oq

# source selection / validation
calls.clear()
r = c.post("/api/search", json=dict(body, sources=["doc"]))
assert [x["source"] for x in r.get_json()["results"]] == ["doc"] and not any(x.startswith("overpass:") for x in calls)
assert c.post("/api/search", json=dict(body, sources=["bogus"])).status_code == 400
# DOC is skipped outside New Zealand
calls.clear()
c.post("/api/search", json={"south": 51.4, "west": -0.3, "north": 51.6, "east": -0.1, "sources": ["doc"]})
assert not any("arcgis" in x for x in calls)
assert c.post("/api/search", json={"south": 0, "west": 0, "north": 5, "east": 5}).status_code == 400
assert c.post("/api/search", json={}).status_code == 400

# one source failing does not hide the others
def boom(q):
    raise RuntimeError("overpass-api.de: timed out")
good = wb.overpass
wb.overpass = boom
r = c.post("/api/search", json=body)
j = r.get_json()
assert r.status_code == 200 and j["warnings"] and any(x["source"] == "doc" for x in j["results"])
wb.overpass = good
c.post("/api/search", json=body)  # refill cache

# --- add (computes ascent), dedupe, expired ids
ids = [111, walkway["osm_id"], by_name["Test Kauri Track"]["osm_id"], 222]
r = c.post("/api/walks", json={"ids": ids, "place": "Takapuna, Auckland"})
assert r.get_json()["added"] == 4, r.data
assert c.post("/api/walks", json={"ids": [111]}).get_json()["added"] == 0
assert c.post("/api/walks", json={"ids": [wb.PATH_BASE + 5]}).status_code == 409

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

# --- page + delete
wb.DB_PATH = os.environ["DB_PATH"]
page = c.get("/").data
assert b"Walkbox" in page and b"srcDoc" in page and b"srcNz" in page and b"importBtn" in page
assert b"Photos & my notes" in page and b"Navigate" in page
assert b"gphoto" not in page and b"googleapis" not in page and b"GOOGLE_PLACES" not in page  # paid Places API stays removed
assert c.delete("/api/walks/111").get_json()["ok"]
assert len(lib("")) == 3 + 2
print("ALL TESTS PASSED")
