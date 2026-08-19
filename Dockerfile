FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# SQLite data + archived PDFs live here (mounted as a volume in compose)
RUN mkdir -p data data/pdfs
RUN chmod +x entrypoint.sh

EXPOSE 8000

# entrypoint.sh seeds the DB on first boot, then launches gunicorn on $PORT
# (Render/Cloud Run set it; defaults to 8000). --forwarded-allow-ips lets the
# app trust X-Forwarded-Proto from the platform proxy so it builds https URLs.
ENTRYPOINT ["./entrypoint.sh"]
