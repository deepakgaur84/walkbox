# Walkbox

Find walking routes anywhere in the world (OpenStreetMap), add them to your own
searchable box, and download each one as a GPX file.

- **Find walks**: type a place or pan the map, press "Find walks in this map view", tick walks, press "Add selected to my box".
- **My walk box**: search by name, ref, operator or place, filter by distance, download one walk as GPX or everything as a .zip.

## Data sources

Pick any combination when you search:

- **DOC tracks (New Zealand)**: official Department of Conservation tracks with description, difficulty, completion time and a link to the DOC page. CC BY 4.0.
- **NZ council tracks**: the national tracks dataset from Herenga ā Nuku Outdoor Access Commission, compiled from councils (including Auckland Council), LINZ and DOC. Segments are grouped by name into one walk each, with a walking grade (path, short walk, walking track, tramping track…), dog and mobility notes, and a link where the council supplies one. The service does not state a licence, so check the managing organisation before reusing the data. Where DOC lists the same track, the DOC entry is kept.
- **Official routes**: named walking route relations in OpenStreetMap. ODbL.
- **Named paths**: named footpaths in OpenStreetMap, grouped by name into one walk (paths shorter than `MIN_PATH_KM` are ignored). ODbL.

## How searching works (fast list, route on click)

"Find walks in this map view" fetches only a **light list** from the chosen sources, all at the same time: name, source, difficulty and length where the
source gives one. No route outlines are downloaded at this stage, so it is quick. Lengths of OpenStreetMap named paths are rough (shown with a ~) and
official OpenStreetMap routes show no length until opened.

**Nothing is fetched twice.** Everything slow is remembered in your database (the `kv` table in `walkbox.db`), so it survives restarts and redeploys: finished searches for
6 hours (`SEARCH_CACHE_SECONDS`), and each walk's route for 7 days, nearby places for 24 hours, surfaces and difficulty for 7 days and elevation for 30 days.
Opening a walk you have opened before, searching an area again, or clicking away and back costs nothing. If an OpenStreetMap server has not answered within
6 seconds (`OVERPASS_HEDGE_SECONDS`) the next one is asked as well and whichever answers first is used. The cache can be cleared by deleting the `kv` rows
or by setting `SEARCH_CACHE_SECONDS=0` to turn off the search part.

**Clicking away does not lose your search.** The Find list and map view are kept when you switch to My walk box and back, and they are saved in the browser
for 6 hours, so reloading the page, or your phone discarding the tab, brings the same list back without searching ("Showing your last search…"). Searches are
aligned to a 5 km grid, and a search whose area is already covered by an earlier one (for example after the map zoomed onto a walk) is answered instantly, with
"(saved)" in the status line. Nothing searches automatically: only the **Find walks in this map view** button (and **Go**) start a search.

**Searches do not block the walk you have opened.** OpenStreetMap lookups run in two lanes: the big list searches use one, and everything about a walk you have opened
(its route, nearby places and surfaces) uses the other, so a slow search no longer makes the surface colours or places wait. The difficulty estimate (from distance,
climbing and steepness) appears at once and is refined, and the route is coloured, when the surface lookup finishes. An OpenStreetMap server that answers "query timed
out" is treated as a failure, tried again on the next server, and never remembered as "nothing found".

Each source is searched as its **own request**, and the page shows a source's walks the moment it answers: DOC and the council tracks usually come back
in a second or two, while OpenStreetMap (routes and named paths) is the slowest because it runs on shared public servers. You can open a walk
while the rest are still loading. The status line shows which sources are done and how long each took. Repeated searches of the same area are
remembered (see below). If OpenStreetMap is slow for you, untick "Official routes" or "Named paths", or list a faster
Overpass server first in `OVERPASS_URLS`.

**Searching for a place** (the box next to Go) also sets where walks should start: when you open a walk that ends at that place (within about 3 km), it is turned round so
that it starts there, and its card shows "⇄ starts at <place>". Press **⇄ Swap start / end** to put it back. Walks you add to your box from that view are saved
the way round you are looking at.

The route itself is fetched only when you **click a walk** (or when you add it to your box). After that the walk view opens, with its exact length.

## Route surface and estimated difficulty

The route in the walk view is coloured by **what it is made of**, with a legend above the elevation chart (for example Paved 41%, Sand 24%, Gravel, Rock,
Steps, Boardwalk, Dirt, Grass). Surfaces come from OpenStreetMap: Walkbox looks up the mapped paths along your route and gives each stretch the surface of the
nearest one (a footpath is preferred to a street beside it; an ordinary street with no surface tag counts as paved). Paths where nobody has recorded the surface
are shown **white (unknown)**, which is common on bush tracks, so the colours are only as good as the map data. Colours follow the walk if you swap start and end.

The **Weather & nearby** card also shows an **estimated difficulty** (Easy, Moderate, Hard or Very hard, with a score out of 10) and the reasons behind it,
and the walk's card in the list gets an "Est. …" tag. The score adds up distance (up to 3 points, 12 km or more), climbing (up to 3 points, 300 m or more),
steepness (up to 2.5 points for 30% or steeper stretches), rough surfaces such as rock, steps and sand (up to 2 points) and OpenStreetMap's hiking-grade
tag (up to 3 points). If the source gives an official grade (DOC or the council tracks), the walk is never rated easier than that grade.
Under 3.5 is Easy, under 6 Moderate, under 8 Hard. It is an estimate from map and elevation data, not a substitute for local knowledge: check conditions
and the official track information before you go. The editor pre-fills the difficulty from this estimate. Surface and difficulty are saved with walks in your box.

## Customising a walk

Open a walk and press **✎ Customise** (top right of the map). The route is shown in green with the original dashed in grey, and a panel on the left
has these tools; the distance, type, climb and time update as you go, and the elevation chart redraws:

- **Reshape**: click the map to add a **detour** point. Only about 150 m of the route either side of it is replaced, the rest is kept as it was. Click **on** the
  route itself and it is just split there. Drag any point to move it, and click a point to remove it.
- **Draw**: click to add points to the end of the route. **Start over** clears the route first, so you can draw a walk from scratch.
- **Trim**: drag the two orange handles along the route to cut off the start or the end, then press **Apply trim**.
- **Follow footpaths between points** (on by default in New Zealand) works out the path between points with the routing engine; with it off, or if
  routing is unavailable, points are joined with straight lines.
- **↶ Undo / ↷ Redo** for every change, and fields for the **name, difficulty, description, your rating and notes**.

**Save to my box…** asks you at that moment whether to **save a new walk** (your version is saved beside the original, marked "Custom") or **replace the original**
(only for walks already in your box). Customised walks keep the original source's credit in their GPX. If a walk has several separate pieces,
only the main track is kept.

## Weather and nearby places

When a walk is open, a **Weather & nearby** card appears on the map:

- **Weather**: current conditions at the middle of the walk and a three-day outlook (temperature, rain chance, wind, UV, sunset), from the free
  [Open-Meteo](https://open-meteo.com/) service. Forecasts are cached for 10 minutes.
- **Along the route**: cafés, restaurants and shops (☕), drinking water (💧), toilets (🚻), rest spots such as benches, picnic tables and shelters (🪑)
  and viewpoints (👀) near the route, from OpenStreetMap. Tap the chips to show or hide each kind; tap a place in the list or on the map for details such as
  opening hours. Food places within about 250 m of the route are included, other places within 120 m, and benches within 60 m. OpenStreetMap
  can be out of date, so a place may have closed.

## Walk view: route, live GPS and elevation profile

Click any walk in the list and the map switches to a navigation-style view:

- the route is drawn as a thick green line with direction arrows, a start marker and an end flag;
- a **live blue dot** follows your GPS position (while you are on the route it sits on the green line, with a faint ring showing your true GPS position and its accuracy; if you wander off it shows where you really are), with a bar at the top saying whether you are on the route, how many km you have done and how many are left
  (or how far you are from the line when you are off it). Your browser asks for location permission the first time, and the page needs the https address;
- an **elevation profile** runs along the bottom with a blue dot at your position along the route. Touch or hover the chart to see the distance and height at
  that point, and the same spot is shown on the map. Heights come from the free Open-Meteo elevation service (approximate) and are saved after the first lookup;
- the **⇄ Swap start / end** button (top right of the map) walks the same route the other way round: the start and end markers and arrows swap, the elevation
  profile, "km done / to go" and the places list are measured from the new start, and the Navigate links, turn-by-turn "along this walk" and the
  **Download GPX** button all use the new direction (a downloaded GPX is named "(reversed)"). It is a view setting, so the walk is stored unchanged;
  press the button again to restore it;
- the × button closes the view and stops the GPS.

The page must stay open with the screen on for the live dot to update; browsers pause web pages in the background.

## Navigating to and along a walk

Every walk card has a **Navigate** button:

- **Directions to the start** opens Google Maps (or Apple Maps) with the start of the walk as the destination. Free, no key needed.
- **Walk the route (Google Maps)** passes the start, end and up to 8 points along the walk to Google Maps in walking mode. Google
  finds its own paths between those points, so this is an approximation, not the exact track.
- **Show start / Walking route in Organic Maps** open the free offline Organic Maps app on your phone (it must be installed) using its `om://` links. Its route is its own pedestrian route from the start to the end of the walk.
- **Open in any map app** uses a `geo:` link for apps such as OsmAnd (Android).
- **Turn-by-turn to the start** and **Turn-by-turn along this walk** (inside Walkbox, no phone app needed) use a self-hosted
  [Valhalla](https://github.com/valhalla/valhalla) routing engine that runs as the `walkbox-valhalla` container in the same stack.
  It covers **New Zealand only** (change `tile_urls` to another [Geofabrik](https://download.geofabrik.de/) extract to cover another region).
  The first start downloads the NZ map and builds routing tiles, which takes a while and needs a few GB of free disk; watch the
  container log. Until it finishes, the buttons show a "not reachable yet" message. Directions are shown as a list of steps and a blue
  line on the map; they are not spoken, and there is no automatic rerouting.
  "Along this walk" snaps the walk's track to the path network, so it can fail on short or unmapped tracks.

For turn-by-turn use on the trail, download the GPX and open it in an offline app (OsmAnd, Organic Maps or similar).

## Photos and your own notes

In **My walk box** each walk has a **Photos & my notes** button.

- **Photos** come from Wikimedia Commons: openly licensed, geotagged photos taken within about 800 m of the start, middle and end of the walk.
  They are free and need no account or key. They are photos taken nearby, so they may not show the exact track. Each shows the photographer and licence and links to the original page.
  Photos are shown live from Wikimedia and are not saved.
- **My rating and notes**: give the walk 1 to 5 stars and write notes (conditions, parking, who you went with). They are saved in your box, shown on the walk's card, and your notes are included when you search the box.

There is no free public source of trail reviews, so Walkbox does not show other people's reviews.

## Importing your own GPX files

In **My walk box** press **Import GPX** and choose one or more `.gpx` files (for example trails you have downloaded yourself).
They join your searchable box like any other walk and can be downloaded again. Files are limited to 15 MB each. Check the original source's terms before sharing them.

Each walk shows distance, loop or point-to-point, an estimated time (5 km/h plus 10 min per 100 m climbed), difficulty where the source has it,
and elevation gain (looked up from the free Open-Meteo elevation API when you add the walk). Downloaded GPX files carry the right attribution for their source.

## Deploy on the Synology (Dockhand)

GitHub Actions builds the image on every push to `main` and publishes it to `ghcr.io/deepakgaur84/walkbox`.
The NAS only pulls that image, so no code folder and no `git` on the NAS are needed.

1. One time, GitHub: after the first build finishes (repo > Actions tab shows a green tick), open your profile > Packages > `walkbox` >
   Package settings > Change visibility > Public. This lets the NAS pull it without a login.
2. File Station: create the folder `docker/walkbox/data` (the database lives here), or extract the deploy zip that already contains it.
3. Dockhand: create a stack named `walkbox`, paste in `docker-compose.yml` from this repo, and deploy.
4. In Zoraxy, add a proxy rule for your hostname with target `walkbox:8080` (container name and internal port; TLS off).
   Walkbox joins the existing `proxynet` Docker network and publishes no host port.
   There is no login, so enable authentication/access control on the proxy rule.

To update after code changes: push to `main`, wait for the green tick on the Actions tab, then redeploy the stack in Dockhand with "pull latest image".

## Look and feel

Walkbox opens as a floating panel over a full-screen map. **Explore** lists walks as cards (a nearby open-licence photo from Wikimedia
Commons when there is one, otherwise a coloured tile) with a ♡ to save to your box. Every result has a dot on the map; at lower zoom
nearby dots merge into "N trails" and tapping one zooms in. The chips above the map (Difficulty, Length, Elevation, Features, Sources)
narrow the list already on screen; **Sources** picks what the next search fetches. Tapping a walk opens its page in the panel: photos,
length, climb, estimated time, facts, description, elevation, weather and nearby places, with **Download GPX** pinned at the bottom.
The **Map layers** box switches between streets (CARTO), satellite (Esri) and topographic (OpenTopoMap).

## At a glance

Opening a walk shows a panel of the facts you check before leaving: distance, time (DOC's own time, or a range estimated from distance and
climb), difficulty, wheelchair access, dog rules, and a red banner if the track is temporarily closed. Council tracks show their
conditions of use (for example seasonal closures). DOC walks get a red **View current alerts** button that opens the DOC page for that walk,
where its live alerts are published. Dog and wheelchair details come from the source data (DOC and the national tracks dataset, or the
`dog` and `wheelchair` tags on OpenStreetMap); when the source says nothing, the panel says "unknown" instead of guessing. The panel also
has **Add to my box**, **Share track** (the share sheet on a phone, or copy to the clipboard) and **Mark as complete** (walks you have
done show a "✓ Completed" tag in your box).

## Search by name or by map

Type in the box at the top of **Find**. With text in it the big button becomes **Search all NZ for “…”**: it lists every walk whose name
holds all the words you typed (any order), anywhere in New Zealand, not just the map view; press Enter for the same. **Go to place** moves
the map to a place instead. With the box empty the button searches the map view, as before. DOC and council walks are searched by name
straight away; OpenStreetMap walks can be searched by name once their download on the Data tab has finished.

## One-off NZ downloads (the Data tab)

The **Data** tab downloads New Zealand data once, with a progress bar for each item. All six boxes start ticked.

| Item | Size (estimate) | Who does it |
|---|---|---|
| DOC walking experiences | ~5 MB | Walkbox, into its database |
| National tracks (council and other) | ~60 MB | Walkbox, into its database |
| Walkbox's own store: OpenStreetMap walks | ~20–60 MB | Walkbox, square by square from the Overpass servers (nearest Auckland first) |
| Raw OSM extract | ~400 MB | Walkbox, saved as `data/osm/new-zealand-latest.osm.pbf`; a stopped download carries on from where it stopped |
| Valhalla routing tiles | ~1–2 GB | The `walkbox-valhalla` container builds them; Walkbox shows its progress |
| Self-hosted Overpass database | ~3–6 GB | The `walkbox-overpass` container imports it; Walkbox shows its progress |

Once the stored walks are in, **Find** answers from Walkbox's own database (no waiting on the public servers) and opening a DOC or
council track needs no lookup. OpenStreetMap squares are used as soon as each one is done; areas not reached yet still use the live
servers. When your own Overpass server is ready, Walkbox asks it first and falls back to the public ones. Downloads run in the
background, survive a restart and can be cancelled. The public Overpass servers sometimes refuse big areas (HTTP 504): Walkbox then retries, cuts that square into smaller ones, and skips any square that stays refused instead of stopping; press Download again later to fetch only the missed squares. The first start queues everything for all of NZ (set `SEED_AUTOSTART` to `0` to
start them yourself). Create the `/volume1/docker/walkbox/overpass` folder before deploying.

## Settings (environment variables)

| Name | Default | Meaning |
|---|---|---|
| DB_PATH | /data/walkbox.db | Where the database is stored. |
| MAX_SPAN_DEG | 0.5 | Largest map area searched (degrees, about 55 km). |
| MAX_RESULTS | 150 | Most routes returned per search. |
| PHOTOS_PER_MINUTE | 30 | Most photo lookups per minute (keeps Wikimedia requests polite). |
| VALHALLA_URL | (blank) | Address of the Valhalla routing engine; blank hides the turn-by-turn buttons. |
| WEATHER_URL | Open-Meteo forecast API | Weather service used by the weather card. |
| SEARCH_CACHE_SECONDS | 21600 | How long a finished search of the same area is remembered. |
| OVERPASS_HEDGE_SECONDS | 6 | Ask the next OpenStreetMap server if the first has not answered in this time. |
| OVERPASS_TIMEOUT | 30 | Give up on one OpenStreetMap request after this many seconds. |
| MIN_TRACK_KM | 0.3 | Shortest NZ council track (grouped by name) listed as a walk. |
| MAX_IMPORT_MB | 15 | Largest GPX file accepted by Import GPX. |
| MIN_PATH_KM | 0.8 | Shortest named path (grouped by name) listed as a walk. |
| PATH_HIGHWAYS | footway\|path\|track\|pedestrian | OSM path types used for "Named paths". |
| ROUTE_TYPES | hiking\|foot\|walking | OSM route types to include. |
| OVERPASS_URLS | overpass-api.de, overpass.kumi.systems | Comma-separated Overpass servers, tried in order. |
| OVERPASS_LOCAL_URL | (blank) | Your own Overpass server; asked first once it answers. |
| SEED_AUTOSTART | 1 | Queue the NZ downloads on the very first start. |
| OSM_EXTRACT_URL | Geofabrik NZ .osm.pbf | File fetched by "Raw OSM extract". |
| SEED_CELL_DEG | 0.5 | Size of each square when storing OpenStreetMap walks. |
| SEED_TIMEOUT | 180 | Seconds one big download request may take. |
| SEED_PAUSE_SECONDS | 1.5 | Pause between big requests to the public servers. |
| SEED_ATTEMPTS | 2 | Tries for one square before it is cut into four smaller ones. |
| SEED_MAX_SPLIT | 4 | How many times a refused square may be halved (0.5° down to about 3 km). |
| SEED_GIVE_UP_AFTER | 12 | Refusals in a row before the OpenStreetMap download stops and reports the servers as not answering. |

## Backup

Copy `data/walkbox.db`.

## Development

```
pip install -r requirements.txt
python tests/test_walkbox.py     # offline tests, no network needed
DB_PATH=./data/walkbox.db flask --app app run
```
