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
narrow-aisle, never counterbalance. This keeps going wrong: BYD ECC, Heli, and
then 24 rows corrected on 2026-10-16 — 7 Electric rows sitting in Class IV/V, and
17 IC rows whose class contradicted the tire in their own model code (the Cat
`C`-series had been following *fuel*, and Hyster `S`, a cushion line, was stored
as Class V on 7 of 11 rows).

Both failures are now guarded rather than just documented. `audit_tire_class.py`
reports any recurrence, `fix_truck_class.py` corrects it (dry-run by default),
and the stress suite fails if a single row drifts back — the tolerance is zero,
not a budget.

### Tire type: read it off the code, not the class

The class carries tire for **IC trucks only** (IV cushion, V pneumatic). An
electric rider is Class I on any tire, so the 303 Class I rows hold no tire
information at all — and tire is what decides kit fit.

There is deliberately **no `tire_type` column**. The trucks it matters for are
cross-market counterparts (Yale `ERP18VT`, Hyster `J1.8XN`), which by definition
have no row here, so a stored field could never answer the question.
`tire_from_model()` in `app/ai/tire.py` reads it off the designation instead, and
`crossref` warns when a proposed counterpart swaps cushion for pneumatic.

Conventions, each validated against the Class IV/V rows and the curated
`platform_group` twins:

| OEM | Cushion | Pneumatic |
|---|---|---|
| Yale | `ERC` `ESC` `GC` `GLC` `GDC` | `ERP` `GP` `GLP` `GDP` |
| Hyster | `S` `E` | `H` `J` |
| CAT/Mitsubishi | `C` `2C` `GC` `EC` `FGC` `FBC` `2EPC` | `P` `2P` `GP` `EP` `2EP` `FG` `FD` `FB` `DP` |

Longest prefix wins (`FGC` before `FG`, `2EPC` before `2EP`). `None` is a normal
answer and must be read as *unknown*, never as *no difference*. Don't add an OEM
without running `audit_tire_class.py` against it first — the `CPD` trap below is
what guessing from a code string looks like when it goes wrong.

### Heli model codes

`C` forklift · `P` counterbalanced · fuel letter · optional `D` · capacity in 100 kg.
Fuel letter: `C` diesel, `Q` gasoline, `Y` LPG, `QY` dual, none = electric.
A trailing `D` means torque converter on IC trucks; no `D` means mechanical clutch.

**Trap:** `CPD` means *diesel-clutch* on an IC family page but *electric* on an
electric family page. `CPD70-CU7G` is a diesel; `CPD25-GE2G` is a lithium electric.
Resolve `CPD` by the category page it came from, never by the string alone.

## Kits

Kits must **not** transfer between Hyster/Yale twins, even where the trucks are
mechanically identical. Don't link them. **40 existing links already break this**
— see the open item below; they were not created by this rule's enforcement and
have not been removed.

### The `Fits:` line is the fitment authority

Every kit's `notes` holds its own fitment statement:

```
OEM: Toyota
Fits: 8FGCU20-32, 8FGCU35U-70U, 8FGU20-32, 8FG35U-80U, 8FDU20-32
Caveats: ...
```

A truck **inside a stated range is documented to fit** — that is SSC's own
paperwork, not an inference. A truck outside every stated range is a question
only a technician can answer, so nothing may be linked beyond what a `Fits:`
line names. `Caveats:` is **not** fitment: it carries serial cutoffs, dates,
installer notes, and sometimes the counterpart brand.

`app/kit_ranges.py` reads those lines; `audit_kit_ranges.py` reconciles them
against the catalog (read-only). Three things the parser must keep doing, each
of which cost real time to get right:

- **Scope every code to the `OEM:` line.** Model codes collide across brands
  (Yale `GP40N` vs CAT `GP40N`), and a first pass linked the wrong one.
- **Expect digits in a suffix.** `GC35-50K5` is GC35K5..GC50K5 and
  `EC15-18LN3` is EC15LN3..EC18LN3. A `[A-Za-z]*` suffix pattern silently
  drops the generation digit and claims an N3 kit fits an LN3 truck.
- **The Hyster kits name their Yale twin inline.** Kit 77 is
  `H135-160FT | S135-160FT | GC/GLC/GDC135-160VX | GP/GLP/GDP135-160VX` under
  `OEM: Hyster`. `GC GLC GDC GP GLP GDP ERC ERP ESC` are Yale prefixes (see
  the tire table above), so the parser sets them aside as another brand.
  Without that, the gap list proposed creating 37 Hyster-branded rows for
  trucks already on file as Yale.

**A range's number is not always an ordered scale.** Reading `ETG216-318` as
the interval 216..318 assumes it is, and Jungheinrich's leading digit is the
*series*: `ETG 230` is rated 1300 kg, **below** `ETG 216` at 1600 kg. Section 0
of `audit_kit_ranges.py` tests every range for this by checking that stored
capacity rises with the model number, and holds the failures back. The same
test catches a plain bad capacity in a family that is ordered — which is how
the Toyota `7FGCU30` row below was found.

Gaps are filled with `fill_kit_gaps.py` (dry run by default, `--apply` to
write). It inserts a row only when the web confirms the **exact** designation
asked for under the right OEM, with every field filled, a reachable spec-sheet
PDF **that names this model**, and a capacity in order with the siblings
already on file. Never derive a row from code arithmetic.

**The open web could not fill the Toyota 8-series or CAT N-series gaps**
(tried 2026-10-27, 25 candidates). Two reasons, both likely to recur:

- **No sheet names the individual model.** All 10 that passed the field check
  rested on a family-level PDF covering a span of trucks, leaving the
  extractor to pick one capacity out of a chart. That is how `7FGU80` and
  `7FDU80` came back at 7938 kg — 17,500 lb exactly — for an 8,000 lb truck,
  and `7FGCU70` at 3175 kg when `7FGCU35` is already on file at 3600 kg.
- **The web answers in pounds, the catalog stores metric.** Every figure
  returned was an exact pound conversion — 2268 = 5,000 lb, 2722 = 6,000 lb,
  2948 = 6,500 lb, 3175 = 7,000 lb — while every existing `25` in these
  families is stored 2200 and every `30` is 2700. Both readings are real
  (Toyota's NA nameplate vs the metric family), so writing them would add 10
  rows under a second convention. **That is the same question as the 19 rows
  below, and the user's call.** `reconcile_capacity()` would not catch it:
  2268 against 2200 is 3%, inside its 12% tolerance.

### Fitment keyed on serials (`kit_fitment`, `serial_record`)

The project is now an internal sales tool, and the kit is the spine rather than
the forklift. A kit does not simply fit a model or not — it can fit a model up
to a serial and not after it, because production changes mid-model:

| Kit | Fits | Serial |
|---|---|---|
| `174-MCA1-00G05-01` | GC35-70K, GP40-50K, FGC35-70K, FG40-50K | `00001-10230` |
| `174-MCA2-00G05-01` | GC35-50K, GP40-50K, FGC35-50K, FG40-50K | `10231+` |

`kit_forklift` cannot express that and still exists — `kit_fitment` is
**additive** and runs alongside it until the new path is proven. Retiring it is
a separate decision.

**`kit_fitment`** — one curated rule per kit × OEM × model expression × serial
condition. `model_expr` is stored **as written** (`GC35-50K`), never expanded to
one row per truck: `app/kit_ranges.py` reads it at query time, and expanding it
here would bake one reading of the notes into the data permanently.
`condition` is one of:

- `always` — no serial constraint on record. 157 of the 178 migrated rows.
- `range` — `[serial_lo, serial_hi]`, either bound nullable (`hi` NULL = `10231+`),
  optionally scoped by `serial_scope`. 12 rows.
- `pattern` — a test only a person can read, held verbatim in `serial_pattern`
  (`'within the serial # if there is "3"'`). 9 rows. **Answers `None`, never a
  quiet match.**

`origin` (`documented` | `observed`) must stay split. The range a salesperson
sees is the documented bound *widened by* what order management actually
supplied, and a rule drifting far past its documented bound is the signal that
something is wrong — invisible once the two are blended into one number.

**`serial_record`** — append-only facts from order management (part number, OEM,
model, serial, customer, location, order ref). **An order record is truth**
(user's ruling): a serial outside a documented range *widens* that range, because
OM only records kits actually supplied. A wrong record is corrected by
re-pointing it at another kit or superseding it — **never** by editing or
deleting the row, because the derived ranges are rebuilt from these rows and an
edit rewrites history silently.

### Serial comparability (`app/serials.py`)

**A serial comparison is refused unless the scheme is known to be ordered.**
`in_range()` returns `None` for "cannot say", which is a different answer from
`False`, and callers must treat it as such. Three real cases produce it:

- **Bobcat/Doosan is `UNORDERED`** — batch-allocated, so a higher sequence can
  predate a lower one. `serial >= 10231` is meaningless there; exact matching
  only, never a range.
- **Hyster/Yale sequences count only within their 4-char prefix**, which names
  the design generation and model family. `01515` under `G004` has no
  relationship to `01515` under `H177`, so every point carries a scope and
  cross-scope comparison is refused.
- **Most OEMs have no scheme on file.** Toyota is deliberately **not** listed as
  ordered — nothing in this repo documents its serial structure, and `int()` of
  whatever digits appear is exactly the guess that reads as confident and is
  wrong.

Only `CAT/Mitsubishi`, `Hyster` and `Yale` are `ORDERED`. Adding an OEM means
evidence — a documented serial structure, or a range SSC already relies on — not
inference.

### Reading a serial caveat off a kit sheet

6 of the 119 kits mention a serial and **each was read by hand** in
`migrate_kit_fitment.py` (`_SERIAL_RULES`). There is no grammar to generalise
from six examples, and two of them are traps:

- **Kit 81** says `Series R177, within the serial # of the truck`. `R177` is a
  *documented Hyster prefix* (`data/reference/hy_serial_prefixes.json`: H40-70FT,
  4000-7000 lbs, 4-wheel pneumatic), so it is a **scope, not a range**. Its
  recorded twin `E875` is Yale `GP040-070VX` — same capacity, same tire — so the
  kit's Yale `GP040-070VX` row gets `E875`. The kit's other Yale rows
  (`GC`/`GLC` are **cushion**, plus `GLP`/`GDP`) are *not* covered by E875,
  which is pneumatic-only, so they keep the caveat verbatim as a `pattern`
  rather than borrowing a prefix that does not name them.
- **Kits 100 and 101** name Linde series numbers (`1276`, `1346`) read out of the
  serial. Linde has no scheme in `app/serials.py`, so even a real bound could
  not be compared. Verbatim.

A kit with nothing on record gets `always` — the sheet's own claim, no more.

### Intake: order records in, observed ranges out

```bash
.venv/Scripts/python.exe import_serials.py orders.csv          # preview, writes nothing
.venv/Scripts/python.exe import_serials.py orders.csv --apply
.venv/Scripts/python.exe import_serials.py --derive-only       # rebuild ranges only
```

`app/serial_intake.py` holds the parsing and validation (not the script) so the
CSV path and a future web form cannot disagree about them. Headers are matched
**by meaning, not position** — `Order No`, `Truck Make`, `Model Number`,
`Serial Number`, `Site` and about thirty other spellings all land on the right
field, so order management's own export should work unchanged. Required: part
number, OEM, model, serial. Optional: customer, location, order ref, note.
`--show-mapping` prints what matched.

**Nothing is rejected for being unrecognised.** A part number not in the kit
catalog and a serial that cannot be placed in its OEM's scheme are *warnings*;
the row is still written with `kit_id` or `serial_seq` left null. The record is
order management's fact; our ability to interpret it is ours, and discarding the
row loses the only copy.

**Re-importing adds nothing.** Identity is `order_ref` + part number + serial
where there is an order reference, and part number + OEM + model + serial where
there is not. Checked against the database *and* within the file being read.

#### The derivation pass (`app/fitment_derive.py`)

**It never edits a `documented` row.** The widening happens in a separate
`observed` row beside it, so you can always see which part of a range came from
the sheet and which came from the field. The effective range is the union;
`app/fitment.py` reports each hit separately, so a rule that has drifted far
past its documented bound stays visible instead of being blended away.

**It never narrows.** The absence of an order record at some serial is not
evidence that the kit does not fit there — it is evidence nobody has ordered one
yet.

**It never ranges an unordered scheme.** Doosan records produce no range at all.
They are still recorded, and the lookup matches them by **exact serial**, which
is stronger evidence than any range and needs no scheme at all.

Observed rows are rebuilt from scratch on every pass (`origin='observed'` and
`confirmed_by='derive'` is the pass's own territory) — they are derived data, and
recomputing is the only way a *superseded* record actually stops counting.

Two things get flagged for a person rather than resolved:

- **observed past the documented bound** — the sheet says `00001-10230` and the
  records reach 10400.
- **one truck, several kits** — two part numbers both recorded against one
  serial.

#### `supplied` outranks everything

A record of this exact truck having been supplied this kit beats every rule,
*including a rule that excludes it*: the kit demonstrably went on that machine.
That is the user's ruling — an order record is truth — and it lives in
`kits_for()`. The verdict is `supplied` and the reason names the customer.

### The lookup (`app/fitment.py`)

`kits_for(db, oem, model, serial)` answers the salesperson's actual question and
returns a verdict per rule. The three are kept apart on purpose:

- `confirmed` — a rule names the model and the serial falls inside it.
- `excluded` — a rule names the model and the serial falls **outside** it. Shown,
  not dropped: it is the reason the other kit is the right one.
- `unconfirmed` — the serial cannot be placed. Unordered OEM, unreadable serial,
  a `pattern` condition, or no serial given. Never shown as a fit, never as a miss.

An **empty** list means no rule names the model at all, which is different from
"no kit fits it", and the caller must say so in those words.

Both query shapes resolve: a single truck (`GP40K`, found inside the range the
sheet wrote) and the sheet's own wording (`GP40-50K`, which names itself).

### The kit-centric pages

| Route | What it answers |
|---|---|
| `GET /kits` | every kit, grouped by the **truck's** OEM (default) — a kit fitting several makes appears under all of them. Other sorts: part number, most recently confirmed, most trucks confirmed, needs attention. |
| `GET /kit/{kid}` | one kit: documented and observed rules **side by side, never merged**, plus the order records behind them and any superseded ones. |
| `GET,POST /fit` | the salesperson's `(OEM, model, serial)` lookup. GET so a result is a shareable link; POST is the same thing over HTMX. |

`app/kit_view.py` assembles both shapes; it sits apart from `app/fitment.py`
because it reads `app/fitment_derive.py`, which reads `fitment` — a view helper
is the right place for that knot, not the lookup.

Styling is in `app/static/fitment.css`, kept separate like `serial.css`. The one
thing it has to get right: **`unconfirmed` must not look like either answer.**
It gets amber and a dotted edge — visibly unfinished — because giving it the
green of a confirmed fit or the red of an exclusion would undo in CSS exactly
what the code works to keep apart.

Two wording traps, both already hit:

- A rule scoped to a serial prefix with no numeric bounds is **not** "any
  serial" — kit 81 fits only trucks carrying prefix R177, and dropping the
  scope reads as the opposite of what it means. `serial_display` says
  `series R177`, and `_weigh` says "in" rather than "within" for it
  (`KitFitment.bounded` is the test).
- The fit card shows the rule's **origin**, not its whole `source_note`. The
  provenance sentence is internal and belongs on the kit's own page, not in
  front of a customer.

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

851 checks. Free by design: it monkeypatches every Tavily method and the
Anthropic client with a blocker, and **asserts on the spend log rather than on a raised
exception**, because `web_identify` wraps searches in a broad `except Exception` that
would swallow the probe. `--online` adds 26 live checks and does cost money. `-v` for detail.

Section 16 is serial comparability (`tests/test_serials.py`, 41 checks, pure
string work) and section 17 is kit fitment (`tests/test_kit_fitment.py`,
43 checks). Section 17 **does read the database** — it checks the rows
`migrate_kit_fitment.py` wrote, not a fixture, because the thing most likely
to be wrong is the reading of a kit sheet rather than the comparison code.
Section 18 is order-record intake and range derivation
(`tests/test_serial_intake.py`, 45 checks), which builds its OWN database
in a temp file and never touches `data/forklifts.db` — its cases need
customer names and order references, and inventing those in the real
database would leave fabricated orders sitting next to real ones with
nothing to tell them apart. All three run on their own too.

Sections 11–15 are the kit `Fits:` reader, which lives in
`tests/test_kit_ranges.py` and shares this suite's scoreboard. It also runs on
its own (`.venv/Scripts/python.exe tests/test_kit_ranges.py`, 176 checks) and
is pure string work — no database, no network.

The suite is idempotent again (fixed 2026-10-27). It had passed once and then
failed three "uses the spec-sheet search" probes on every later run: the
blocked Tavily call raised, `web_identify` swallowed it in its broad
`except Exception`, and the empty result was written to `web_cache` — which,
once that cache became shared and persistent, outlived the process. `finish()`
in `find_spec_sheet` now takes `searched=False` on that path and caches
nothing, because a result the web never gave says nothing about the model.

## Branding

SSC house fonts: Crimson Text (serif, headings) + Hind (body), matching
loadingzonesafety.com. Red is `--red: #e10600`.

## Open items

- **`174-MCA1-00G05-01`'s notes field says only `test link`.** The MCA1 half of
  the flagship serial pair was never entered: no `OEM:` line, no `Fits:` line, so
  `kit_ranges` reads nothing from it. Its four `kit_fitment` rows and the
  `00001-10230` range come from `_SHEET_FITS` in `migrate_kit_fitment.py` —
  the sheet's line as quoted in chat on 2026-10-27 — and say so in their
  `source_note`. **The kits.notes field itself is deliberately untouched**; the
  migration does not edit the catalog. The line it should hold is:

  ```
  OEM: CAT/Mitsubishi
  Fits: GC35-70K and GP40-50K / Mitsubishi Forklift FGC35-70K and FG40-50K (serial # 00001-10230)
  ```

  Once that is in the notes, drop `_SHEET_FITS` and the rows migrate like every
  other kit's.

- **16 of the 119 kits have no readable `Fits:` line**, so they have no
  `kit_fitment` rows at all and cannot be found by model at all. Nine are blank
  (`#57 174-CSC52`, `#59 174-CPR45`, `#60`/`#61 174-CRC55`, `#62 174-CSP34`,
  `#63 174-CFC45`, `#64 174-CTR4`, `#65 174-CPC4`, `#66 174-CMPC3`,
  `#119 174-RK6NC`); the rest name something the parser refuses rather than
  guesses at — `#13 EC22N2-EC30LN2`, `#30 Platinum II trucks`,
  `#53 8610Tugger`, `#86 E30HSDS3-E40HSD3`, `#110 16-SERIES TRUCKS`,
  `#116 CarryAll 300-500`. Run `.venv/Scripts/python.exe migrate_kit_fitment.py`
  (dry run) for the current list.

- **Kit 89's model expression reads as `H3.0`, dropping `OXT`.** Its notes say
  `Fits: H3.0 OXT`; `kit_ranges` splits on the space and keeps only the first
  token, so the stored `model_expr` is `H3.0`. Not fixed here because the fix
  belongs in the parser and would move other kits' readings with it.

- **Kit 101's caveat contradicts its `Fits:` line.** `Fits: 25-27C` against
  `Caveats: ... Can only be a model E18S-E20S`. Both are recorded (the caveat is
  in `source_note`); which one is right is a question for the sheet's author.

- **`deploy/seed.db` has not been refreshed** for `kit_fitment` /
  `serial_record`. The tables are created by `create_all` on startup, so a
  deploy will not break — but it will come up with zero fitment rows until the
  seed is rebuilt per the procedure above. Do that when the UI for this lands,
  not before.

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

- **40 of the 433 `kit_forklift` links join a Hyster kit to a Yale truck**, which the
  Kits rule above forbids. They are not an oversight: kits 77/79/81/82/83/84/86
  declare `OEM: Hyster` and their own `Fits:` lines name the Yale counterpart.
  So the standing rule and the kits' own documentation disagree, and the user's
  rule is that the `Fits:` notes are the authority. **Decided to leave them in
  place and not create more** (2026-10-27) — a spec sheet can't settle it, since
  a truck's sheet never mentions aftermarket kits. Listed by
  `audit_kit_ranges.py` section 4. Which rule wins is a question about what this
  catalog publishes, and is the **user's call**.

- **Four more capacity rows that contradict their own model code**, found by
  `audit_kit_ranges.py` section 0 and *not* in the 19-row list above:

  | OEM | Model | Stored | Looks like |
  |---|---|---|---|
  | Toyota | 7FGCU30 | 1300 | 3000 — sits between 7FGCU25 at 2200 and 7FGCU32 at 2900, and equals 7FGCU15 exactly, so probably copied from it |
  | CAT/Mitsubishi | GC50K | 6100 | ~5000 — above GC60K at 6000 |
  | CAT/Mitsubishi | NOR030 | 400 | ~1360 — NOR15 is 1360 and `NOR030` is likely the same truck spelled differently |
  | Jungheinrich | ETG 230 | 1300 | not an error — the leading digit is the series, see Kits above |

  Same standing rule as the 19: **do not bulk-normalise.** The Jungheinrich row
  is correct as stored; the other three need checking against the OEM's sheet.

- 984 of 1009 rows are still `info_status = 'yellow'` (pending admin review), so the
  status column is mostly one repeated mark.
- All 320 Heli rows lack a spec-sheet PDF; Clark has one on 2 of 59.
- Every Crown `2.0`-suffixed capacity traces to one importer reading Crown's tonnes
  suffix (`FC 4510 - 2.0`) as kilograms.
