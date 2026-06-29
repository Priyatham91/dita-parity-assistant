"""Render a human-readable patch_report.txt from a PatchReport.

Three sections:
  - APPLIED:  the patch engine successfully changed the XML
  - SKIPPED:  the patch engine refused for a safety / structural reason
  - DETECTED: the diff engine found a change but the patch engine doesn't
              yet auto-apply this op kind (currently only INSERT)
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from app.diff_engine import OpKind
from app.patch_engine import PatchReport, PatchResult
from app.schematron_validator import ValidationReport


def write_patch_report(
    report: PatchReport,
    path: Path,
    validation: Optional[ValidationReport] = None,
    dry_run: bool = False,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    applied = report.applied
    skipped = report.skipped
    detected = report.detected
    map_edits = report.map_edits
    warnings = [r for r in applied if r.warning]

    lines: List[str] = []
    lines.append("=" * 72)
    lines.append("DITA PARITY PATCH REPORT" + ("  (DRY RUN)" if dry_run else ""))
    lines.append("=" * 72)
    if dry_run:
        lines.append("DRY RUN — no .dita files were written. The list below shows")
        lines.append("the files that WOULD be written on a real run.")
        lines.append("")
    lines.append(f"Total ops processed:    {len(report.results)}")
    lines.append(f"Applied:                {len(applied)}")
    lines.append(f"Skipped (safety):       {len(skipped)}")
    lines.append(f"Detected (manual):      {len(detected)}")
    lines.append(f"Map edits (reltable):   {len(map_edits)}")
    lines.append(f"Applied with warnings:  {len(warnings)}")
    files_label = "Files that would be written" if dry_run else "Files written"
    lines.append(f"{files_label}:          {len(report.files_written)}")
    for f in report.files_written:
        lines.append(f"  - {f}")
    lines.append("")

    if applied:
        lines.append("-" * 72)
        lines.append("APPLIED  (auto-patched into output/)")
        lines.append("-" * 72)
        for i, r in enumerate(applied, 1):
            _write_entry(lines, i, r, header="APPLIED")

    if skipped:
        lines.append("-" * 72)
        lines.append("SKIPPED  (auto-patch refused for safety / structural reasons)")
        lines.append("-" * 72)
        for i, r in enumerate(skipped, 1):
            _write_entry(lines, i, r, header="SKIPPED")

    if detected:
        lines.append("-" * 72)
        lines.append("DETECTED  (change found but op kind not yet auto-applied)")
        lines.append("-" * 72)
        for i, r in enumerate(detected, 1):
            _write_entry(lines, i, r, header="DETECTED")

    if map_edits:
        lines.append("-" * 72)
        lines.append("MAP EDITS  (content belongs in the .ditamap reltable, not topic bodies)")
        lines.append("-" * 72)
        for i, r in enumerate(map_edits, 1):
            _write_entry(lines, i, r, header="MAP EDIT")

    if validation is not None:
        lines.append("-" * 72)
        lines.append("SCHEMATRON VALIDATION")
        lines.append("-" * 72)
        if validation.skipped_reason:
            lines.append(f"Skipped: {validation.skipped_reason}")
        elif not validation.issues:
            sch_name = validation.sch_path.name if validation.sch_path else "(none)"
            lines.append(f"Ran against {sch_name}: no issues on patched files.")
        else:
            sch_name = validation.sch_path.name if validation.sch_path else "(none)"
            counts = validation.by_severity
            lines.append(f"Ran against {sch_name}: {len(validation.issues)} issues "
                         f"({counts.get('error', 0)} error, "
                         f"{counts.get('warning', 0)} warning, "
                         f"{counts.get('info', 0)} info)")
            lines.append("")
            for i, issue in enumerate(validation.issues, 1):
                lines.append(f"[VALIDATION #{i}]")
                lines.append(f"  rule:        {issue.rule_id or '(rule)'}")
                lines.append(f"  severity:    {issue.role.upper() or 'INFO'}")
                lines.append(f"  topic file:  {issue.topic_file}")
                lines.append(f"  location:    {issue.location}")
                lines.append(f"  message:     {issue.message}")
                lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_entry(lines: List[str], i: int, r: PatchResult, header: str) -> None:
    op = r.op
    lines.append(f"[{header} #{i}]")
    if op is None:
        # Run-level advisory (e.g. "verify embedded media"). No diff op
        # to describe — just the reason explains the action.
        lines.append("  operation:   ADVISORY")
    else:
        lines.append(f"  operation:   {op.kind.value.upper()}")
        if op.kind == OpKind.INSERT:
            anchor = op.anchor_block
            if anchor is not None:
                lines.append(f"  anchor:      after {anchor.topic_id}::{anchor.element_xpath}")
            else:
                lines.append("  anchor:      <publication start>")
            lines.append(f"  new text:    {op.updated_text!r}")
        else:
            block = op.source_block
            lines.append(f"  topic file:  {block.topic_id}")
            lines.append(f"  xpath:       {block.element_xpath}")
            lines.append(f"  old text:    {block.text!r}")
            if op.updated_text is not None:
                lines.append(f"  new text:    {op.updated_text!r}")

    if r.reason:
        lines.append(f"  reason:      {r.reason}")
    if r.warning:
        lines.append(f"  warning:     {r.warning}")
    if r.code_snippet:
        lines.append("  snippet:")
        for snippet_line in r.code_snippet.splitlines():
            lines.append(f"    {snippet_line}")
    lines.append("")
