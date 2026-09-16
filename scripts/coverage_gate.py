#!/usr/bin/env python3
"""Enforce the per-module coverage floors in ``coverage.floors``.

Two rules, and the second is the point:

* a module below its floor fails;
* a module listed in neither the floors nor the exclusions **also** fails, so a new module gets
  a coverage decision on purpose rather than acquiring 0% by being forgotten.

Run it through ``make coverage-gate``, which produces the JSON report first. It reads
``coverage.json`` rather than the binary ``.coverage`` so that it needs nothing but the standard
library and can be pointed at a report produced anywhere -- in CI that report comes from the
merged unit and integration profiles.

``percent_covered`` is the statement-and-branch figure, the same number
``coverage report`` prints, because ``branch = true`` is set in pyproject.toml.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO_ROOT / "src"


def load_floors(path: Path) -> tuple[dict[str, float], set[str]]:
    floors: dict[str, float] = {}
    excluded: set[str] = set()
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, value = line.partition(" ")
        value = value.strip()
        if key == "exclude":
            if not value:
                sys.exit(f"{path}:{lineno}: 'exclude' with no module path")
            excluded.add(value)
            continue
        if not value:
            sys.exit(f"{path}:{lineno}: '{key}' has no floor")
        try:
            floors[key] = float(value)
        except ValueError:
            sys.exit(f"{path}:{lineno}: '{value}' is not a percentage")
    return floors, excluded


def measured(report: dict) -> dict[str, float]:
    return {name: entry["summary"]["percent_covered"] for name, entry in report["files"].items()}


def source_modules() -> set[str]:
    """Every module the floors have to account for.

    ``__init__.py`` is omitted in pyproject.toml's ``[tool.coverage.run]``, so it never appears
    in a report and must not be demanded here either.
    """
    return {
        str(p.relative_to(REPO_ROOT)) for p in SOURCE_DIR.rglob("*.py") if p.name != "__init__.py"
    }


def main(argv: list[str]) -> int:
    report_path = Path(argv[1] if len(argv) > 1 else "coverage.json")
    floors_path = Path(argv[2] if len(argv) > 2 else "coverage.floors")

    if not report_path.is_file():
        print(
            f"coverage report {report_path} not found; run 'make test-cov' first",
            file=sys.stderr,
        )
        return 1
    if not floors_path.is_file():
        print(f"floors file {floors_path} not found", file=sys.stderr)
        return 1

    floors, excluded = load_floors(floors_path)
    coverage = measured(json.loads(report_path.read_text()))

    # A module that the report never mentions still has to be accounted for: an unimported
    # module measures nothing, which is exactly the case the second rule is about.
    modules = sorted(source_modules() | set(coverage))

    status = 0
    for module in modules:
        if module in excluded:
            print(f"  skip  {module:<44} excluded")
            continue
        if module not in floors:
            print(f"  FAIL  {module:<44} no floor and no exclusion in {floors_path}")
            status = 1
            continue
        actual = coverage.get(module)
        if actual is None:
            print(f"  FAIL  {module:<44} has a floor but is missing from {report_path}")
            status = 1
            continue
        floor = floors[module]
        # Compare on the printed figure: a module reported as 71.0% against a floor of 71 must
        # not fail on a hundredth of a percent that no report ever shows.
        if round(actual, 1) + 1e-9 < floor:
            print(f"  FAIL  {module:<44} {actual:.1f}% < {floor:g}%")
            status = 1
        else:
            print(f"  ok    {module:<44} {actual:.1f}% >= {floor:g}%")

    for stale in sorted(set(floors) - set(modules)):
        print(f"  note  {stale:<44} floor for a module that no longer exists — drop the line")

    if status:
        print()
        print("A module below its floor is either a test you have not written yet or a floor")
        print("that should move. Lowering one is a reviewable change: say why in the PR.")
    else:
        # The ratchet only works if it is tightened. Say so when there is room.
        room = [
            (m, coverage[m], floors[m])
            for m in modules
            if m in floors and m in coverage and math.floor(coverage[m]) > floors[m]
        ]
        if room:
            print()
            print(f"{len(room)} module(s) now measure above their floor — raise them:")
            for module, actual, floor in sorted(room, key=lambda r: r[1] - r[2], reverse=True)[:10]:
                print(f"    {module:<44} {floor:g} -> {math.floor(actual):g}")

    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
