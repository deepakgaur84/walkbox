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

## Google Maps reviews and photos (optional)

In **My walk box** each walk gets a **Google reviews & photos** button when a Google API key is set. It looks up the matching
Google Maps place (the name must closely match the walk's name) and shows the rating, up to 5 reviews and up to 8 photos.
Everything is fetched live when you click and is never saved, because Google's terms do not allow storing it.
The match can be a different place from the walk (for example the beach itself), so the Google place name is always shown.

Set up (once):
1. Google Cloud console > create a project and link a billing account (Places API is billed; set a budget alert).
2. APIs & Services > Library > enable **Places API (New)**.
3. APIs & Services > Credentials > Create credentials > API key. Under "API restrictions" restrict it to **Places API (New)**.
4. APIs & Services > Places API (New) > Quotas: set a daily request cap.
5. In Dockhand, add an environment variable `GOOGLE_PLACES_API_KEY` to the stack and redeploy.

Walkbox also limits itself to `GOOGLE_CALLS_PER_MINUTE` (20) lookups and `GOOGLE_PHOTOS_PER_MINUTE` (80) photos. The key stays on the server and is never sent to the browser.
Walkbox has no login, so anyone who can open the page can trigger lookups on your key: keep authentication on the proxy rule.

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

## Settings (environment variables)

| Name | Default | Meaning |
|---|---|---|
| DB_PATH | /data/walkbox.db | Where the database is stored. |
| MAX_SPAN_DEG | 0.5 | Largest map area searched (degrees, about 55 km). |
| MAX_RESULTS | 150 | Most routes returned per search. |
| GOOGLE_PLACES_API_KEY | (blank) | Enables the Google reviews and photos button. |
| MIN_TRACK_KM | 0.3 | Shortest NZ council track (grouped by name) listed as a walk. |
| MAX_IMPORT_MB | 15 | Largest GPX file accepted by Import GPX. |
| MIN_PATH_KM | 0.8 | Shortest named path (grouped by name) listed as a walk. |
| PATH_HIGHWAYS | footway\|path\|track\|pedestrian | OSM path types used for "Named paths". |
| ROUTE_TYPES | hiking\|foot\|walking | OSM route types to include. |
| OVERPASS_URLS | overpass-api.de, overpass.kumi.systems | Comma-separated Overpass servers, tried in order. |

## Backup

Copy `data/walkbox.db`.

## Development

```
pip install -r requirements.txt
python tests/test_walkbox.py     # offline tests, no network needed
DB_PATH=./data/walkbox.db flask --app app run
```
