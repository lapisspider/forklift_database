#!/bin/sh
# On first boot (empty volume), seed the database from the baked-in snapshot so
# the deployed app has the full catalog. Existing data is never overwritten.
set -e
mkdir -p data data/pdfs
if [ ! -f data/forklifts.db ] && [ -f deploy/seed.db ]; then
  cp deploy/seed.db data/forklifts.db
  echo "Seeded database from deploy/seed.db"
fi

# Bind to the platform-provided port (Render/Cloud Run set $PORT); default 8000.
exec gunicorn app.main:app \
  -k uvicorn.workers.UvicornWorker \
  -b "0.0.0.0:${PORT:-8000}" \
  -w 2 \
  --forwarded-allow-ips '*' \
  --access-logfile -
