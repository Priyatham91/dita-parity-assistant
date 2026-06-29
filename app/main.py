"""End-to-end runner: parse map -> reconstruct -> diff -> patch -> report.

Current scope:
  - Diff engine emits EQUAL / REPLACE / INSERT / DELETE
  - Patch engine applies REPLACE only (INSERT/DELETE are ignored)
  - Report writes a human-readable patch_report.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python app/main.py ...` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.diff_engine import OpKind, diff, split_updated_article, summarize
from app.html_report import write_html_report
from app.map_parser import TopicRef, parse_ditamap_entries
from app.patch_engine import apply_ops
from app.publication_reconstructor import reconstruct
from app.report_generator import write_patch_report
from app.schematron_validator import validate_files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", required=True, help="Path to the .ditamap file")
    parser.add_argument("--article", required=True, help="Path to the updated article (.txt)")
    parser.add_argument("--output", default="output", help="Output directory (default: output)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run all checks and produce the report, but do not write patched .dita files.",
    )
    args = parser.parse_args()

    map_entries = parse_ditamap_entries(Path(args.map))
    topic_refs = [e for e in map_entries if isinstance(e, TopicRef)]
    publication = reconstruct(map_entries)
    updated_blocks = split_updated_article(Path(args.article).read_text(encoding="utf-8"))
    ops = diff(publication, updated_blocks)

    output_dir = Path(args.output)
    patch_report = apply_ops(
        ops, output_dir, publication=publication, dry_run=args.dry_run,
    )

    # Schematron validation of patched files (no-op if no .sch or lxml absent).
    schematron_dir = Path(__file__).resolve().parent.parent / "schematron"
    validation_report = validate_files(patch_report.files_written, schematron_dir)

    write_patch_report(
        patch_report, output_dir / "patch_report.txt",
        validation=validation_report, dry_run=args.dry_run,
    )

    html_path = output_dir / "report.html"
    write_html_report(
        html_path,
        map_path=Path(args.map),
        article_path=Path(args.article),
        topic_refs=topic_refs,
        publication=publication,
        updated_block_count=len(updated_blocks),
        diff_summary=summarize(ops),
        report=patch_report,
        output_dir=output_dir,
        validation=validation_report,
        dry_run=args.dry_run,
    )

    # Dry-run cleanup — see server.py for context.
    if args.dry_run:
        for f in patch_report.files_written:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

    print(f"Topics in map:        {len(topic_refs)}")
    print(f"Source blocks:        {len(publication.blocks)}")
    print(f"Updated blocks:       {len(updated_blocks)}")
    print(f"Diff summary:         {summarize(ops)}")
    print()
    print(f"Ops processed:        {len(patch_report.results)}")
    print(f"  applied:            {len(patch_report.applied)}")
    print(f"  skipped:            {len(patch_report.skipped)}")
    print(f"  detected (manual):  {len(patch_report.detected)}")
    mode_label = "would write" if args.dry_run else "Files written"
    print(f"{mode_label}:        {len(patch_report.files_written)}")
    for f in patch_report.files_written:
        print(f"  - {f}")
    if args.dry_run:
        print("(dry-run: no .dita files were actually written)")
    print(f"Patch report:         {output_dir / 'patch_report.txt'}")
    print(f"HTML report:          {html_path}")

    print()
    print("Non-EQUAL ops (full list):")
    for op in ops:
        if op.kind == OpKind.EQUAL:
            continue
        loc = (
            f"{op.source_block.topic_id}::{op.source_block.element_xpath}"
            if op.source_block is not None
            else (
                f"after {op.anchor_block.topic_id}::{op.anchor_block.element_xpath}"
                if op.anchor_block is not None
                else "<publication start>"
            )
        )
        safe = "" if op.safe_to_apply else "  [MANUAL REVIEW]"
        if op.kind == OpKind.REPLACE:
            print(f"  REPLACE @ {loc}{safe}")
            print(f"    - {op.source_block.text!r}")
            print(f"    + {op.updated_text!r}")
        elif op.kind == OpKind.DELETE:
            print(f"  DELETE  @ {loc}{safe}")
            print(f"    - {op.source_block.text!r}")
        elif op.kind == OpKind.INSERT:
            print(f"  INSERT  @ {loc}{safe}  (detected; see patch_report.txt)")
            print(f"    + {op.updated_text!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
