"""Reconcile the catalog against the fitment the kits already document.

READ-ONLY by design, like `audit_tire_class.py`. It opens the database in
SQLite read-only mode and writes nothing: section 2 is a shopping list of web
lookups and section 3 a list of questions, and neither is a batch to apply.

Every kit row carries its own fitment statement in `notes`:

    OEM: Toyota
    Fits: 8FGCU20-32, 8FGCU35U-70U, ...

Those lines are SSC's own documentation, so a truck inside a stated range is
documented to fit -- and a truck outside every stated range is a question only
a technician can answer. `app/kit_ranges.py` reads the lines; this script
compares what they name against what the catalog holds, in three sections:

  1. LINKS NOT MADE -- a catalog row the notes already name, with no row in
     `kit_forklift` joining it to that kit. Free: no new fact is needed, the
     fitment is already written down.
  2. MODELS NOT IN THE CATALOG -- a designation the notes name that has no
     catalog row at all. These are the holes inside the series, and each one
     costs a web confirmation before it can become a row.
  2b. ALREADY HERE UNDER ANOTHER BRAND -- a designation missing under the
     kit's OEM that the catalog already holds under a different one. These are
     the cross-market twins: the Hyster kits' notes name the Yale counterpart
     next to the Hyster model, so `ERC080VH` reads as a missing Hyster truck
     when it is a Yale truck already on file. Creating it would duplicate the
     catalog AND put a Hyster/Yale link one join away, which CLAUDE.md forbids.
  3. NOT READ -- series-level claims, other brands, and tokens the parser
     refused. Reported rather than guessed at.

Run:
    .venv/Scripts/python.exe audit_kit_ranges.py            # sections 1 and 3
    .venv/Scripts/python.exe audit_kit_ranges.py --missing  # add section 2
    .venv/Scripts/python.exe audit_kit_ranges.py --csv out.csv
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.kit_ranges import KitFits, ModelRange, normalise, parse_all  # noqa: E402

DB = Path(__file__).parent / "data" / "forklifts.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _bare(model: str) -> str:
    """How two spellings of one truck are compared: `G20-N7` == `G20N-7`."""
    return normalise(model).replace("-", "").replace(".", "")


def vocabularies(rows) -> dict[str, Counter[int]]:
    """Capacity numbers each OEM actually uses, with how widely each is used.

    The interior of a range cannot be counted out -- `8FGCU20-32` is 20/25/30/32,
    not thirteen trucks -- so it has to come from the OEM's own numbering. The
    count is how many distinct model families in that OEM's catalog use the
    number, which is the only ranking available before anything is confirmed:
    Toyota's 25 appears across 7FGCU/7FGU/7FDU/8FBCU, while 23 appears on 9BXU
    alone.
    """
    seen: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in rows:
        for prefix, number, suffix, _width in _runs(normalise(row["model"])):
            if prefix:
                seen[row["manufacturer"]][number].add(prefix + "|" + suffix)
    return {
        oem: Counter({number: len(families) for number, families in numbers.items()})
        for oem, numbers in seen.items()
    }


def _runs(token: str):
    from app.kit_ranges import _decompose

    return _decompose(token)


# ------------------------------------------------------------------ section 1


def family(rows, oem: str, rng: ModelRange):
    """(number, capacity, model) for every catalog row in a range's family."""
    out = set()
    for row in rows:
        if row["manufacturer"] != oem or row["capacity_kg"] is None:
            continue
        for prefix, number, suffix, _width in _runs(normalise(row["model"])):
            if _bare(prefix) == _bare(rng.prefix) and _bare(suffix) == _bare(rng.suffix):
                out.add((number, row["capacity_kg"], row["model"]))
                break
    return sorted(out)


def capacity_order_breaks(fits: list[KitFits], rows):
    """Ranges whose family's stored capacity does NOT rise with the number.

    Reading `ETG216-318` as the interval 216..318 assumes the number is a
    single ordered scale. For Jungheinrich it is not -- `ETG 230` is a 2-series
    truck rated 1300 kg, BELOW `ETG 216` at 1600 kg, because the leading digit
    is the series and the rest is the capacity. Containment is meaningless
    there, so those ranges are held out of section 1 rather than linked.

    The same test also catches a plain bad capacity in a family that IS ordered
    -- Toyota `7FGCU30` stored at 1300 kg between 7FGCU25 at 2200 and 7FGCU32
    at 2900. Both are a question for a person, which is why this reports rather
    than decides.
    """
    breaks = []
    for kit in fits:
        for rng in kit.ranges:
            points = family(rows, kit.oem or "", rng)
            if len(points) < 2:
                continue
            caps = [cap for _n, cap, _m in points]
            if any(later < earlier for earlier, later in zip(caps, caps[1:])):
                breaks.append((kit, rng, points))
    return breaks


def links_not_made(fits: list[KitFits], rows, links: set[tuple[int, int]],
                   held: set[tuple[int, str]] | None = None):
    """(kit, forklift row, what named it) for each documented link that is absent.

    `held` names (kit_id, range source) pairs whose interval reading is not
    trustworthy -- see `capacity_order_breaks`. A row reached only through one
    of those is left out: section 1 is meant to be the safe half.
    """
    held = held or set()
    by_oem: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_oem[row["manufacturer"]].append(row)

    out = []
    for kit in fits:
        if not kit.readable:
            continue
        exact = {_bare(name) for name in kit.exact}
        for row in by_oem.get(kit.oem or "", ()):
            if (kit.kit_id, row["id"]) in links:
                continue
            named: str | None = None
            if _bare(row["model"]) in exact:
                named = "named outright"
            else:
                for rng in kit.ranges:
                    if (kit.kit_id, rng.source) in held:
                        continue
                    if rng.contains(row["model"]):
                        named = f"inside {rng.source}"
                        break
            if named:
                out.append((kit, row, named))
    return out


# ------------------------------------------------------------------ section 2


def models_not_in_catalog(fits: list[KitFits], rows, vocab: dict[str, Counter[int]]):
    """(oem, designation, kits that name it, confidence) for every gap."""
    have: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        have[row["manufacturer"]].add(_bare(row["model"]))

    found: dict[tuple[str, str], dict] = {}
    for kit in fits:
        if not kit.readable:
            continue
        oem = kit.oem or ""
        numbers = vocab.get(oem, Counter())
        for name, source, weight in _named(kit, numbers):
            if _bare(name) in have[oem]:
                continue
            entry = found.setdefault(
                (oem, name), {"kits": [], "sources": [], "weight": weight},
            )
            entry["kits"].append(kit)
            entry["sources"].append(source)
            # `None` means a kit wrote the designation out, which outranks any
            # interior suggestion for the same code.
            if weight is None or entry["weight"] is None:
                entry["weight"] = None
            else:
                entry["weight"] = max(entry["weight"], weight)
    return found


def _named(kit: KitFits, numbers: Counter[int]):
    """Each designation a kit names, with its source and a confidence weight.

    An endpoint is written out in the notes, so it is as certain as the notes
    get -- weight `None`, reported as "stated". An interior member is this
    script's own suggestion, carrying the number's sibling-family count so the
    likely ones can be confirmed first.
    """
    for name in kit.exact:
        yield name, "stated", None
    for rng in kit.ranges:
        endpoints = (rng.render(rng.lo), rng.render(rng.hi))
        for name in rng.members(set(numbers)):
            weight = None if name in endpoints else numbers.get(_number_of(rng, name), 0)
            yield name, rng.source, weight


def _number_of(rng: ModelRange, rendered: str) -> int:
    body = rendered[len(rng.prefix):]
    if rng.suffix:
        body = body[: -len(rng.suffix)]
    return int(body)


# -------------------------------------------------------------------- report


def main() -> int:
    if not DB.exists():
        print(f"No database at {DB}")
        return 1
    show_missing = "--missing" in sys.argv or "--all" in sys.argv
    csv_path = None
    if "--csv" in sys.argv:
        csv_path = Path(sys.argv[sys.argv.index("--csv") + 1])

    conn = _connect()
    rows = conn.execute(
        "SELECT id, manufacturer, series, model, capacity_kg, fuel_type, truck_class "
        "FROM forklifts ORDER BY manufacturer, model"
    ).fetchall()
    kit_rows = conn.execute("SELECT id, sku, notes FROM kits ORDER BY id").fetchall()
    links = {(r[0], r[1]) for r in conn.execute("SELECT kit_id, forklift_id FROM kit_forklift")}
    conn.close()

    fits = parse_all((r["id"], r["sku"], r["notes"]) for r in kit_rows)
    vocab = vocabularies(rows)
    linked_now = {forklift_id for _kit_id, forklift_id in links}

    print("=" * 78)
    print("KIT FITMENT RECONCILIATION")
    print("=" * 78)
    readable = [k for k in fits if k.readable]
    print(f"{len(kit_rows)} kits, {len(readable)} with a readable Fits: line, "
          f"{sum(len(k.ranges) for k in fits)} ranges, "
          f"{sum(len(k.exact) for k in fits)} models named outright")
    print(f"{len(rows)} catalog rows, {len(rows) - len(linked_now)} with no kit link")

    # ---------------------------------------------------------- section 0
    breaks = capacity_order_breaks(fits, rows)
    held = {(kit.kit_id, rng.source) for kit, rng, _points in breaks}
    print(f"\n{'-' * 78}\n0. RANGES WHOSE NUMBER IS NOT AN ORDERED SCALE -- read these first\n{'-' * 78}")
    print(f"{len(breaks)} of {sum(len(k.ranges) for k in fits)} ranges sit over a family whose")
    print("stored capacity does not rise with the model number. Either the number")
    print("is not a single scale (Jungheinrich's leading digit is the series), or a")
    print("stored capacity is wrong. Both make containment meaningless, so these")
    print("ranges are held out of section 1 below.\n")
    for kit, rng, points in breaks:
        print(f"  kit {kit.kit_id:3d} {kit.oem:16s} {str(rng):22s} <- {rng.source}")
        print("       " + "  ".join(f"{model}={cap:.0f}kg" for _n, cap, model in points))

    # ---------------------------------------------------------- section 1
    missing_links = links_not_made(fits, rows, links, held)
    gains_first = {row["id"] for _kit, row, _why in missing_links} - linked_now
    print(f"\n{'-' * 78}\n1. LINKS NOT MADE -- documented fitment with no kit_forklift row\n{'-' * 78}")
    print(f"{len(missing_links)} kit/truck pairs; {len(gains_first)} of those trucks "
          f"have no kit link at all today.\n")
    by_kit: dict[int, list] = defaultdict(list)
    for kit, row, why in missing_links:
        by_kit[kit.kit_id].append((row, why))
    for kit in readable:
        found = by_kit.get(kit.kit_id)
        if not found:
            continue
        print(f"  kit {kit.kit_id:3d} {kit.sku:24s} {kit.oem}")
        for row, why in sorted(found, key=lambda pair: pair[0]["model"]):
            flag = " *first link*" if row["id"] in gains_first else ""
            print(f"       + {row['model']:22s} ({why}){flag}")

    # ---------------------------------------------------------- section 2
    gaps = models_not_in_catalog(fits, rows, vocab)
    elsewhere: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        elsewhere[_bare(row["model"])].add(row["manufacturer"])

    twins = {key: val for key, val in gaps.items() if _bare(key[1]) in elsewhere}
    gaps = {key: val for key, val in gaps.items() if key not in twins}
    stated = {key: val for key, val in gaps.items() if val["weight"] is None}
    suggested = {key: val for key, val in gaps.items() if val["weight"] is not None}

    print(f"\n{'-' * 78}\n2. MODELS NOT IN THE CATALOG -- the series gaps\n{'-' * 78}")
    print(f"{len(stated)} designations written out in the notes, and "
          f"{len(suggested)} interior members this script suggests.")
    print("Each one needs a web confirmation before it can become a row, so the")
    print("suggested ones are ranked by how many model families in that OEM's")
    print("catalog already use the same capacity number.")
    if show_missing:
        for title, group in (("STATED IN THE NOTES", stated), ("SUGGESTED INTERIOR", suggested)):
            print(f"\n  {title}")
            order = sorted(
                group.items(),
                key=lambda item: (item[0][0], -(item[1]["weight"] or 0), item[0][1]),
            )
            for (oem, name), entry in order:
                weight = entry["weight"]
                rank = "stated" if weight is None else f"{weight} sibling families"
                kits = ",".join(str(k.kit_id) for k in entry["kits"])
                print(f"    {oem:28s} {name:22s} {rank:20s} kit {kits}  <- {entry['sources'][0]}")
    else:
        print("\n  (pass --missing to list them)")

    print(f"\n{'-' * 78}\n2b. ALREADY HERE UNDER ANOTHER BRAND -- do not create, do not link\n{'-' * 78}")
    print(f"{len(twins)} designations. The Hyster kits name the Yale counterpart")
    print("beside the Hyster model, so these read as missing Hyster trucks when")
    print("they are Yale trucks already on file. Adding them would duplicate the")
    print("catalog and put a Hyster/Yale link one join away.\n")
    for (oem, name), entry in sorted(twins.items()):
        holders = ", ".join(sorted(elsewhere[_bare(name)]))
        kits = ",".join(str(k.kit_id) for k in entry["kits"])
        print(f"    kit OEM {oem:16s} {name:22s} on file as {holders:16s} kit {kits}")

    # ---------------------------------------------------------- section 3
    print(f"\n{'-' * 78}\n3. NOT READ -- reported rather than guessed at\n{'-' * 78}")
    for kit in fits:
        bits = []
        if kit.series_refs:
            bits.append("series-level: " + ", ".join(kit.series_refs))
        if kit.foreign:
            bits.append("other brand: " + ", ".join(f"{t!r} ({o})" for o, t in kit.foreign))
        if kit.unparsed:
            bits.append("unreadable: " + "; ".join(repr(u) for u in kit.unparsed))
        if bits:
            print(f"  kit {kit.kit_id:3d} {kit.sku:24s} {kit.oem or '(no OEM)'}")
            for bit in bits:
                print(f"       {bit}")

    # ---------------------------------------------------------- section 4
    # Turned up by the twin rule above. CLAUDE.md: "Kits must not transfer
    # between Hyster/Yale twins, even where the trucks are mechanically
    # identical. Don't link them." The kits' own notes say otherwise, and the
    # user's rule is that the notes are the authority -- so the two rules
    # disagree, on links that already exist. Reported, never touched: which
    # one wins is a question about what this catalog publishes.
    oem_of = {row["id"]: row["manufacturer"] for row in rows}
    model_of = {row["id"]: row["model"] for row in rows}
    kit_oem = {kit.kit_id: kit.oem for kit in fits}
    pair = {"Hyster": "Yale", "Yale": "Hyster"}
    crossed = [
        (kit_id, forklift_id) for kit_id, forklift_id in sorted(links)
        if pair.get(kit_oem.get(kit_id) or "") == oem_of.get(forklift_id)
    ]
    print(f"\n{'-' * 78}\n4. HYSTER/YALE LINKS THAT ALREADY EXIST -- a standing-rule conflict\n{'-' * 78}")
    print(f"{len(crossed)} of the {len(links)} links in kit_forklift join a kit of one")
    print("twin to a truck of the other. CLAUDE.md forbids those links; the kits'")
    print("own Fits: notes name both trucks, and the notes are meant to be the")
    print("authority. Nothing here is changed either way -- which rule wins is a")
    print("question about what the catalog publishes.\n")
    for kit_id, forklift_id in crossed:
        print(f"    kit {kit_id:3d} ({kit_oem.get(kit_id)})  ->  "
              f"{oem_of.get(forklift_id)} {model_of.get(forklift_id)}")

    if csv_path:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            out = csv.writer(handle)
            out.writerow(["section", "kit_id", "sku", "oem", "model", "detail"])
            for kit, row, why in missing_links:
                out.writerow(["link", kit.kit_id, kit.sku, kit.oem, row["model"], why])
            for (oem, name), entry in sorted(gaps.items()):
                weight = entry["weight"]
                out.writerow([
                    "missing", entry["kits"][0].kit_id, entry["kits"][0].sku, oem, name,
                    "stated" if weight is None else f"suggested/{weight}",
                ])
        print(f"\nWrote {csv_path}  (a *.csv report -- do not commit it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
