"""Confirm typography-only differences fold to equal under normalize_for_match."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.publication_reconstructor import normalize_for_match


def main() -> int:
    cases = [
        # (left, right, should_be_equal_after_normalize, label)
        ("organization's website", "organization’s website", True,
         "smart vs ASCII single quote"),
        ('the "Save" button', "the “Save” button", True,
         "smart vs ASCII double quotes"),
        ("Not Reversible When you delete", "Not Reversible – When you delete", False,
         "different content (en-dash + missing word)"),
        ("a - b", "a — b", True,
         "hyphen and em-dash with spacing — both fold to 'a - b'"),
        ("a - b", "a - b", True,
         "identical hyphen"),
        ("a–b", "a-b", True,
         "en-dash vs hyphen, no spacing"),
        ("foo bar", "foo bar", True,
         "non-breaking space vs regular space"),
        ("etcetera…", "etcetera...", True,
         "ellipsis vs three dots"),
        ("Click  Save", "Click Save", True,
         "double space vs single space"),
        ("Click Save", "Click Save Now", False,
         "real semantic change"),
    ]

    failures = 0
    for left, right, should_eq, label in cases:
        ln = normalize_for_match(left)
        rn = normalize_for_match(right)
        eq = ln == rn
        status = "OK" if eq == should_eq else "FAIL"
        if eq != should_eq:
            failures += 1
        print(f"[{status}] {label}")
        print(f"       L: {ln!r}")
        print(f"       R: {rn!r}")
        print(f"       eq={eq}  expected={should_eq}")
        print()

    print(f"{len(cases) - failures}/{len(cases)} cases passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
