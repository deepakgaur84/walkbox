"""Read walking routes, named footpaths and nearby places straight out of an OpenStreetMap .osm.pbf file.

No Overpass and no public server is involved: the file Walkbox already downloads is read in three light passes
and only the walking data is kept in memory.

    pass 1  relations   hiking / foot / walking route relations   -> their tags and the ids of their ways
    pass 2  ways        route ways and named footpaths            -> their tags and the ids of their nodes
    pass 3  nodes       the nodes those ways use, found in one sweep (the file is sorted by id), and on the same sweep the
                        places a walker looks for: cafes, takeaways, toilets, drinking water, shelters, benches, viewpoints

It runs as its own process (python osm_scan.py ...), so reading a big file can never slow the website down or take it
with it if the NAS runs short of memory. Progress is printed as "T <phase> <count>" lines; the answer is a pickle file
holding plain dicts and arrays (no classes, so it can be read back by any program).

Standard library plus pyosmium only.
"""
import heapq
import os
import pickle
import sys
from array import array
from bisect import bisect_left

import osmium

NAN = float("nan")

# places a walker looks for (the same list app.py sorts into food / water / toilets / rest / view)
POI_AMENITY = {"cafe", "restaurant", "fast_food", "food_court", "ice_cream", "pub", "bar", "drinking_water", "toilets", "shelter", "bench"}
POI_SHOP = {"bakery", "convenience", "supermarket"}
POI_KEEP = ("amenity", "shop", "leisure", "tourism", "name", "opening_hours", "website", "contact:website")


def is_poi(tags):
    return (tags.get("amenity") in POI_AMENITY or tags.get("shop") in POI_SHOP or tags.get("leisure") == "picnic_table"
            or tags.get("tourism") in ("picnic_site", "viewpoint"))


def poi_tags(tags):
    return {k: tags.get(k) for k in POI_KEEP if tags.get(k)}


class _Routes(osmium.SimpleHandler):
    def __init__(self, out, route_types, tick):
        super().__init__()
        self.out, self.types, self.tick, self.seen = out, set(route_types), tick, 0

    def relation(self, r):
        self.seen += 1
        if not self.seen % 5000:
            self.tick("relations", self.seen)
        tags = r.tags
        if tags.get("type") != "route" or tags.get("route") not in self.types:
            return
        ways = [m.ref for m in r.members if m.type == "w"]
        if ways:
            self.out[r.id] = {"tags": {t.k: t.v for t in tags}, "ways": ways}


class _Ways(osmium.SimpleHandler):
    def __init__(self, out, wanted, path_highways, tick, poi_ways):
        super().__init__()
        self.out, self.wanted, self.hw, self.tick, self.seen = out, wanted, set(path_highways), tick, 0
        self.poi_ways = poi_ways                                   # way id -> {"tags", "ref": its first node}

    def way(self, w):
        self.seen += 1
        if not self.seen % 20000:
            self.tick("ways", self.seen)
        tags = w.tags
        if is_poi(tags) and len(w.nodes):                          # a cafe drawn as a building: placed by its first corner
            self.poi_ways[w.id] = {"tags": poi_tags(tags), "ref": w.nodes[0].ref}
        is_path = bool(tags.get("highway") in self.hw and tags.get("name")
                       and tags.get("access") not in ("private", "no") and tags.get("foot") != "no")
        if not is_path and w.id not in self.wanted:
            return
        refs = array("q", [n.ref for n in w.nodes])
        if len(refs) >= 2:
            self.out[w.id] = {"tags": {t.k: t.v for t in tags} if is_path else None, "refs": refs}


class _Nodes(osmium.SimpleHandler):
    """Nodes arrive sorted by id, and so do the wanted ids: walk both lists together, no lookups, no big index."""

    def __init__(self, ids, lats, lons, tick, pois):
        super().__init__()
        self.ids, self.lats, self.lons, self.tick, self.pois = ids, lats, lons, tick, pois
        self.i, self.count, self.seen = 0, len(ids), 0

    def node(self, n):
        self.seen += 1
        if not self.seen % 2000000:
            self.tick("nodes", self.seen)
        nid, ids, i = n.id, self.ids, self.i
        if len(n.tags) and n.location.valid():
            tg = n.tags
            if is_poi(tg):
                self.pois.append((round(n.location.lat, 5), round(n.location.lon, 5), poi_tags(tg)))
        while i < self.count and ids[i] < nid:               # a wanted node outside the extract: leave it blank
            i += 1
        if i < self.count and ids[i] == nid:
            loc = n.location
            if loc.valid():
                self.lats[i], self.lons[i] = loc.lat, loc.lon
            i += 1
        self.i = i


def _unique_sorted(ways, extra_refs=(), chunk=500000):
    """Every node id the kept ways use, once each and in order, without ever holding millions of Python integers at once."""
    allrefs = array("q")
    for w in ways.values():
        allrefs.extend(w["refs"])
    allrefs.extend(extra_refs)
    runs = [array("q", sorted(allrefs[i:i + chunk])) for i in range(0, len(allrefs), chunk)]
    del allrefs
    ids, last = array("q"), None
    for ref in heapq.merge(*runs):
        if ref != last:
            ids.append(ref)
            last = ref
    return ids


def scan_extract(path, route_types, path_highways, tick=None):
    """Return {"routes": {relation id: {"tags", "ways"}}, "ways": {way id: {"tags" or None, "pts": array of lat, lon, ...}},
    "pois": [(lat, lon, tags), ...]}."""
    tick = tick or (lambda phase, count: None)
    tick("start", 0)
    routes = {}
    _Routes(routes, route_types, tick).apply_file(path)                # only relations are decoded
    wanted = {wid for r in routes.values() for wid in r["ways"]}
    tick("routes", len(routes))

    ways, poi_ways = {}, {}
    _Ways(ways, wanted, path_highways, tick, poi_ways).apply_file(path)    # only ways are decoded
    tick("waysdone", len(ways))

    ids = _unique_sorted(ways, [pw["ref"] for pw in poi_ways.values()])
    pois = []
    lats, lons = array("d", [NAN]) * len(ids), array("d", [NAN]) * len(ids)
    _Nodes(ids, lats, lons, tick, pois).apply_file(path)               # only nodes are decoded; the whole node block is read
    tick("nodesdone", len(ids))

    for pw in poi_ways.values():                                       # places drawn as areas sit at their first corner
        k = bisect_left(ids, pw["ref"])
        if k < len(ids) and ids[k] == pw["ref"] and lats[k] == lats[k]:
            pois.append((round(lats[k], 5), round(lons[k], 5), pw["tags"]))
    tick("poisdone", len(pois))

    for w in ways.values():
        pts = array("d")
        for ref in w.pop("refs"):
            k = bisect_left(ids, ref)
            if k < len(ids) and ids[k] == ref and lats[k] == lats[k]:   # NaN means the node was not in the file
                pts.append(round(lats[k], 5))
                pts.append(round(lons[k], 5))
        w["pts"] = pts
    ways = {wid: w for wid, w in ways.items() if len(w["pts"]) >= 4}   # a line needs two known points
    tick("done", len(ways))
    return {"routes": routes, "ways": ways, "pois": pois}


def main(argv):
    path, out, route_types, path_highways = argv[1], argv[2], argv[3].split("|"), argv[4].split("|")
    try:
        os.nice(10)                                                    # be gentle with the NAS
    except (OSError, AttributeError):
        pass

    def tick(phase, count):
        print("T %s %d" % (phase, count), flush=True)

    result = scan_extract(path, route_types, path_highways, tick)
    tmp = out + ".part"
    with open(tmp, "wb") as fh:
        pickle.dump(result, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, out)
    print("OK", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as ex:                                            # the caller shows this line to the person
        print("ERROR %s: %s" % (type(ex).__name__, ex), flush=True)
        sys.exit(1)
