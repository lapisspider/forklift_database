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

- **19 rows where the model code disagrees with the stored capacity.** Reproduce with
  `reconcile_capacity()` over the catalog; the list is stable:

  | OEM | Model | Stored | Coded |
  |---|---|---|---|
  | Heli | CPYD25C | 2200 | 2500 |
  | Heli | CQD14X2 | 1350 | 1400 |
  | Heli | CQD16X1 | 1500 | 1600 |
  | Heli | QYCD20 | 2040 | 2000 |
  | Heli | QYCD25 | 2550 | 2500 |
  | Heli | QYCD30 | 3060 | 3000 |
  | Heli | QYD45S-SU | 4000 | 4500 |
  | Hyundai | 15BRP-9 | 1361 | 1500 |
  | Hyundai | 18BRP-9 | 1588 | 1800 |
  | Hyundai | 20BRP-9 | 1814 | 2000 |
  | Hyundai | 23BRP-9 | 2041 | 2300 |
  | Hyundai | 25BC-9 | 2200 | 2500 |
  | Komatsu | FG20T-16 | 1800 | 2000 |
  | Komatsu | FG25ST-16 | 2200 | 2500 |
  | Komatsu | FG25T-16 | 2200 | 2500 |
  | Linde | E35PHL | 3100 | 3500 |
  | Linde | E35SH | 3100 | 3500 |
  | Linde | HT25CT | 2200 | 2500 |
  | Linde | HT27CT | 2400 | 2700 |

  **Do not bulk-normalise these.** They are three different situations:

  1. *Genuine rating difference, not an error.* The Hyundai `BRP-9` rows are exact pound
     figures (1361 kg = 3000 lb, 1814 kg = 4000 lb). Hyundai's model code names the metric
     family while its North American nameplate rates lower — 1500 kg is 3307 lb, so these
     are two real ratings, not a botched conversion. Deciding between them is a question
     about what this catalog publishes, and is the **user's call**.
  2. *Probable conversion artifact.* Linde and Komatsu rows look like pounds round-trips
     on trucks whose code states the metric rating.
  3. *Possible bad decode — check before touching.* The four Heli `QY…` rows move the
     wrong way (stored figure **above** the coded one). `reconcile_capacity()` assumes the
     first 2–3 digits are capacity in 100 kg, which holds for `CPxD` counterbalance codes
     but is unverified for the `QY` lines. Confirm what `QY` designates on Heli's own
     category page before treating these as errors.

- 984 of 1009 rows are still `info_status = 'yellow'` (pending admin review), so the
  status column is mostly one repeated mark.
- All 320 Heli rows lack a spec-sheet PDF; Clark has one on 2 of 59.
- Every Crown `2.0`-suffixed capacity traces to one importer reading Crown's tonnes
  suffix (`FC 4510 - 2.0`) as kilograms.
