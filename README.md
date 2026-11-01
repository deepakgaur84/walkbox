# Walkbox

Find walking routes anywhere in the world (OpenStreetMap), add them to your own
searchable box, and download each one as a GPX file.

- **Find walks**: type a place or pan the map, press "Find walks in this map view", tick walks, press "Add selected to my box".
- **My walk box**: search by name, ref, operator or place, filter by distance, download one walk as GPX or everything as a .zip.

Route data © OpenStreetMap contributors (ODbL). Downloaded GPX files carry this notice.

## Deploy on the Synology (Dockhand)

GitHub Actions builds the image on every push to `main` and publishes it to `ghcr.io/deepakgaur84/walkbox`.
The NAS only pulls that image, so no code folder and no `git` on the NAS are needed.

1. One time, GitHub: after the first build finishes (repo > Actions tab shows a green tick), open your profile > Packages > `walkbox` >
   Package settings > Change visibility > Public. This lets the NAS pull it without a login.
2. File Station: create the folder `docker/walkbox/data` (the database lives here), or extract the deploy zip that already contains it.
3. Dockhand: create a stack named `walkbox`, paste in `docker-compose.yml` from this repo, and deploy.
4. Open `http://<NAS-IP>:8095`. Optionally add a proxy host in Nginx Proxy Manager pointing to `<NAS-IP>` port `8095`.
   There is no login, so keep it on your LAN or put NPM access control in front.

To update after code changes: push to `main`, wait for the green tick on the Actions tab, then redeploy the stack in Dockhand with "pull latest image".

## Settings (environment variables)

| Name | Default | Meaning |
|---|---|---|
| DB_PATH | /data/walkbox.db | Where the database is stored. |
| MAX_SPAN_DEG | 0.5 | Largest map area searched (degrees, about 55 km). |
| MAX_RESULTS | 150 | Most routes returned per search. |
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
