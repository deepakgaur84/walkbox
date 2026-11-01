# Walkbox

Find walking routes anywhere in the world (OpenStreetMap), add them to your own
searchable box, and download each one as a GPX file.

- **Find walks**: type a place or pan the map, press "Find walks in this map view", tick walks, press "Add selected to my box".
- **My walk box**: search by name, ref, operator or place, filter by distance, download one walk as GPX or everything as a .zip.

## Data sources

Pick any combination when you search:

- **DOC tracks (New Zealand)**: official Department of Conservation tracks with description, difficulty, completion time and a link to the DOC page. CC BY 4.0.
- **Official routes**: named walking route relations in OpenStreetMap. ODbL.
- **Named paths**: named footpaths in OpenStreetMap, grouped by name into one walk (paths shorter than `MIN_PATH_KM` are ignored). ODbL.

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
