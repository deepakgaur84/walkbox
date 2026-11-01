import io
import json
import os
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
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
    {"type": "way", "id": 5, "tags": {"name": "Footpath", "highway": "footway"}, "geometry": seg(5) + seg(6)},
]}

DOC = {"features": [{
    "id": 7,
    "geometry": {"type": "LineString", "coordinates": [[174.0, -36.0], [174.0, -36.02], [174.01, -36.04]]},
    "properties": {"OBJECTID": 7, "name": "Test Kauri Track", "introduction": "Big trees.",
                   "difficulty": "Easy", "completionTime": "1 hr 30 min one way", "region": "Northland",
                   "walkingAndTrampingWebPage": "https://www.doc.govt.nz/x"},
}]}


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
assert walkway["source"] == "osm-path" and walkway["distance_km"] > 1.0 and len(walkway["line"]) == 1, walkway
assert by_name["Test Kauri Track"]["source"] == "doc" and by_name["Test Kauri Track"]["difficulty"] == "Easy"
assert by_name["Test Kauri Track"]["time_text"].startswith("1 hr")
assert by_name["Test Loop & Ridge"]["difficulty"] == "Moderate"  # from sac_scale
assert res[0]["source"] == "doc"  # DOC listed first

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
assert len(names) == 5 and "ATTRIBUTION.txt" in names, names
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

# --- page + delete
wb.DB_PATH = os.environ["DB_PATH"]
page = c.get("/").data
assert b"Walkbox" in page and b"srcDoc" in page
assert c.delete("/api/walks/111").get_json()["ok"]
assert len(lib("")) == 3
print("ALL TESTS PASSED")
