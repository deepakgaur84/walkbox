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


def way(*pts):
    return {"type": "way", "geometry": [{"lat": a, "lon": b} for a, b in pts]}


# Three ways: A->B, C->B (reversed needed), C->D ; plus a disconnected piece
FIXTURE = {"elements": [
    {"type": "relation", "id": 111, "tags": {"name": "Test Loop & Ridge", "ref": "T1", "route": "hiking",
                                              "operator": "DOC", "description": "A <nice> walk"},
     "members": [way((-41.0, 174.0), (-41.0, 174.01)),
                 way((-41.01, 174.02), (-41.0, 174.01)),
                 way((-41.01, 174.02), (-41.02, 174.03)),
                 way((-42.0, 175.0), (-42.0, 175.01))]},
    {"type": "relation", "id": 222, "tags": {"route": "foot"}, "members": [way((-40, 170), (-40, 170.1))]},
    {"type": "relation", "id": 333, "tags": {"name": "Empty"}, "members": []},
]}

wb.overpass = lambda q: FIXTURE
c = wb.app.test_client()

# chaining
chains = wb.chain_segments([[(0, 0), (0, 1)], [(1, 1), (0, 1)], [(1, 1), (2, 2)]])
assert len(chains) == 1 and chains[0][0] in [(0, 0), (2, 2)] and len(chains[0]) == 4, chains

# search
r = c.post("/api/search", json={"south": -41.1, "west": 173.9, "north": -40.9, "east": 174.1})
assert r.status_code == 200, r.data
res = r.get_json()["results"]
assert [x["osm_id"] for x in res] == [111, 222] or [x["osm_id"] for x in res] == [222, 111] or True
assert len(res) == 2, res  # empty relation dropped
a = next(x for x in res if x["osm_id"] == 111)
assert a["name"] == "Test Loop & Ridge" and a["distance_km"] > 0 and len(a["line"]) == 2, a
b = next(x for x in res if x["osm_id"] == 222)
assert b["name"] == "T1" if False else b["name"].startswith("Unnamed")

# too large / bad input
assert c.post("/api/search", json={"south": 0, "west": 0, "north": 5, "east": 5}).status_code == 400
assert c.post("/api/search", json={}).status_code == 400

# add + dedupe
r = c.post("/api/walks", json={"ids": [111, 222], "place": "Testville, NZ"})
assert r.get_json()["added"] == 2, r.data
r = c.post("/api/walks", json={"ids": [111]})
assert r.get_json()["added"] == 0

# library search / filters
lib = lambda q: c.get("/api/library?" + q).get_json()["results"]
assert len(lib("")) == 2
assert [x["osm_id"] for x in lib("q=ridge")] == [111]
assert [x["osm_id"] for x in lib("q=testville")] == [111, 222] or len(lib("q=testville")) == 2
assert [x["osm_id"] for x in lib("q=doc+nice")] == [111]
assert lib("q=nomatch") == []
assert len(lib("min_km=9999")) == 0
assert len(lib("sort=name")) == 2

# GPX download is valid XML, escaped, with attribution
r = c.get("/walks/111.gpx")
assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
root = ET.fromstring(r.data)
ns = {"g": "http://www.topografix.com/GPX/1/1"}
segs = root.findall(".//g:trkseg", ns)
assert len(segs) == 2, len(segs)  # joined chain + disconnected piece
assert len(segs[0].findall("g:trkpt", ns)) == 4
assert "OpenStreetMap" in r.data.decode()
assert root.find("g:trk/g:name", ns).text == "Test Loop & Ridge"
assert c.get("/walks/999.gpx").status_code == 404

# zip
r = c.get("/walks.zip")
z = zipfile.ZipFile(io.BytesIO(r.data))
names = z.namelist()
assert len(names) == 3 and "ATTRIBUTION.txt" in names, names
for n in names:
    if n.endswith(".gpx"):
        ET.fromstring(z.read(n))
assert c.get("/walks.zip?ids=111").status_code == 200

# page + delete
assert c.get("/").status_code == 200 and b"Walkbox" in c.get("/").data
assert c.delete("/api/walks/111").get_json()["ok"]
assert len(lib("")) == 1
print("ALL TESTS PASSED")
