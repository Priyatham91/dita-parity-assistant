"""Smoke-test the Help Center HTML parser against a real fetched page."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.article_html_parser import blocks_to_strings, parse_help_center_html


SAMPLE = Path(r"C:\Users\parukala\AppData\Local\Temp\dpa_hc_sample.html")


def main() -> int:
    if not SAMPLE.exists():
        print(f"Sample HTML not found at {SAMPLE}; run the curl fetch first.")
        return 1

    html = SAMPLE.read_text(encoding="utf-8")
    blocks = parse_help_center_html(html)
    strings = blocks_to_strings(blocks)

    print(f"Parsed {len(blocks)} structured blocks -> {len(strings)} diff strings\n")

    # Show kind distribution.
    by_kind: dict = {}
    for b in blocks:
        by_kind[b.kind] = by_kind.get(b.kind, 0) + 1
    print(f"By kind: {by_kind}\n")

    print("--- Blocks ---")
    for i, b in enumerate(blocks, 1):
        note_marker = f" ({b.note_kind})" if b.note_kind else ""
        preview = b.text if len(b.text) <= 90 else b.text[:87] + "..."
        print(f"  {i:2d}. [{b.kind}{note_marker}] {preview}")

    print("\n--- Diff strings (what feeds into the diff engine) ---")
    for i, s in enumerate(strings, 1):
        preview = s if len(s) <= 90 else s[:87] + "..."
        print(f"  {i:2d}. {preview}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
