FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# osmium (reads the OpenStreetMap file) is installed from a ready-made wheel; the build stops at once if none exists for this CPU
RUN pip install --no-cache-dir --only-binary=osmium -r requirements.txt

COPY app.py osm_scan.py ./
COPY templates templates

ENV DB_PATH=/data/walkbox.db
VOLUME /data
EXPOSE 8080

CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "--timeout", "150", "app:app"]
