# Forklift Spec Database

A local web app to store, search, and look up forklift specifications — with AI
features for pulling spec sheets off the web and querying the database in plain
English.

## What it does

1. **Look up by code** — type a make/model (e.g. `Toyota 8FGCU25`). The app checks
   the database first; if it's missing, it searches the web (Tavily), extracts the
   specs (Claude), and shows them for review. **Nothing is saved until you confirm.**
2. **Spec sheet on request** — every record keeps a link to the source PDF. Open or
   download it live, or click **Save a permanent copy** to archive it locally.
   (By default only the *link* is stored, so the database stays tiny.)
3. **Ask in plain English** — e.g. *"which electric forklifts lift over 5 meters?"*.
   Claude converts the question to a read-only SQL query, runs it, and answers.

## Quick start (Windows)

```powershell
cd forklift-db
./run.ps1
```

That creates a virtual environment, installs dependencies, seeds sample data, and
starts the server at <http://127.0.0.1:8000>.

### Manual start (any OS)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then add your keys
python seed.py
python -m uvicorn app.main:app --reload
```

## API keys

The app runs as a plain database with **no keys**. To turn on the AI features, copy
`.env.example` to `.env` and fill in:

- `ANTHROPIC_API_KEY` — spec extraction + plain-English questions. <https://console.anthropic.com>
- `TAVILY_API_KEY` — web search for spec sheets. <https://tavily.com>

Keys stay server-side; they are never sent to the browser.

## Safety of the plain-English query feature

The AI only ever *proposes* SQL. Before anything runs, a validator
(`app/ai/nl_query.py`) enforces that the statement is a single read-only `SELECT`
against the `forklifts` table, blocks write/DDL keywords and comments, and appends a
row limit. Queries also execute on a connection set to read-only mode. So a bad
question cannot modify or delete data.

## Moving to a server later

- Swap SQLite for Postgres by changing `DATABASE_URL` in `.env` — no code changes.
- The app is a standard ASGI app (`app.main:app`); run it behind
  `uvicorn`/`gunicorn` + a reverse proxy, or containerize it.
- Add authentication before exposing it publicly (not included in this local build).

## Project layout

```
app/
  main.py            FastAPI routes + UI
  config.py          settings from .env
  database.py        engine / session
  models.py          Forklift table
  schemas.py         Pydantic models
  ai/
    tavily_client.py web search + extract
    extractor.py     Claude: text -> structured specs
    lookup.py        orchestration: DB -> web -> review
    nl_query.py      plain English -> safe SQL -> answer
  templates/         Jinja + HTMX views
  static/style.css
seed.py              sample data
run.ps1              one-shot setup + run
```
