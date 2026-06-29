"""Run the project's Schematron rules against patched DITA files.

Uses lxml's built-in ISO Schematron support (XSLT 1 query binding). The
caller passes a directory containing .sch files and a list of DITA files
to validate; we return a flat list of ValidationIssue records describing
every assertion that fired.

If no .sch file is found, or if lxml isn't importable, validation is a
graceful no-op (returns empty list) — the rest of the pipeline still
runs without it. Honest about its limits: lxml only does XSLT 1, so
rules requiring XPath 2 (matches/lower-case/ends-with) must be expressed
with translate/contains/substring or dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

try:
    from lxml import etree, isoschematron
    _HAS_LXML = True
except ImportError:  # lxml not installed
    _HAS_LXML = False


@dataclass
class ValidationIssue:
    rule_id: str          # e.g. "IM_note09"
    role: str             # "error" | "warning" | "" if unspecified
    message: str          # the rule's human-readable text
    location: str         # XPath to the offending element (from SVRL)
    topic_file: str       # path-relative filename of the .dita that failed
    test_expr: str = ""   # the XPath expression that fired (best-effort)


@dataclass
class ValidationReport:
    issues: List[ValidationIssue]
    sch_path: Optional[Path]            # which .sch was used (None if validation skipped)
    skipped_reason: Optional[str] = None  # populated when validation didn't run

    @property
    def by_severity(self) -> dict:
        out: dict = {"error": 0, "warning": 0, "info": 0}
        for i in self.issues:
            key = i.role.lower() if i.role else "info"
            out[key] = out.get(key, 0) + 1
        return out


_SVRL_NS = {"svrl": "http://purl.oclc.org/dsdl/svrl"}


def find_schematron(schematron_dir: Path) -> Optional[Path]:
    """Return the first *.sch file in the schematron directory, if any."""
    if not schematron_dir.exists():
        return None
    matches = sorted(schematron_dir.glob("*.sch"))
    return matches[0] if matches else None


def validate_files(
    dita_files: List[Path],
    schematron_dir: Path,
) -> ValidationReport:
    if not _HAS_LXML:
        return ValidationReport(
            issues=[],
            sch_path=None,
            skipped_reason="lxml is not installed; pip install lxml to enable validation",
        )

    sch_path = find_schematron(schematron_dir)
    if sch_path is None:
        return ValidationReport(
            issues=[],
            sch_path=None,
            skipped_reason=f"no .sch files found in {schematron_dir}",
        )

    try:
        sch_doc = etree.parse(str(sch_path))
        schematron = isoschematron.Schematron(sch_doc, store_report=True)
    except etree.SchematronParseError as exc:
        return ValidationReport(
            issues=[],
            sch_path=sch_path,
            skipped_reason=f"failed to compile {sch_path.name}: {exc}",
        )

    issues: List[ValidationIssue] = []
    for dita_path in dita_files:
        try:
            doc = etree.parse(str(dita_path))
        except etree.XMLSyntaxError as exc:
            issues.append(
                ValidationIssue(
                    rule_id="(xml-parse)",
                    role="error",
                    message=f"could not parse {dita_path.name}: {exc}",
                    location="",
                    topic_file=dita_path.name,
                )
            )
            continue

        schematron.validate(doc)
        report = schematron.validation_report
        if report is None:
            continue

        # SVRL emits both <failed-assert> (for <assert> rules) and
        # <successful-report> (for <report> rules). Both indicate something
        # the author should look at; merge them into one issue list.
        for tag in ("failed-assert", "successful-report"):
            for el in report.findall(f".//svrl:{tag}", _SVRL_NS):
                issues.append(_svrl_to_issue(el, dita_path.name))

    return ValidationReport(issues=issues, sch_path=sch_path)


def _svrl_to_issue(el, topic_file: str) -> ValidationIssue:
    location = el.get("location", "")
    test_expr = el.get("test", "")
    role = (el.get("role") or el.get("flag") or "").lower()

    # The human message lives in a <svrl:text> child.
    text_el = el.find("svrl:text", _SVRL_NS)
    message = "".join(text_el.itertext()).strip() if text_el is not None else ""
    # Squash internal whitespace runs for readability.
    message = " ".join(message.split())

    # Best-effort rule id extraction: messages start with "<rule_id>: ..."
    rule_id = ""
    if ":" in message[:40]:
        head = message.split(":", 1)[0].strip()
        # Heuristic: rule ids look like IM_xxx, IM-style, or similar
        if head and " " not in head and len(head) < 30:
            rule_id = head

    return ValidationIssue(
        rule_id=rule_id,
        role=role,
        message=message,
        location=location,
        topic_file=topic_file,
        test_expr=test_expr,
    )
