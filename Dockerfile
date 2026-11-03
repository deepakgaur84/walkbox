FROM python:3.12-slim

WORKDIR /app

# osmium needs the expat library, which the slim Python image does not include
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# osmium (reads the OpenStreetMap file) is installed from a ready-made wheel; the build stops at once if none exists for this CPU
RUN pip install --no-cache-dir --only-binary=osmium -r requirements.txt
# fail the build here, not on the NAS, if the OSM reader cannot start
RUN python -c "import osmium"

COPY app.py osm_scan.py ./
COPY templates templates

ENV DB_PATH=/data/walkbox.db
VOLUME /data
EXPOSE 8080

CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "--timeout", "150", "app:app"]
