# OilTrace AI - runs on any container host (Render, Railway, Fly.io, plain Docker)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OILTRACE_DATA_DIR=/data

WORKDIR /app

# rasterio's manylinux wheel bundles GDAL, PROJ, GEOS and friends, but it still
# links against the system libexpat, which python:*-slim does not ship. Without
# it `import rasterio` dies with "libexpat.so.1: cannot open shared object file"
# and every Sentinel-1 fetch fails at the pixel-read step.
# opencv-python-headless needs no libGL, which is why the headless build is used.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libexpat1 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY tests/ ./tests/
COPY tools/ ./tools/

# Mount a persistent volume here, or the database resets on every redeploy.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Hosts inject $PORT; fall back to 8000 for plain `docker run`.
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
