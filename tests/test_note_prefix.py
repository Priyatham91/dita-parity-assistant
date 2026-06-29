"""Sanity check for IM_note05 prefix stripping in the article splitter."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.diff_engine import _strip_note_prefix


def main() -> int:
    cases = [
        # (input, expected, should_be_stripped)
        ("Important to know: Workspace names cannot be changed later.",
         "Workspace names cannot be changed later.", True),
        ("Important to know  Workspace names cannot be changed later.",
         "Workspace names cannot be changed later.", True),
        ("important to know - workspace names cannot be changed later.",
         "workspace names cannot be changed later.", True),
        ("Here's a tip: You can drag tasks between projects later.",
         "You can drag tasks between projects later.", True),
        ("Here’s a tip: smart-quote variant",
         "smart-quote variant", True),
        ("Who can use this feature: Page admins only.",
         "Page admins only.", True),
        # False-positive guards: leading "Note" / "Important" without the
        # full marker phrase must NOT be stripped.
        ("Note that you must enable the setting first.",
         "Note that you must enable the setting first.", False),
        ("Important context for the next paragraph.",
         "Important context for the next paragraph.", False),
        ("Workspace names cannot be changed later.",
         "Workspace names cannot be changed later.", False),
        # Marker-only lines (no body) get dropped entirely — these are
        # publish-time decorations, not content.
        ("Who can use this feature?",
         "", True),
        ("Important to know",
         "", True),
        ("Important to know.",
         "", True),
        ("Here's a tip!",
         "", True),
    ]

    failures = 0
    for raw, expected, should_strip in cases:
        actual = _strip_note_prefix(raw)
        ok = actual == expected
        stripped = raw != actual
        status = "OK" if ok else "FAIL"
        print(f"[{status}] {raw!r}")
        print(f"       -> {actual!r}")
        if not ok:
            print(f"  expected: {expected!r}")
            failures += 1
        if stripped != should_strip:
            print(f"  expected stripped={should_strip}, got {stripped}")
            failures += 1

    print()
    print(f"{len(cases) - failures}/{len(cases)} cases passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
