"""Serial comparability -- app/serials.py.

Pure string and integer work: no database, no network, nothing billable. Shares
the stress suite's scoreboard (`run(R)`) and also runs on its own.

What these checks are really protecting: a kit is quoted off a `>=` against a
serial number. If that comparison runs on a scheme that does not count upwards,
or across two scopes that each count separately, the tool names the wrong part
and looks certain doing it. So most of what is asserted here is that the module
REFUSES -- returns None rather than True or False -- and the distinction between
None and False is itself tested, because a caller that treats them alike
reintroduces the whole problem.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import serials as S  # noqa: E402


def run(R) -> None:
    R.start("16. Serial comparability -- refusing to compare is the feature")

    # --- which schemes may be ranged at all -------------------------------
    R.check("CAT/Mitsubishi is ordered", S.scheme_is_ordered("CAT/Mitsubishi"))
    R.check("Hyster is ordered", S.scheme_is_ordered("Hyster"))
    R.check("Yale is ordered", S.scheme_is_ordered("Yale"))
    # Batch-allocated: a higher number can be an older truck (CLAUDE.md).
    R.check("Doosan is NOT ordered", not S.scheme_is_ordered("Bobcat/Doosan"),
            "Doosan sequences are batch-allocated; ranging them is wrong")
    for oem in ("Toyota", "Crown", "Heli", "Linde", None):
        R.check(f"{oem} has no scheme on file", not S.scheme_is_ordered(oem))

    # --- placing a serial --------------------------------------------------
    p = S.serial_point("CAT/Mitsubishi", "10231")
    R.check("CAT numeric places", p is not None and p.sequence == 10231, f"got {p}")
    p = S.serial_point("CAT/Mitsubishi", "00001")
    R.check("leading zeros do not change the number", p is not None and p.sequence == 1,
            f"got {p}")
    p = S.serial_point("Hyster", "G004V01515D")
    R.check("Hyster serial splits prefix from sequence",
            p is not None and p.scope == "G004" and p.sequence == 1515, f"got {p}")
    p = S.serial_point("Yale", "g004 v01515 d")
    R.check("spacing and case are ignored",
            p is not None and p.scope == "G004" and p.sequence == 1515, f"got {p}")

    for oem, ser, why in (
        ("CAT/Mitsubishi", "ABC123", "not numeric on a numeric scheme"),
        ("Hyster", "nonsense", "does not fit the Hyster shape"),
        ("Bobcat/Doosan", "L7-00116", "scheme is unordered"),
        ("Toyota", "12345", "no scheme on file"),
        ("CAT/Mitsubishi", "", "empty"),
        ("CAT/Mitsubishi", None, "missing"),
    ):
        R.check(f"{oem} {ser!r} is unplaceable ({why})",
                S.serial_point(oem, ser) is None)

    # --- the case this exists for -----------------------------------------
    # 174-MCA1-00G05-01 fits to 10230, 174-MCA2-00G05-01 from 10231.
    for ser, in_mca1 in (("00001", True), ("05000", True), ("10230", True),
                         ("10231", False), ("12000", False)):
        got = S.in_range("CAT/Mitsubishi", ser, 1, 10230)
        R.check(f"serial {ser} in MCA1 range -> {in_mca1}", got is in_mca1, f"got {got!r}")
        got2 = S.in_range("CAT/Mitsubishi", ser, 10231, None)
        R.check(f"serial {ser} in MCA2 open range -> {not in_mca1}",
                got2 is (not in_mca1), f"got {got2!r}")

    # --- None is not False -------------------------------------------------
    doosan = S.in_range("Bobcat/Doosan", "00116", 1, 10230)
    R.check("a Doosan range answers None, not False",
            doosan is None, f"got {doosan!r} -- False would read as 'this kit does not fit'")
    unknown = S.in_range("Toyota", "12345", 1, 10230)
    R.check("an unknown scheme answers None, not False", unknown is None, f"got {unknown!r}")

    # --- scope isolation ---------------------------------------------------
    R.check("a Hyster serial outside the rule's prefix answers None",
            S.in_range("Hyster", "G004V01515D", 1, 99999, scope="H177") is None)
    R.check("the same serial inside its own prefix answers True",
            S.in_range("Hyster", "G004V01515D", 1, 99999, scope="G004") is True)
    a = S.serial_point("Hyster", "G004V01515D")
    b = S.serial_point("Hyster", "H177B01515C")
    R.check("equal sequences under different prefixes are not comparable",
            not S.comparable(a, b),
            "01515 under G004 has no relationship to 01515 under H177")
    R.check("a point is not comparable with nothing", not S.comparable(a, None))

    # --- open bounds -------------------------------------------------------
    R.check("no bounds means any placeable serial matches",
            S.in_range("CAT/Mitsubishi", "55555", None, None) is True)
    R.check("no bounds still refuses an unplaceable serial",
            S.in_range("Bobcat/Doosan", "00116", None, None) is None)
    R.check("open bottom", S.in_range("CAT/Mitsubishi", "5", None, 10230) is True)
    R.check("open bottom excludes above", S.in_range("CAT/Mitsubishi", "99999", None, 10230) is False)

    # --- the UI sentence ---------------------------------------------------
    R.check("Doosan explains batch allocation",
            "batch" in S.explain("Bobcat/Doosan").lower())
    R.check("an unknown OEM explains itself",
            "no serial-number scheme" in S.explain("Toyota").lower())


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from stress_lookup import R  # noqa: E402

    run(R)
    raise SystemExit(R.summary())
