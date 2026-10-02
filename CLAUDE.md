# Working notes for Claude

Context for this repo that isn't derivable from the code. Read `README.md` for what
the app does and `DEPLOY.md` for hosting; this file is conventions and standing rules.

## Stack and the one migration mechanism

FastAPI + SQLAlchemy 2.0 + SQLite + Jinja2 + HTMX. No Alembic.
`_ensure_schema()` in `app/database.py` is the **only** migration path — it adds
columns and views to an existing DB file idempotently. Any new column must be
added there as well as on the model, or existing databases break on boot.

## Deploy

Render, from GitHub (`lapisspider/forklift_database`). Storage is ephemeral:
`entrypoint.sh` copies `deploy/seed.db` into `data/forklifts.db` on first boot only.

**Refreshing the seed** (needed whenever catalog data changes should reach production):

```bash
cp data/forklifts.db deploy/seed.db
sqlite3 deploy/seed.db "DELETE FROM users; VACUUM;"
```

Dropping `users` matters — the seed is committed, and account rows must not be.

## Never commit

`.env`, `*.csv` reports, `data/*.db`. All three are in `.gitignore`; don't `-f` past it.
`deploy/seed.db` **is** committed, deliberately, and is the one exception.

## Gotchas that have cost real time

- **`asset_v` is computed once at import** (`app/main.py:33`), from the newest static
  file mtime. Editing `style.css` while the dev server runs serves the **old** file —
  restart the server, don't debug the CSS.
- **`app/static/style.css` holds hand-tuned values** (the panel gradient, the footer
  wave) that are easy to clobber. Edit it with line-number anchors, never by
  `str.index()` on a selector — `".tier {"` substring-matches inside `".meta-pill .tier {"`,
  which once deleted 711 lines between two selectors.
- **Global `button { text-transform: uppercase }` exists.** To undo it on a specific
  button, write `text-transform: none` — removing the local declaration does nothing.
- **`flex-basis: 100%` sets an item's main SIZE**, not just a line break. The gold tier
  star pyramid uses CSS grid (`grid-column: 1 / -1`) for that reason.
- PowerShell mangles multi-line `-m` heredocs. Use single-line commit messages, or the
  Bash tool.

## Data conventions

- **Capacity is stored in kilograms**, as the OEM publishes it. Metric-code OEMs state
  capacity in the model code (Linde H25D = 2500 kg, Heli CPCD25 = 2500 kg); the code wins
  over a pounds-conversion artifact. `reconcile_capacity()` in `app/ai/extractor.py`
  repairs these automatically, but only within 12% — see the open item below.
- **Series never contains the word "Series".** Store `G3`, not `G3 Series`.
  `clean_series()` in `app/schemas.py` enforces this on input.
- Warehouse gear with no letter series takes its **model-code prefix** as the series
  (`CBD`, `CDD`, `CQD`, `CQE`).
- **Model names use the full order code**, including the config suffix
  (`CPYD25-KU1H`, `CPCD25-K2`) — the suffix identifies the engine/ECU, which is what
  decides kit fit.
- `fuel_type` is a closed set: `Electric`, `Diesel`, `LPG`, `Gasoline`, `Gasoline/LPG`.

### OSHA class mapping

| Product | Class |
|---|---|
| Electric counterbalance (3- or 4-wheel, lead-acid or li-ion) | I |
| Narrow-aisle reach truck, order picker | II |
| Pallet truck, walkie/rider stacker | III |
| IC cushion tire | IV |
| IC pneumatic tire, incl. heavy 12–46 t | V |
| Tow tractor | VI |
| Rough terrain | VII |

**Electric counterbalance is Class I regardless of tire.** Class II is *only*
narrow-aisle, never counterbalance. This has been got wrong twice (BYD ECC, Heli).

### Heli model codes

`C` forklift · `P` counterbalanced · fuel letter · optional `D` · capacity in 100 kg.
Fuel letter: `C` diesel, `Q` gasoline, `Y` LPG, `QY` dual, none = electric.
A trailing `D` means torque converter on IC trucks; no `D` means mechanical clutch.

**Trap:** `CPD` means *diesel-clutch* on an IC family page but *electric* on an
electric family page. `CPD70-CU7G` is a diesel; `CPD25-GE2G` is a lithium electric.
Resolve `CPD` by the category page it came from, never by the string alone.

## Kits

Kits must **not** transfer between Hyster/Yale twins, even where the trucks are
mechanically identical. Don't link them.

## Lookup pipeline

Decoder registry in `app/ai/decoders/`, each exposing `match()` / `decode()` /
`reject_reason()` plus a confidence score. Thresholds in `app/ai/lookup.py`:
`WEB_CONFIDENCE_FLOOR = 0.60`, `SERIAL_SHAPE_FLOOR = 0.80`, `TIE_BAND = 0.15`.

`normalize()` strips separators, so Crown `FC 5200` and Doosan `FC-5200` collapse to the
same string. `_serial_shape_is_distinctive()` is what keeps a real Crown model from being
read as a Doosan serial — be careful changing it.

## Tests

```bash
.venv/Scripts/python.exe tests/stress_lookup.py
```

518 checks, all passing. Free by design: it monkeypatches every Tavily method and the
Anthropic client with a blocker, and **asserts on the spend log rather than on a raised
exception**, because `web_identify` wraps searches in a broad `except Exception` that
would swallow the probe. `--online` adds 26 live checks and does cost money. `-v` for detail.

## Branding

SSC house fonts: Crimson Text (serif, headings) + Hind (body), matching
loadingzonesafety.com. Red is `--red: #e10600`.

## Open items

- **19 rows where the model code disagrees with the stored capacity** (Hyundai 15BRP-9
  1361→1500, Komatsu FG25T-16 2200→2500, Heli CPYD25C 2200→2500, Linde E35PHL/E35SH
  3100→3500, Linde HT25CT 2200→2500, HT27CT 2400→2700, …). All metric brands holding US
  pounds conversions. By the convention above the code should win, but this is a decision
  about whether the catalog records the metric or the US rating — **ask before normalising.**
- 984 of 1009 rows are still `info_status = 'yellow'` (pending admin review), so the
  status column is mostly one repeated mark.
- All 320 Heli rows lack a spec-sheet PDF; Clark has one on 2 of 59.
- Every Crown `2.0`-suffixed capacity traces to one importer reading Crown's tonnes
  suffix (`FC 4510 - 2.0`) as kilograms.
