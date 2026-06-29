"""Apply diff ops to DITA topic files.

Supported now:
  - REPLACE: rewrite the text of the owning element identified by xpath.
  - DELETE:  remove the owning element from its parent.

Not yet applied (recorded as DETECTED in the report):
  - INSERT:  the anchor strategy for inserting new content into the right
             topic needs a design call before this is safe to automate.
             The op is surfaced in the report so a human can place it.

Safety rules:
  - Skip ops where safe_to_apply=False (manual-review zones, structural
    elements like <dlentry> that REPLACE/DELETE would damage incorrectly).
  - Walk the positional xpath the reconstructor recorded; no fuzzy fallback.
  - If the target element had inline child markup, REPLACE drops it and
    records a warning ("inline markup discarded").
  - DOCTYPE, XML declaration, attributes, ids, sibling order, and untouched
    elements are all preserved on round-trip.
"""

from __future__ import annotations

import html as html_lib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace as _dc_replace
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.diff_engine import DiffOp, OpKind
from app.map_parser import ReltableEntry
from app.publication_reconstructor import (
    MANUAL_REVIEW_TITLES,
    Publication,
    build_topic_to_section,
    normalize_for_match,
)


class ResultCategory(str, Enum):
    APPLIED = "applied"      # successfully changed the XML
    SKIPPED = "skipped"      # tried but refused for a safety/structural reason
    DETECTED = "detected"    # op kind not yet auto-applied; reported for review
    MAP_EDIT = "map_edit"    # belongs in the .ditamap (reltable), not a topic body


@dataclass
class PatchResult:
    # `op` is None for run-level advisories (media verification, etc.)
    # — those aren't tied to a specific diff op but still belong in the
    # writer's review queue.
    op: Optional[DiffOp]
    category: ResultCategory
    reason: str = ""           # populated for SKIPPED / DETECTED / MAP_EDIT
    warning: str = ""          # populated for APPLIED with caveats
    code_snippet: str = ""     # copy-pasteable XML for MAP_EDIT entries
    # Optional topic association for op=None advisories. The HTML
    # report uses this to file the card under a specific topic's
    # section instead of the global / map-level area.
    topic_id: Optional[str] = None


@dataclass
class PatchReport:
    results: List[PatchResult] = field(default_factory=list)
    files_written: List[Path] = field(default_factory=list)

    @property
    def applied(self) -> List[PatchResult]:
        return [r for r in self.results if r.category == ResultCategory.APPLIED]

    @property
    def skipped(self) -> List[PatchResult]:
        return [r for r in self.results if r.category == ResultCategory.SKIPPED]

    @property
    def detected(self) -> List[PatchResult]:
        return [r for r in self.results if r.category == ResultCategory.DETECTED]

    @property
    def map_edits(self) -> List[PatchResult]:
        return [r for r in self.results if r.category == ResultCategory.MAP_EDIT]


def _element_flat_text(elem) -> str:
    """Concatenate an element's own text and all descendants' text/tails."""
    parts: List[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        parts.append(_element_flat_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _replace_preserving_inline_markup(
    element, new_text: str, emphasis: Optional[List[str]] = None,
) -> Tuple[bool, int]:
    """Attempt to apply a REPLACE while keeping `element`'s inline
    children intact and, optionally, adding <em> wrappers around new
    emphasis phrases the article highlighted.

    Returns (success, em_added). `em_added` counts how many new <em>
    elements were created so the caller can flag them in the report
    for manual semantic review (a phrase wrapped <em> might more
    accurately be <uicontrol>/<wintitle>/<keyword>).

    Algorithm:
      1. Locate each existing child's flat text in `new_text` (in
         order, case-sensitive). If any child's phrase isn't found,
         abort — caller falls back to the skip-with-DETECTED path.
      2. Locate each emphasis phrase in `new_text`. If a phrase
         overlaps an existing child's region, skip it (the child's
         tag wins). Otherwise queue it as a new <em> wrap.
      3. Sort all spans by start position. If any two overlap, abort.
      4. Clear the element and rebuild text + children in order,
         re-using the preserved children and creating fresh <em>
         elements for the wrap spans.
    """
    children = list(element)
    if not children and not emphasis:
        return False, 0

    # (start, end, kind, payload) where kind ∈ {"preserve", "em"}
    spans: List[tuple] = []
    cursor = 0
    for child in children:
        phrase = _element_flat_text(child)
        if not phrase:
            return False, 0
        idx = new_text.find(phrase, cursor)
        if idx < 0:
            return False, 0
        spans.append((idx, idx + len(phrase), "preserve", child))
        cursor = idx + len(phrase)

    def _overlaps(start: int, end: int) -> bool:
        for s_start, s_end, _, _ in spans:
            if start < s_end and s_start < end:
                return True
        return False

    if emphasis:
        for phrase in emphasis:
            if not phrase:
                continue
            search_from = 0
            while True:
                idx = new_text.find(phrase, search_from)
                if idx < 0:
                    break
                end = idx + len(phrase)
                if not _overlaps(idx, end):
                    spans.append((idx, end, "em", phrase))
                    break
                search_from = idx + 1

    spans.sort(key=lambda s: s[0])

    # Final overlap check after insertions.
    for i in range(len(spans) - 1):
        if spans[i][1] > spans[i + 1][0]:
            return False, 0

    # If nothing matched at all, there's nothing for this helper to do —
    # let the caller fall back.
    if not spans:
        return False, 0

    # Detach existing children before re-appending them in new order.
    for child in children:
        element.remove(child)
    element.text = None

    last_inserted = None
    cursor = 0
    em_added = 0
    for start, end, kind, payload in spans:
        text_before = new_text[cursor:start]
        if last_inserted is None:
            element.text = (element.text or "") + text_before if text_before else element.text
        else:
            last_inserted.tail = (last_inserted.tail or "") + text_before if text_before else last_inserted.tail

        if kind == "preserve":
            payload.tail = None
            element.append(payload)
            last_inserted = payload
        else:  # "em"
            em_el = ET.SubElement(element, "em")
            em_el.text = payload
            last_inserted = em_el
            em_added += 1
        cursor = end

    trailing = new_text[cursor:]
    if trailing:
        if last_inserted is None:
            element.text = trailing
        else:
            last_inserted.tail = trailing
    return True, em_added


_MEDIA_OR_LINK_TAGS = {"xref", "image", "object", "fig"}

# Media tags whose presence in a soon-to-be-deleted block is genuinely
# dangerous: the article HTML parser does not capture embedded
# pictures/videos, so a DELETE here would silently strip them. Xrefs
# are intentionally NOT in this set — an <xref> renders as its link
# text in the article HTML and IS captured by the parser, so a
# paragraph-with-xref is safe to delete when its text doesn't appear
# in the article.
_MEDIA_TAGS = {"image", "object", "fig"}


def _has_media_or_link_children(elem) -> bool:
    """True if `elem` (or any descendant) contains a tag we treat as
    'do not silently delete' content — cross-references and embedded
    media. Used by the REPLACE guard (where we want to preserve link
    attributes through a content rewrite).
    """
    for descendant in elem.iter():
        if descendant is elem:
            continue
        if _local(descendant.tag) in _MEDIA_OR_LINK_TAGS:
            return True
    return False


def _has_media_children(elem) -> bool:
    """True if `elem` (or any descendant) contains an embedded media tag
    (`image`, `object`, `fig`). Used by the DELETE guard — xrefs are
    excluded because their link text round-trips through the article
    parser.
    """
    for descendant in elem.iter():
        if descendant is elem:
            continue
        if _local(descendant.tag) in _MEDIA_TAGS:
            return True
    return False


def _normalize_href_for_compare(href: str) -> str:
    """Lower-case + strip trailing slash + drop scheme/host. So
    "https://help.example.com/answer/87951" and
    "/answer/87951/" compare equal — only the path identifier
    carries equality semantics.

    NOTE: distinct from the `_normalize_href` defined later in this
    file, which absolutizes Help Center relative URLs for outgoing
    DITA xrefs. The two share a prefix in intent but live at opposite
    ends of the round-trip — keep them separate.
    """
    if not href:
        return ""
    h = href.strip().lower()
    if "://" in h:
        h = h.split("://", 1)[1]
        if "/" in h:
            h = h[h.index("/"):]
        else:
            h = "/" + h
    for sep in ("?", "#"):
        if sep in h:
            h = h.split(sep, 1)[0]
    if h.endswith("/") and len(h) > 1:
        h = h[:-1]
    return h


def _hrefs_equivalent(
    src: Tuple[str, ...], art: Tuple[str, ...],
) -> bool:
    """True when two ordered href lists match after normalization. A
    different length, a missing one side, or different paths after
    normalization means NOT equivalent."""
    if len(src) != len(art):
        return False
    return all(
        _normalize_href_for_compare(s) == _normalize_href_for_compare(a)
        for s, a in zip(src, art)
    )


def _xref_hrefs(elem) -> Tuple[str, ...]:
    """Return the hrefs of every <xref> descendant of `elem` in
    document order. Empty when there are none."""
    hrefs: List[str] = []
    for descendant in elem.iter():
        if descendant is elem:
            continue
        if _local(descendant.tag) == "xref":
            href = descendant.attrib.get("href")
            if href:
                hrefs.append(href)
    return tuple(hrefs)


def _ambiguous_xref_sibling_delete(child, root) -> bool:
    """True when `child` contains <xref>(s) AND there's another
    same-tag element anywhere in this topic root with identical
    visible text but different <xref href> values. The duplicate
    doesn't have to be a direct sibling — in real DITA the two
    occurrences are often in separate `<ul>` parents under the same
    `<conbody>` (e.g. one ul for "U.S. members", another for
    "Non-U.S. members", both containing an `<li>` with the same
    link text). In that case the LCS pairing in the diff was
    effectively arbitrary; the tool can't safely DELETE either
    occurrence without writer review.

    Same-tag scoping prevents false positives from `<xref>` text
    appearing both inside an `<li>` and inside an outer `<p>` —
    those are different structural elements, not duplicates."""
    child_hrefs = _xref_hrefs(child)
    if not child_hrefs:
        return False
    child_text_norm = normalize_for_match(_element_text_for_compare(child))
    if not child_text_norm:
        return False
    child_local = _local(child.tag)
    child_descendants = {id(d) for d in child.iter()}
    for other in root.iter():
        if other is child:
            continue
        if id(other) in child_descendants:
            continue
        if _local(other.tag) != child_local:
            continue
        other_hrefs = _xref_hrefs(other)
        if not other_hrefs or other_hrefs == child_hrefs:
            continue
        other_text_norm = normalize_for_match(
            _element_text_for_compare(other)
        )
        if other_text_norm == child_text_norm:
            return True
    return False


# Cap the suppression to short label-like spans. A filename or
# stylesheet artifact is typically under ~50 chars; a full sentence
# removal (the bug we're guarding against) is usually much longer.
_NO_REAL_CHANGE_MAX_EXTRA = 50


def _apply_cell_aware_row_replace(
    row_elem,
    article_cells,
    cell_emphasis_per_cell,
) -> Optional[Tuple[int, str]]:
    """Apply a row-level REPLACE at the cell granularity.

    Returns (cells_changed, warning_text) on success, or None when the
    row can't be matched cleanly (e.g. cell counts differ).

    For each <entry> child of the source row, compare the flattened
    text against the corresponding article-side cell. If they're
    already equal (after normalization), leave the entry untouched —
    preserves inline markup like <uicontrol>, <xref>, <wintitle>. If
    they differ, rewrite that entry's text using the markup-preserving
    helper if the source had inline children, or as plain text
    otherwise. If the article side added bold/italic phrases for that
    cell, wrap them in <em>.
    """
    entries = [c for c in row_elem if _local(c.tag) == "entry"]
    # Cell counts must match — otherwise we can't trust positional
    # alignment.
    if len(entries) != len(article_cells):
        return None

    cells_changed = 0
    cells_skipped_for_structure = 0
    em_added_total = 0
    inline_markup_dropped = False

    # Block-level tags inside a cell mean the cell has real structure
    # (a note, a bullet list, a code sample, etc.). We don't try to
    # auto-update those — too easy to silently destroy a <note> or
    # rearrange a <ul>. Surface them in the warning so the writer
    # can apply the change by hand.
    _CELL_BLOCK_TAGS = (
        "note", "ul", "ol", "dl", "p", "fig", "codeblock", "table",
    )

    for i, entry in enumerate(entries):
        article_text = article_cells[i] or ""
        source_text = _element_text_for_compare(entry)
        if normalize_for_match(source_text) == normalize_for_match(article_text):
            continue
        # Block-level child present? Skip this cell for safety.
        has_block_children = any(
            _local(c.tag) in _CELL_BLOCK_TAGS
            for c in entry.iter()
            if c is not entry
        )
        if has_block_children:
            cells_skipped_for_structure += 1
            continue
        # Cell is plain text + inline markup — safe to rewrite.
        cell_emphasis = None
        if cell_emphasis_per_cell and i < len(cell_emphasis_per_cell):
            cell_emphasis = cell_emphasis_per_cell[i] or None
        had_children = len(list(entry)) > 0
        if had_children:
            ok, em_added = _replace_preserving_inline_markup(
                entry, article_text, cell_emphasis,
            )
            if ok:
                em_added_total += em_added or 0
            else:
                # Markup couldn't be preserved (phrases gone) — drop it.
                _replace_with_inline(entry, article_text, links=None)
                inline_markup_dropped = True
        else:
            _replace_with_inline(entry, article_text, links=None)
            if cell_emphasis:
                ok, em_added = _replace_preserving_inline_markup(
                    entry, article_text, cell_emphasis,
                )
                if ok:
                    em_added_total += em_added or 0
        cells_changed += 1

    # If we skipped any cells because they had block structure (notes,
    # bullets, etc.) AND didn't change any other cells, surface the
    # row as needs-review rather than silently calling it applied.
    if cells_changed == 0 and cells_skipped_for_structure > 0:
        return None

    if cells_changed == 0:
        # Source and article fully matched at the cell level — diff
        # opcode was noise (e.g. whitespace flattening). Treat as a
        # successful no-op so the writer's "needs review" list stays
        # short. (Caller will record this as APPLIED with a warning.)
        return 0, (
            "row matched the article side at the cell level after "
            "normalization — no DITA changes needed."
        )

    parts = [
        f"rewrote {cells_changed} cell{'s' if cells_changed != 1 else ''} "
        "in this <row> (other cells left untouched)"
    ]
    if cells_skipped_for_structure:
        parts.append(
            f"left {cells_skipped_for_structure} cell"
            f"{'s' if cells_skipped_for_structure != 1 else ''} alone because "
            "they contain block structure (<note>, <ul>, etc.) — review "
            "those by hand if the article changed them"
        )
    if em_added_total:
        parts.append(
            f"added {em_added_total} <em> wrap{'s' if em_added_total != 1 else ''} "
            "for article-side bold/italic — VERIFY each <em>: it may be more "
            "accurate as <uicontrol> / <wintitle> / <keyword>"
        )
    if inline_markup_dropped:
        parts.append(
            "dropped inline markup in at least one cell because the "
            "article wording no longer contains the wrapped phrase"
        )
    return cells_changed, " | ".join(parts)


# Separators the Help Center stylesheet (and writers) may use between
# Term and Definition in a rendered <dlentry>. Writers don't all follow
# the IM's colon rule — they sometimes use a hyphen or an en-dash. We
# accept any of them when splitting article text into term/definition.
_DLENTRY_SEPARATOR_RE = re.compile(r"\s*[:\-–—]\s+")

# Block-level child tags whose presence in a <dd> rules out an
# auto-applied dlentry REPLACE: those children carry structure (a
# nested list, a callout, a code sample) that a text-only rewrite
# would silently destroy.
_DD_BLOCK_TAGS = (
    "p", "ul", "ol", "dl", "note", "table", "fig", "codeblock",
)


def _is_simple_dlentry(dlentry_elem) -> bool:
    """True when a <dlentry> has the shape the auto-REPLACE handler
    can rewrite safely: exactly one <dt> and one <dd>, with <dd>
    containing only text + inline markup (no nested blocks)."""
    dts = [c for c in dlentry_elem if _local(c.tag) == "dt"]
    dds = [c for c in dlentry_elem if _local(c.tag) == "dd"]
    if len(dts) != 1 or len(dds) != 1:
        return False
    dd = dds[0]
    for descendant in dd.iter():
        if descendant is dd:
            continue
        if _local(descendant.tag) in _DD_BLOCK_TAGS:
            return False
    return True


def _apply_dlentry_replace(
    dlentry_elem,
    article_text: str,
) -> Optional[Tuple[bool, str]]:
    """Apply a REPLACE on a <dlentry> at the <dd> granularity.

    Returns (inline_markup_dropped, warning_text) on success, or None
    when the dlentry can't be rewritten safely.

    Strategy:
      1. Refuse non-simple dlentries (≠ 1 <dt> or ≠ 1 <dd>; <dd> has
         block children).
      2. Split `article_text` on the first separator (`:`, `-`, `–`,
         or `—`). Refuse if there is no separator.
      3. Compare the article-side term against the existing <dt> text
         after normalization. If they differ, refuse — a renamed term
         is meaningful and may break cross-references; surface it.
      4. Rewrite the <dd>'s definition. If the existing <dd> had inline
         markup (<uicontrol>, <xref>, <b>, …) AND each child's text
         still appears in the new definition in the same order, keep
         the children and re-position them. Otherwise rewrite the <dd>
         as plain text and flag that inline markup was dropped.
      5. Leave <dt> untouched.
    """
    if not _is_simple_dlentry(dlentry_elem):
        return None

    dt = next(c for c in dlentry_elem if _local(c.tag) == "dt")
    dd = next(c for c in dlentry_elem if _local(c.tag) == "dd")

    sep_match = _DLENTRY_SEPARATOR_RE.search(article_text)
    if not sep_match:
        return None
    article_term = article_text[:sep_match.start()].strip()
    article_def = article_text[sep_match.end():].strip()
    if not article_term or not article_def:
        return None

    dt_text = "".join(dt.itertext()).strip()
    if normalize_for_match(dt_text) != normalize_for_match(article_term):
        # Term itself changed (more than just separator format).
        # Surface for review rather than rewriting the dd in place.
        return None

    # If the definition didn't actually change, no-op.
    dd_text_current = "".join(dd.itertext()).strip()
    if normalize_for_match(dd_text_current) == normalize_for_match(article_def):
        return (False, "")

    inline_children = [c for c in dd if isinstance(c.tag, str)]

    # Plain-text dd: rewrite directly.
    if not inline_children:
        dd.text = article_def
        # No children to clear (already empty).
        return (False, "")

    # Inline-markup dd: try to preserve children by locating each
    # child's text in the new definition, in order. If any child's
    # phrase isn't found, fall back to plain-text rewrite and report
    # that inline markup was dropped.
    positions: List[Tuple[int, ET.Element, str]] = []
    cursor = 0
    preserved_ok = True
    for child in inline_children:
        child_text = "".join(child.itertext())
        if not child_text:
            preserved_ok = False
            break
        idx = article_def.find(child_text, cursor)
        if idx < 0:
            preserved_ok = False
            break
        positions.append((idx, child, child_text))
        cursor = idx + len(child_text)

    if not preserved_ok:
        # Drop inline markup and rewrite as plain text.
        for child in list(dd):
            dd.remove(child)
        dd.text = article_def
        return (
            True,
            "dropped inline markup in <dd> because the article wording "
            "no longer contains one of the wrapped phrases — verify "
            "whether the missing <uicontrol>/<xref> should be re-added",
        )

    # All inline children located in the new definition: reposition
    # them. The <dd>'s direct text becomes the prefix before the first
    # child; each child's tail becomes the text up to the next child
    # (or to the end of the definition for the last child).
    dd.text = article_def[:positions[0][0]] or None
    for i, (idx, child, ctext) in enumerate(positions):
        next_start = (
            positions[i + 1][0]
            if i + 1 < len(positions)
            else len(article_def)
        )
        tail = article_def[idx + len(ctext):next_start]
        child.tail = tail or None

    return (False, "")


def _element_text_for_compare(elem) -> str:
    """Flatten an element's text content (including descendant text)
    for comparison against an article-side cell text."""
    parts = [elem.text or ""]
    for child in elem.iter():
        if child is elem:
            continue
        parts.append(child.text or "")
        parts.append(child.tail or "")
    return "".join(parts)


def _is_no_real_change(op: DiffOp) -> bool:
    """True when a REPLACE op's only difference is a SHORT label-like
    PREFIX that the article-side rendering omits but the DITA carries
    as context.

    Common case: a multi-paragraph callout in the DITA carries a
    filename or stylesheet artifact at the START of its text — e.g.
    "Guide.pdf Please review this document…" — that the article
    HTML doesn't render visibly. The visible wording is identical;
    the diff opcode is noise.

    Strict suffix match only. Tightened 2026-06-25 (Copilot CLI code
    review): the previous "article is a substring anywhere in source"
    check silently dropped genuine sentence truncations. Example
    Copilot caught:
        source  = "Click the Save button and then close the window."
        article = "Click the Save button"
    The article TRUNCATED the second clause; 28 chars are missing.
    The old substring-anywhere logic suppressed this (it was under
    the size cap) and the writer saw no card. The article's deletion
    silently went unflagged.

    Suffix match only (extra is at the START of source) catches the
    intended "label prefix" pattern without catching end-truncations
    of the article. Same logic in reverse for article-longer-than-
    source (extra is at the START of article — a label-like prefix
    the article added that the DITA never had)."""
    if op.source_block is None or not op.updated_text:
        return False
    source_norm = normalize_for_match(op.source_block.text or "").lower()
    article_norm = normalize_for_match(op.updated_text or "").lower()
    if not source_norm or not article_norm:
        return False
    # Article is a SUFFIX of source → extra (label-like) lives at the
    # START of source.
    if source_norm.endswith(article_norm) and source_norm != article_norm:
        extra = len(source_norm) - len(article_norm)
        return extra <= _NO_REAL_CHANGE_MAX_EXTRA
    # Source is a SUFFIX of article → extra (label-like) lives at the
    # START of article.
    if article_norm.endswith(source_norm) and source_norm != article_norm:
        extra = len(article_norm) - len(source_norm)
        return extra <= _NO_REAL_CHANGE_MAX_EXTRA
    return False


def _redirect_navtitle_anchors(
    ops, publication, article_blocks, topic_to_section,
):
    """Rewrite INSERT ops anchored at a <ditamap> navtitle so they
    target the first source block of the topic that navtitle labels.

    The diff engine uses last_source as the INSERT anchor. When an
    article block (e.g., a stem-sentence heading at the top of a
    panel) aligns right after the matched navtitle, the resulting
    op's anchor is in <ditamap> — not in any .dita file. The safety
    guard would skip the INSERT and the writer never sees the
    heading land where it should.

    For each such op we look up the target topic via the article
    block's section_id, find that topic's first source block, and
    replace anchor_block. The downstream handler (e.g. heading →
    stepsection at start of <steps>) does the right thing once the
    anchor is inside a real topic.
    """
    if not topic_to_section or publication is None or not article_blocks:
        return ops

    import dataclasses
    section_to_topic = {sec: tid for tid, sec in topic_to_section.items()}

    # First source block per topic — used as the redirect anchor.
    first_block_per_topic = {}
    for blk in publication.blocks:
        if blk.topic_id == "<ditamap>":
            continue
        if blk.topic_id not in first_block_per_topic:
            first_block_per_topic[blk.topic_id] = blk

    out = []
    for op in ops:
        if (
            op.kind == OpKind.INSERT
            and op.anchor_block is not None
            and op.anchor_block.topic_id == "<ditamap>"
            and op.updated_index is not None
            and op.updated_index < len(article_blocks)
        ):
            article_block = article_blocks[op.updated_index]
            section = getattr(article_block, "section_id", None)
            target_topic_id = section_to_topic.get(section) if section else None
            target_anchor = (
                first_block_per_topic.get(target_topic_id)
                if target_topic_id
                else None
            )
            if target_anchor is not None:
                op = dataclasses.replace(
                    op,
                    anchor_block=target_anchor,
                    safe_to_apply=target_anchor.auto_update,
                )
        out.append(op)
    return out


def _record_replace_skip(
    report: PatchReport, op: DiffOp, reason: str,
) -> None:
    """Record a SKIPPED for a REPLACE and, if the op carries a new text
    that would otherwise be lost, also surface a DETECTED entry so the
    writer can place the new content manually.

    Without this, a REPLACE refused for safety reasons silently consumes
    the article-side block — the new text never reappears anywhere in the
    report.
    """
    report.results.append(
        PatchResult(op=op, category=ResultCategory.SKIPPED, reason=reason)
    )
    if op.kind == OpKind.REPLACE and (op.updated_text or "").strip():
        loc = ""
        if op.source_block is not None:
            loc = f" near {op.source_block.topic_id}::{op.source_block.element_xpath}"
        report.results.append(
            PatchResult(
                op=op,
                category=ResultCategory.DETECTED,
                reason=(
                    "the original REPLACE was refused for safety reasons "
                    "(see the SKIPPED entry above); the new article text is "
                    "surfaced here so it isn't lost — place it manually"
                    + loc
                ),
            )
        )


_MEDIA_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "wistia.com")


def _surface_media_for_verification(
    report: PatchReport,
    publication: Optional[Publication],
) -> None:
    """Emit one MAP_EDIT-style entry PER topic that contains embedded
    media (videos, images, linked assets, <codeblock>s).

    The article HTML parser doesn't capture iframes, pictures, or
    video embeds reliably, so we never try to auto-diff media — it's
    always the writer's responsibility to confirm each one against the
    live article. Surfacing this per-topic (rather than as one
    consolidated map-level entry) lets the writer focus on the
    specific topic that has the media, and the report renders it under
    that topic's section.
    """
    if publication is None or not publication.blocks:
        return

    seen_paths = set()
    for blk in publication.blocks:
        if blk.topic_path in seen_paths:
            continue
        seen_paths.add(blk.topic_path)
        if not blk.topic_path or not blk.topic_path.exists():
            continue
        try:
            tree = ET.parse(str(blk.topic_path))
        except Exception:  # noqa: BLE001
            continue
        if not _has_media_anywhere(tree.getroot()):
            continue
        report.results.append(
            PatchResult(
                op=None,
                category=ResultCategory.MAP_EDIT,
                topic_id=blk.topic_id,
                reason=(
                    "Verify embedded media and code blocks in this topic "
                    "manually. It contains images, videos, linked assets, "
                    "or <codeblock> samples that the tool can't reliably "
                    "diff (the article HTML renders them as "
                    "iframes/pictures/<pre>, not as DITA elements). "
                    "Confirm each one against the live article."
                ),
            )
        )


_OPAQUE_CONTENT_TAGS = ("image", "object", "fig", "codeblock")


def _has_media_anywhere(elem) -> bool:
    """True if any descendant is a content-bearing media element
    (<image>, <object>, <fig>, <codeblock>) or an <xref> pointing at
    a known video host.

    Inline UI icons — `<image>` elements nested inside `<uicontrol>`,
    `<menucascade>`, etc. — don't count. Those are decorative glyphs
    next to a button label ("Click ✱ More"), not standalone media the
    writer needs to verify against the live article. Surfacing them
    fires the advisory on every report that mentions a UI button with
    an icon, which is noise rather than signal.
    """
    # Build a parent map so we can ask "is this image nested inside
    # a uicontrol-like element?" without re-walking each time.
    parent_map = {id(c): p for p in elem.iter() for c in p}

    def _is_decorative_icon(media_elem) -> bool:
        if _local(media_elem.tag) != "image":
            return False
        cur = parent_map.get(id(media_elem))
        while cur is not None:
            if _local(cur.tag) in _INLINE_ICON_PARENT_TAGS:
                return True
            cur = parent_map.get(id(cur))
        return False

    for descendant in elem.iter():
        tag = _local(descendant.tag)
        if tag in _OPAQUE_CONTENT_TAGS:
            if _is_decorative_icon(descendant):
                continue
            return True
        if tag == "xref":
            href = (descendant.get("href") or "").lower()
            if any(host in href for host in _MEDIA_VIDEO_HOSTS):
                return True
    return False


# Tags whose descendant <image>s are decorative icons (button glyphs,
# menu cascades), not content media. Used by _has_media_anywhere to
# silence the verification advisory when the only "media" in a topic
# is a UI icon reference inside a control label.
_INLINE_ICON_PARENT_TAGS = (
    "uicontrol", "menucascade", "wintitle", "term", "keyword",
)


def _surface_orphan_sources(
    report: PatchReport, article_blocks: Optional[List],
) -> None:
    """For each SKIPPED structural REPLACE, check whether the source
    text still appears somewhere in the article. If not, emit a
    DETECTED entry asking the user to verify whether the element
    should be removed.

    Without this, content the article has deleted survives silently in
    the DITA because the safety layer (correctly) refused to mutate the
    structural element.
    """
    if not article_blocks:
        return

    from difflib import SequenceMatcher  # local import, only used here

    article_texts = [
        normalize_for_match(getattr(b, "text", "") or "")
        for b in article_blocks
        if getattr(b, "text", "")
    ]
    if not article_texts:
        return

    seen = set()
    for r in list(report.results):
        if r.category != ResultCategory.SKIPPED:
            continue
        if r.op is None or r.op.kind != OpKind.REPLACE:
            continue
        if r.op.source_block is None:
            continue
        reason = (r.reason or "").lower()
        if "structural element" not in reason:
            continue
        key = (
            r.op.source_block.topic_id,
            r.op.source_block.element_xpath,
        )
        if key in seen:
            continue
        seen.add(key)

        src_text = normalize_for_match(r.op.source_block.text or "")
        if len(src_text) < 20:
            # Too short to evaluate similarity meaningfully.
            continue
        best = 0.0
        for at in article_texts:
            ratio = SequenceMatcher(None, src_text, at).ratio()
            if ratio > best:
                best = ratio
                if best >= 0.7:  # early-exit: clearly present
                    break

        if best < 0.4:
            element_tag = r.op.source_block.element_tag or "element"
            report.results.append(
                PatchResult(
                    op=r.op,
                    category=ResultCategory.DETECTED,
                    reason=(
                        f"the source <{element_tag}> text has no close "
                        f"match in the updated article "
                        f"(best similarity {best:.0%}); the content may "
                        "have been removed or fully rewritten — verify "
                        "and consider deleting this element manually"
                    ),
                )
            )


# The topic→section map was moved to publication_reconstructor.build_topic_to_section
# so the diff engine can use it too (section-aware alignment). This module
# now imports the function instead of duplicating it.


def _article_section_for(
    op: DiffOp, article_blocks: Optional[List],
) -> Optional[str]:
    if not article_blocks or op.updated_index is None:
        return None
    if op.updated_index >= len(article_blocks):
        return None
    return getattr(article_blocks[op.updated_index], "section_id", None)


# Feature-launcher callouts ("Get ahead with a Premium" etc.)
# are product promos that can't be reliably round-tripped: the source
# DITA carries custom @outputclass styling + a launcher URL the article
# HTML rewrites on click. We never auto-modify or auto-insert them.
# But we MAY auto-delete a source feature note when the live article
# no longer renders one — that's the only safe direction.
def _note_rebuild_is_lossless(
    element,
    new_text: str,
    note_bullets,
    links,
    emphasis,
) -> bool:
    """True when a `<note>` REPLACE has nothing to verify.

    Rebuilds carry a "Please verify" warning by default because the
    source's inline markup (`<xref>`, `<uicontrol>`, `<keyword>`, etc.)
    can subtly disappear if the article-side rewording drops a phrase
    we relied on for re-anchoring. But for plain prose → plain prose
    rewordings with matching paragraph counts, there's nothing for the
    writer to verify — the rebuild is text-for-text deterministic.

    Returns False (and the warning fires) when ANY of these are true:
      - article side wants to embed links (`<xref>` rebuild — text the
        rebuild relies on for re-anchoring could change subtly)
      - article side wants emphasis wraps (`<em>` rebuild)
      - source has inline markup (`<xref>`, `<uicontrol>`, `<keyword>`,
        …) inside its `<p>` or `<li>` children — that markup would be
        dropped on rebuild
      - source / article structure don't match (prose vs bullets, or
        paragraph counts differ)

    Beta feedback (2026-06-24): the unconditional warning was making
    every wording rewrite of a plain-prose note look like it needed
    verification, eroding writer trust in the warning."""
    if links or emphasis:
        return False
    # --- Bullets-to-bullets path: <note><ul><li>…</li></ul></note> ---
    if note_bullets:
        ul_children = [c for c in element if _local(c.tag) == "ul"]
        # Source must be exactly one <ul>, nothing else (no orphan <p>
        # next to the list).
        if len(ul_children) != 1 or sum(1 for _c in element) != 1:
            return False
        for li in ul_children[0]:
            if _local(li.tag) != "li":
                return False
            for d in li.iter():
                if d is li:
                    continue
                # Inline markup inside an <li> — rebuild can't preserve.
                return False
        return True
    # --- Prose-to-prose path: <note><p>…</p></note> ---
    source_paras = [c for c in element if _local(c.tag) == "p"]
    if len(source_paras) != sum(1 for _c in element):
        return False
    if not source_paras:
        return False
    for p in source_paras:
        for d in p.iter():
            if d is p:
                continue
            return False
    new_paras = [s for s in re.split(r"\n\n+", new_text or "") if s.strip()]
    if not new_paras:
        new_paras = [new_text.strip()] if (new_text or "").strip() else []
    if len(new_paras) != len(source_paras):
        return False
    return True


_FEATURE_NOTE_KINDS = frozenset({"feature", "feature-launcher"})

_FEATURE_NOTE_SKIP_REASON = (
    "feature note (othertype=\"feature\") — please add this feature "
    "note to the topic manually; the tool couldn't apply it."
)


def _is_source_feature_note(block) -> bool:
    """True if a source DITA Block is a feature note. We identify these
    via the skip_reason populated by publication_reconstructor (which
    has already inspected `othertype="feature"`); piggybacking on it
    avoids carrying a duplicate field on Block."""
    if block is None or getattr(block, "element_tag", None) != "note":
        return False
    return "feature note (othertype" in (getattr(block, "skip_reason", "") or "")


def _is_article_feature_launcher(article_block) -> bool:
    """True if a parsed HtmlArticleBlock represents a feature-launcher
    callout from the live article."""
    if article_block is None:
        return False
    if getattr(article_block, "kind", None) != "note":
        return False
    note_kind = getattr(article_block, "note_kind", None) or ""
    return note_kind in _FEATURE_NOTE_KINDS


def _article_has_feature_launcher(article_blocks: Optional[List]) -> bool:
    """True if the article HTML has at least one feature-launcher
    callout. When False, a source DITA feature note can be safely
    deleted — the live article no longer renders the promo."""
    if not article_blocks:
        return False
    return any(_is_article_feature_launcher(ab) for ab in article_blocks)


def _is_cross_section(
    op: DiffOp,
    block,
    article_blocks: Optional[List],
    topic_to_section: Dict[str, str],
    report: PatchReport,
) -> bool:
    """If the article block's tab section disagrees with the DITA
    topic's tab section, append a DETECTED entry and return True.

    The diff engine treats both sides as a flat sequence and has no
    notion of "this article block lives in the Mobile tab" or "this
    DITA file is the Mobile-tab topic." Without this check, alignments
    that span tab boundaries route content into the wrong topic.

    Topics that have no section binding (pre/post-tab content) are
    only restricted from receiving content that DOES live in a panel.
    """
    if not topic_to_section:
        return False
    article_section = _article_section_for(op, article_blocks)
    topic_section = topic_to_section.get(getattr(block, "topic_id", ""))
    if article_section == topic_section:
        return False
    # One side has a section, the other doesn't, or they differ.
    article_label = article_section or "outside any tab"
    topic_label = topic_section or "outside any tab"
    report.results.append(
        PatchResult(
            op=op,
            category=ResultCategory.DETECTED,
            reason=(
                f"cross-tab routing refused: the article block lives in "
                f"{article_label!r} but the target topic '{block.topic_id}' "
                f"corresponds to {topic_label!r}. Place this change in the "
                "correct tab's topic manually."
            ),
        )
    )
    return True


def apply_ops(
    ops: List[DiffOp],
    output_dir: Path,
    article_blocks: Optional[List] = None,
    publication: Optional[Publication] = None,
    dry_run: bool = False,
    reltable_entries: Optional[List[ReltableEntry]] = None,
) -> PatchReport:
    """Apply REPLACE/DELETE/INSERT ops; record map edits and skips.

    `article_blocks` is a parallel list to the article-side block strings
    fed into the diff (i.e. article_blocks[updated_index] is the rich
    HtmlArticleBlock for that string). When present, table-row INSERTs
    can use cells to build a proper <row><entry>...</entry></row>.

    `publication` lets the engine match article tab labels against DITA
    topic titles so it can refuse INSERT/REPLACE ops that would route
    content from one tab into another tab's topic file.

    `dry_run=True` runs every check and produces a full report (including
    APPLIED/SKIPPED/DETECTED categorization) without writing any patched
    `.dita` files to disk. Use it to audit what *would* happen before
    committing to a real run.
    """
    topic_to_section = build_topic_to_section(publication, article_blocks)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    report = PatchReport()

    # Whether the live article still renders a feature-launcher callout.
    # When False, a source DITA feature note can be safely DELETEd
    # (the writer doesn't need to do anything by hand — the article
    # legitimately dropped the promo).
    article_has_feature_launcher = _article_has_feature_launcher(
        article_blocks
    )

    # Redirect INSERT ops anchored at a <ditamap> navtitle to the
    # first source block of the target topic. SequenceMatcher anchors
    # an article block at the most recent matched source block — when
    # the article's first panel-1 block (e.g. a stem sentence) aligns
    # right after the Desktop navtitle, the anchor is in <ditamap>,
    # not in any .dita file. The safety guard would skip the INSERT.
    # Redirecting lets the heading/paragraph/step handler land it
    # inside the target topic.
    ops = _redirect_navtitle_anchors(
        ops, publication, article_blocks, topic_to_section,
    )

    # Mass-deletion guard: only fire when this is genuinely a retired
    # topic, not when the writer is just trimming some content. The
    # earlier 50% blanket was too aggressive — a small topic (8 blocks)
    # that legitimately loses 4 items would falsely look "entirely
    # removed" and stop the deletes from applying.
    #
    # Per-topic signals used by the trigger below:
    #   - delete_counts: how many DELETE ops target each topic.
    #   - title_in_delete: the source <title> text is not in the
    #     article — strong "topic was retired" signal that lets us
    #     fire the advisory at a lower body-removal ratio (≥ 50%).
    #
    # An EQUAL or REPLACEd <title> means the article still renders the
    # topic's heading, so the file itself is alive. In that case rule
    # (a) below can't trigger (title_gone is False), and rule (b)
    # requires ≥ 80% body removal — the bar for flagging an alive-but-
    # heavily-trimmed topic.
    delete_counts: Dict[str, int] = {}
    title_in_delete: set = set()
    for op in ops:
        if op.source_block is None:
            continue
        if (
            op.source_block.element_tag == "title"
            and op.kind == OpKind.DELETE
        ):
            title_in_delete.add(op.source_block.topic_id)
        if op.kind == OpKind.DELETE:
            tid = op.source_block.topic_id
            delete_counts[tid] = delete_counts.get(tid, 0) + 1
    blocks_per_topic: Dict[str, int] = {}
    if publication is not None:
        for b in publication.blocks:
            blocks_per_topic[b.topic_id] = blocks_per_topic.get(b.topic_id, 0) + 1
    mass_delete_topics: set = set()
    for tid, dc in delete_counts.items():
        total = blocks_per_topic.get(tid, 0)
        if total == 0 or dc < 3:
            continue
        ratio = dc / total
        title_gone = tid in title_in_delete
        # Rule (a): title-gone signal + ≥ 50% body removal.
        # Rule (b): ≥ 80% body removal alone (defensive — fires even
        # when the title is renamed-but-alive, since that's an unusual
        # combination worth flagging).
        # A title that's REPLACE/EQUAL (alive) and < 80% body removal
        # is normal editing — Report 1's case — and falls through to
        # "no advisory, deletes apply normally."
        if (title_gone and ratio >= 0.5) or ratio >= 0.8:
            mass_delete_topics.add(tid)

    # Emit ONE clear advisory per mass-delete topic — the "entire topic
    # was removed from the live article" case. No per-block SKIPPED
    # entries are emitted alongside it (see the per-op loop below); the
    # advisory carries the count and the action by itself.
    for tid in sorted(mass_delete_topics):
        dc = delete_counts.get(tid, 0)
        total = blocks_per_topic.get(tid, 0)
        ratio_pct = round(dc / total * 100) if total else 0
        title_gone = tid in title_in_delete
        signal = (
            "the topic title is gone from the live article"
            if title_gone
            else f"{ratio_pct}% of blocks are missing"
        )
        report.results.append(
            PatchResult(
                op=None,
                category=ResultCategory.DETECTED,
                reason=(
                    f"Entire topic appears removed from the live article "
                    f"({signal}): {dc} of {total} content blocks in "
                    f"'{tid}' are missing. If the article was retired, "
                    "delete this topic file AND remove its <topicref> "
                    "from the .ditamap. The individual block-level "
                    "DELETEs are suppressed below to keep this report "
                    "focused on the single topic-level action you need "
                    "to take."
                ),
                topic_id=tid,
            )
        )

    # Stale-href advisories. For every EQUAL pairing where the source
    # block carries one or more <xref href>s, compare them against the
    # article-side block's link hrefs. When they don't match, the DITA
    # href is most likely outdated — this is common after an
    # article-ID format change (e.g. "/87951" → "/a1342713"). The tool
    # never auto-rewrites hrefs (the new target may be a different
    # document, not the same one renamed) but it does surface a clear
    # "verify this link" advisory per affected topic so the writer can
    # update the DITA hrefs by hand.
    if article_blocks is not None:
        stale_per_topic: Dict[str, List[Tuple[str, Tuple[str, ...], Tuple[str, ...]]]] = {}
        for op in ops:
            if op.kind != OpKind.EQUAL:
                continue
            if op.source_block is None or not op.source_block.xref_hrefs:
                continue
            if op.source_block.topic_id in mass_delete_topics:
                continue  # subsumed by topic-level advisory
            idx = op.updated_index
            if idx is None or idx >= len(article_blocks):
                continue
            ab = article_blocks[idx]
            article_links = getattr(ab, "links", None) or []
            article_hrefs = tuple(
                h for (_t, h) in article_links if h
            )
            if not article_hrefs:
                # Article side had no link at this position — likely
                # the parser didn't preserve it; don't flag.
                continue
            if _hrefs_equivalent(
                op.source_block.xref_hrefs, article_hrefs,
            ):
                continue
            tid = op.source_block.topic_id
            stale_per_topic.setdefault(tid, []).append(
                (op.source_block.text, op.source_block.xref_hrefs, article_hrefs),
            )
        for tid, entries in sorted(stale_per_topic.items()):
            sample_lines = []
            for text, src_hrefs, art_hrefs in entries[:3]:
                sample_lines.append(
                    f'  • "{text[:60]}": DITA → {", ".join(src_hrefs)} '
                    f'vs article → {", ".join(art_hrefs)}'
                )
            extra = (
                f"\n…and {len(entries) - 3} more"
                if len(entries) > 3 else ""
            )
            report.results.append(
                PatchResult(
                    op=None,
                    category=ResultCategory.DETECTED,
                    reason=(
                        f"{len(entries)} link(s) in '{tid}' may have "
                        "outdated <xref href>s — the DITA's href "
                        "doesn't match the live article's link target. "
                        "Common cause: the article-ID format changed "
                        "(old numeric IDs like 87951 → new 'a' + number "
                        "like a1342713). The tool does NOT auto-rewrite "
                        "hrefs (the new target could be a different "
                        "document) — update by hand if the new href is "
                        "the right one. Affected links:\n"
                        + "\n".join(sample_lines)
                        + extra
                    ),
                    topic_id=tid,
                )
            )

    # New FAQ topic advisory. When an article has an
    # `<span class="article-content__collapsible-trigger-text">`
    # (parsed as kind="expandable_header") whose text doesn't match
    # any source topic title, the writer needs to create a new .dita
    # topic file AND add a <topicref> for it in the .ditamap. The
    # tool never auto-creates topic files. Beta surfaced this on
    # Article 5 (a multi-topic FAQ): a newly added question
    # showed up as a generic paragraph INSERT instead of an explicit
    # "new topic needed" callout.
    if article_blocks is not None and publication is not None:
        topic_titles = {
            normalize_for_match(b.text): b.topic_id
            for b in publication.blocks
            if b.element_xpath.endswith("/title[1]")
            and b.topic_id != "<ditamap>"
            and b.text
        }
        new_questions: List[str] = []
        for ab in article_blocks:
            if getattr(ab, "kind", None) != "expandable_header":
                continue
            text = (getattr(ab, "text", "") or "").strip()
            if not text:
                continue
            if normalize_for_match(text) in topic_titles:
                continue
            new_questions.append(text)
        if new_questions:
            sample_lines = [f"  • \"{q[:80]}\"" for q in new_questions[:6]]
            extra = (
                f"\n…and {len(new_questions) - 6} more"
                if len(new_questions) > 6 else ""
            )
            report.results.append(
                PatchResult(
                    op=None,
                    category=ResultCategory.DETECTED,
                    reason=(
                        f"{len(new_questions)} new FAQ-style question(s) in "
                        "the live article have no matching .dita topic. "
                        "Each one needs a new .dita topic file AND a "
                        "<topicref> added to the .ditamap. The tool does "
                        "NOT auto-create files. New questions:\n"
                        + "\n".join(sample_lines)
                        + extra
                    ),
                )
            )

    # Screenshot-present advisory. Beta surfaced that an article
    # screenshot was added but the writer didn't see any signal to
    # add it. We can't auto-insert the <image> (we don't know the
    # asset href), but we can list every article position
    # that has a screenshot so the writer scans them by eye. We only
    # flag content-class screenshots — the parser separates these
    # from inline icon glyphs (li-icon / small svgs that already map
    # to <uicontrol> text in DITA).
    if article_blocks is not None:
        screenshot_positions: List[str] = []
        for ab in article_blocks:
            if not getattr(ab, "has_screenshot", False):
                continue
            text = (getattr(ab, "text", "") or "").strip()
            screenshot_positions.append(text[:80] or "(no surrounding text)")
        if screenshot_positions:
            sample_lines = [f"  • after: \"{s}\"" for s in screenshot_positions[:6]]
            extra = (
                f"\n…and {len(screenshot_positions) - 6} more"
                if len(screenshot_positions) > 6 else ""
            )
            report.results.append(
                PatchResult(
                    op=None,
                    category=ResultCategory.DETECTED,
                    reason=(
                        f"The live article has {len(screenshot_positions)} "
                        "screenshot(s) the tool did NOT add to the DITA "
                        "— we don't know the matching asset href in the "
                        "asset catalog, so the writer must place the "
                        "<image> element by hand. Positions (article-side "
                        "text just before each screenshot):\n"
                        + "\n".join(sample_lines)
                        + extra
                    ),
                )
            )

    # Icon-presence advisory. Same idea as the screenshot one but
    # scoped to lines the tool is actively adding (INSERT) or
    # rewriting (REPLACE). Without this, an inline UI-icon glyph
    # (`<li-icon>`, small <svg>) on a brand-new step gets inserted as
    # text-only — the writer has no easy signal that they need to
    # place the icon by hand. We deliberately do NOT fire on EQUAL
    # blocks: those keep their existing DITA wording and icon, so
    # there's nothing for the writer to do. Screenshot blocks are
    # excluded too (they're surfaced by the broader screenshot
    # advisory).
    if article_blocks is not None:
        icon_positions: List[Tuple[str, str]] = []
        for op in ops:
            if op.kind not in (OpKind.INSERT, OpKind.REPLACE):
                continue
            idx = op.updated_index
            if idx is None or idx >= len(article_blocks):
                continue
            ab = article_blocks[idx]
            if not getattr(ab, "has_inline_image", False):
                continue
            if getattr(ab, "has_screenshot", False):
                continue
            text = (getattr(ab, "text", "") or "").strip()
            icon_positions.append(
                (op.kind.value, text[:80] or "(no surrounding text)")
            )
        if icon_positions:
            sample_lines = [
                f"  • [{k}] \"{t}\"" for k, t in icon_positions[:6]
            ]
            extra = (
                f"\n…and {len(icon_positions) - 6} more"
                if len(icon_positions) > 6 else ""
            )
            report.results.append(
                PatchResult(
                    op=None,
                    category=ResultCategory.DETECTED,
                    reason=(
                        f"The live article has an inline UI icon in "
                        f"{len(icon_positions)} new or rewritten line(s). "
                        "The text was inserted/replaced, but the <image> "
                        "element for the icon glyph was NOT added (we "
                        "don't know the matching asset href in the "
                        "asset catalog). Add the icon by hand at each "
                        "position:\n"
                        + "\n".join(sample_lines)
                        + extra
                    ),
                )
            )

    # Reltable href staleness. The .ditamap's <reltable> lists Related
    # tasks / Learn more / See also links that the topic-body diff
    # doesn't touch. Beta surfaced a real case: a "Contact us" link
    # whose href changed in the article but the tool didn't flag it.
    # Scan article-side blocks AFTER any Related-tasks/Learn-more
    # heading and compare each link by display text against the
    # reltable entries. When the navtitle matches but the href differs,
    # emit a single advisory listing the affected links. We never
    # auto-rewrite the .ditamap.
    if reltable_entries and article_blocks is not None:
        article_link_after_section: Dict[str, List[Tuple[str, str]]] = {}
        # Match the article's heading text against the IM section
        # categories used in <relcolspec @type>.
        _HEADING_TO_SECTION = {
            "related task": "Related tasks",
            "related tasks": "Related tasks",
            "related topic": "Related tasks",
            "related topics": "Related tasks",
            "related article": "Related tasks",
            "related articles": "Related tasks",
            "related link": "Related tasks",
            "related links": "Related tasks",
            "learn more": "Learn more",
            "learn more about": "Learn more",
            "see also": "See also",
        }
        current_section: Optional[str] = None
        for ab in article_blocks:
            text = (getattr(ab, "text", "") or "").strip().lower()
            section = _HEADING_TO_SECTION.get(text)
            if section is not None:
                current_section = section
                continue
            if current_section is None:
                continue
            links = getattr(ab, "links", None) or []
            for link_text, link_href in links:
                if not link_text or not link_href:
                    continue
                article_link_after_section.setdefault(
                    current_section, []
                ).append(
                    (link_text.strip(), link_href.strip())
                )

        stale_reltable: List[Tuple[str, str, str, str]] = []
        for entry in reltable_entries:
            article_links = article_link_after_section.get(
                entry.section, []
            )
            entry_navtitle_norm = normalize_for_match(entry.navtitle)
            for art_text, art_href in article_links:
                if normalize_for_match(art_text) != entry_navtitle_norm:
                    continue
                if _hrefs_equivalent((entry.href,), (art_href,)):
                    continue
                stale_reltable.append(
                    (entry.section, entry.navtitle, entry.href, art_href),
                )
                break

        if stale_reltable:
            # Build a structured per-link list. The renderer recognizes
            # lines starting with "• " inside the reason text and lays
            # them out as a proper <ul><li> block, so this isn't just
            # a wall of text any more.
            link_lines = []
            for section, nav, dita_href, art_href in stale_reltable[:6]:
                link_lines.append(
                    f'• [{section}] "{nav[:50]}"\n'
                    f'    .ditamap: {dita_href}\n'
                    f'    article:  {art_href}'
                )
            if len(stale_reltable) > 6:
                link_lines.append(
                    f"• …and {len(stale_reltable) - 6} more"
                )
            count_phrase = (
                f"{len(stale_reltable)} reltable link"
                + ("s" if len(stale_reltable) != 1 else "")
            )
            report.results.append(
                PatchResult(
                    op=None,
                    # MAP_EDIT routes this to the Map updates section.
                    # Used to be DETECTED, which surfaced the wall of
                    # text under a topic-body advisory tab and made
                    # writers think this was a per-topic concern.
                    category=ResultCategory.MAP_EDIT,
                    reason=(
                        f"Stale reltable hrefs: {count_phrase} in the "
                        ".ditamap may need updating. The article shows "
                        "the same link text pointing at a different "
                        "href (commonly the article-ID format change "
                        "— old numeric IDs like 87951 vs. new 'a' + "
                        "number like a1342713). The tool never auto-"
                        "rewrites .ditamap hrefs; verify each one and "
                        "update by hand if the article's target is "
                        "correct.\n\nAffected links:\n"
                        + "\n".join(link_lines)
                    ),
                )
            )

    # Group mutating ops (REPLACE, DELETE, INSERT) by topic so each topic
    # file is opened and written exactly once.
    #
    # For INSERTs we detect a "manual-review section run" — when an INSERT's
    # text matches a Related-tasks / Learn-more / See-also heading, that
    # INSERT and every subsequent INSERT in op order is treated as reltable
    # noise (skipped, not applied). Articles place these at the end as a
    # block; the run continues until the ops stream ends. Ops are scanned
    # in their original sequence so the state machine matches publication
    # order.
    mutating_by_topic: Dict[Path, List[DiffOp]] = {}
    current_reltable_section: Optional[str] = None  # "Related tasks" / "Learn more" / "See also"
    # We collapse all reltable detections into a single MAP_EDIT at the
    # end. Individual headings/links don't need per-item snippets — the
    # user reviews the reltable as a whole.
    reltable_first_op: Optional[DiffOp] = None
    reltable_sections: set = set()
    reltable_item_count = 0
    for op in ops:
        if op.kind == OpKind.EQUAL:
            continue

        # A "Related tasks" / "Learn more" / "See also" article block can
        # arrive as either an INSERT (no DITA counterpart) OR a REPLACE
        # (the article block accidentally aligns with an unrelated DITA
        # element). In either case it marks the start of a reltable
        # section; flip the flag so subsequent article items get
        # consolidated rather than applied. The REPLACE op itself still
        # falls through to the regular handling below — typically it'll
        # get skipped by structural / alignment-ambiguity safeguards.
        if op.kind in (OpKind.INSERT, OpKind.REPLACE):
            text_norm = normalize_for_match(op.updated_text or "").lower()
            if text_norm in MANUAL_REVIEW_TITLES:
                current_reltable_section = op.updated_text or ""
                if reltable_first_op is None:
                    reltable_first_op = op
                reltable_sections.add(current_reltable_section)
                # Consume the op — for both INSERT and REPLACE. The
                # reltable MAP_EDIT at the end covers this whole
                # section. Letting a REPLACE through to "regular flow"
                # only produces a misleading SKIPPED + DETECTED pair
                # showing a clearly-misaligned pairing (e.g. a source
                # <note> aligned with the article's "Related tasks"
                # heading text). The writer already gets the reltable
                # advisory; no need to surface the noise too.
                continue

        if op.kind == OpKind.INSERT:
            if current_reltable_section is not None:
                if reltable_first_op is None:
                    reltable_first_op = op
                reltable_sections.add(current_reltable_section)
                reltable_item_count += 1
                continue
            # Article-side feature-launcher INSERTs are never auto-
            # applied. The HTML renders the promo text inline, which
            # the diff treats as new content — but the DITA equivalent
            # carries custom @outputclass styling and a launcher URL
            # we can't reproduce. Refuse and flag for manual addition.
            insert_article_block = (
                article_blocks[op.updated_index]
                if (
                    article_blocks is not None
                    and op.updated_index is not None
                    and op.updated_index < len(article_blocks)
                ) else None
            )
            if _is_article_feature_launcher(insert_article_block):
                report.results.append(
                    PatchResult(
                        op=op,
                        category=ResultCategory.SKIPPED,
                        reason=_FEATURE_NOTE_SKIP_REASON,
                    )
                )
                continue
            if op.anchor_block is None:
                report.results.append(
                    PatchResult(
                        op=op,
                        category=ResultCategory.DETECTED,
                        reason=(
                            "INSERT at <publication start> has no anchor; "
                            "cannot place automatically"
                        ),
                    )
                )
                continue
            if not op.safe_to_apply:
                # Anchor sits in a manual-review zone; skip with its reason.
                reason = op.op_skip_reason or (
                    op.anchor_block.skip_reason or "anchor in unsafe zone"
                )
                report.results.append(
                    PatchResult(op=op, category=ResultCategory.SKIPPED, reason=reason)
                )
                continue
            if _is_cross_section(op, op.anchor_block, article_blocks, topic_to_section, report):
                continue
            mutating_by_topic.setdefault(
                op.anchor_block.topic_path, []
            ).append(op)
            continue
        # REPLACE or DELETE
        if op.source_block is None:
            continue  # defensive
        # Route unsafe ops to SKIPPED here, before they enter
        # mutating_by_topic. Otherwise we'd try to load the source's
        # topic_path — which for synthesized MapLabel blocks (topichead
        # navtitles) is the literal placeholder string "<ditamap>" and
        # not a real file. Same end result as the safety check inside
        # the per-topic loop; this just runs earlier so we don't crash
        # on the file read.
        #
        # Exception: note-extend candidates LOOK unsafe (structural
        # note) but actually have a clean additive path. Let them
        # through so the per-topic loop can take the extend branch
        # before falling back to the unsafe skip.
        # Feature-note safe-delete: when a source DITA feature note is
        # being DELETEd AND the live article no longer renders any
        # feature-launcher callout, the delete is genuinely intended
        # (the article dropped the promo). Bypass the structural-unsafe
        # gate and let the per-topic loop apply the DELETE normally.
        # When the article DOES still have a launcher, the original
        # refusal stands — the writer must add the new launcher by hand.
        if (
            op.kind == OpKind.DELETE
            and _is_source_feature_note(op.source_block)
            and not article_has_feature_launcher
        ):
            safe_op = _dc_replace(op, safe_to_apply=True)
            mutating_by_topic.setdefault(
                op.source_block.topic_path, []
            ).append(safe_op)
            continue
        if not op.safe_to_apply and not _can_extend_note(op):
            # Mass-deletion sink: if this op belongs to a topic whose
            # topic-level "entire topic appears removed" advisory has
            # already fired, drop it silently. The advisory subsumes
            # every per-block DELETE/REPLACE for that topic — surfacing
            # individual unsafe-skip reasons would bloat needs-review
            # with redundant lines for an action the writer takes once
            # at the topic level (delete the file + .ditamap entry).
            if (
                op.source_block is not None
                and op.source_block.topic_id in mass_delete_topics
            ):
                continue
            # If the article side's text is fully contained in the
            # source-side text (after normalization), the diff opcode
            # is noise — the DITA just has more context (a filename,
            # a label) that the article doesn't render. Don't flag it.
            # Saves the writer from reviewing items that aren't really
            # wording changes.
            if op.kind == OpKind.REPLACE and _is_no_real_change(op):
                continue
            # Cell-aware row REPLACE: when the source is a <row> AND
            # the article side carries per-cell data, we have enough
            # information to do cell-level matching in the per-topic
            # loop. Bypass the structural-skip gate so it can attempt
            # the apply.
            if (
                op.kind == OpKind.REPLACE
                and op.source_block.element_tag == "row"
                and article_blocks is not None
                and op.updated_index is not None
                and op.updated_index < len(article_blocks)
                and getattr(article_blocks[op.updated_index], "cells", None)
            ):
                mutating_by_topic.setdefault(
                    op.source_block.topic_path, []
                ).append(op)
                continue
            # Reason cascade: the source block's skip_reason wins when
            # the source is structural (feature note, manual zone, etc.)
            # because that's a semantic refusal — the writer wants to
            # know "we won't touch this kind of block" rather than the
            # downstream LCS detail ("alignment ambiguity"). For
            # non-structural refusals, the op's own reason carries the
            # specific diff-time context and stays first.
            if op.source_block.structural and op.source_block.skip_reason:
                reason = op.source_block.skip_reason
            else:
                reason = (
                    op.op_skip_reason
                    or op.source_block.skip_reason
                    or "auto_update=False"
                )
            _record_replace_skip(report, op, reason)
            continue
        if op.kind == OpKind.REPLACE and _is_cross_section(
            op, op.source_block, article_blocks, topic_to_section, report,
        ):
            continue
        if op.kind == OpKind.DELETE:
            # Mass-deletion guard runs FIRST: if this topic is in a
            # topic-level "entire topic appears removed" advisory, the
            # advisory subsumes every per-block DELETE — including the
            # <title> DELETE. Surfacing the title's own "title removal
            # refused" message alongside the advisory would just bloat
            # needs-review with a line saying the same thing.
            tid = op.source_block.topic_id
            if tid in mass_delete_topics:
                continue
            # Topic title preservation: a topic <title> being marked for
            # DELETE is almost always a diff misalignment (articles
            # don't ship without their heading). Refuse and let the
            # user decide.
            if op.source_block.element_xpath.endswith("/title[1]"):
                report.results.append(
                    PatchResult(
                        op=op,
                        category=ResultCategory.SKIPPED,
                        reason=(
                            "DELETE on a topic <title> refused — title "
                            "removal is almost always a diff misalignment, "
                            "not an intentional deletion. Verify whether "
                            "the article truly removed this topic; if so, "
                            "delete the file and update the .ditamap."
                        ),
                    )
                )
                continue
        mutating_by_topic.setdefault(op.source_block.topic_path, []).append(op)

    for topic_path, topic_ops in mutating_by_topic.items():
        tree, xml_decl, doctype = _read_xml(topic_path)
        root = tree.getroot()

        applied_in_topic = 0

        # Both DELETEs and INSERTs shift positional indices of subsequent
        # siblings, which would invalidate other ops' xpaths in this batch.
        # Execute REPLACEs first (no shift), then DELETEs in reverse doc
        # order (preserves earlier indices), then INSERTs in reverse doc
        # order (preserves earlier anchor indices and keeps multiple
        # inserts at the same anchor in their original op order).
        # Sort key needs the article-block kind for INSERT ops so it can
        # put step INSERTs BEFORE list_item INSERTs within the same
        # anchor group. That lets the loop record each new <step> and
        # redirect subsequent list_items to anchor inside it.
        def _kind_for_op(op):
            if op.updated_index is None or article_blocks is None:
                return None
            if op.updated_index >= len(article_blocks):
                return None
            return getattr(article_blocks[op.updated_index], "kind", None)

        ordered_ops = sorted(topic_ops, key=lambda o: _op_sort_key(o, _kind_for_op(o)))

        # Pre-resolve INSERT anchor xpaths to element references BEFORE
        # any DELETE runs. Element-tree object references stay valid
        # across DELETEs on other elements, even though positional
        # xpaths (e.g. "li[2]") renumber once siblings are removed.
        # Without this, an INSERT anchored at li[2] gets SKIPPED because
        # li[2]'s xpath no longer resolves after li[1] (or li[3]) was
        # deleted. Beta surfaced this on before_you_begin where 3 of 6
        # new bullets disappeared from the output.
        anchor_cache: Dict[int, ET.Element] = {}
        for op in ordered_ops:
            if op.kind != OpKind.INSERT or op.anchor_block is None:
                continue
            resolved = _find_by_xpath(root, op.anchor_block.element_xpath)
            if resolved is not None:
                anchor_cache[id(op)] = resolved

        # Anchor-propagation state: maps source anchor xpath → new <cmd>
        # element of the step we created at that anchor. Subsequent
        # list_item INSERTs at the same anchor get redirected to insert
        # inside the new step instead of next to the original source
        # element.
        new_step_cmd_by_anchor: Dict[str, ET.Element] = {}

        for op in ordered_ops:
            # Note-extend special case: when the article appended
            # paragraphs to an existing <note>, apply additively rather
            # than refusing as structural. Existing children are kept;
            # only new <p> children are appended.
            if _can_extend_note(op):
                element = _find_by_xpath(root, op.source_block.element_xpath)
                if element is not None:
                    article_block = (
                        article_blocks[op.updated_index]
                        if article_blocks is not None
                        and op.updated_index is not None
                        and op.updated_index < len(article_blocks)
                        else None
                    )
                    if _extend_note(element, op, article_block):
                        report.results.append(
                            PatchResult(
                                op=op,
                                category=ResultCategory.APPLIED,
                                warning=(
                                    "extended <note> with new <p> children; "
                                    "review for correct paragraph "
                                    "boundaries and inline markup"
                                ),
                            )
                        )
                        applied_in_topic += 1
                        continue

            if not op.safe_to_apply:
                # Cell-aware row REPLACE escape hatch: even though the
                # source row is marked structural, the pre-check
                # already routed it here because the article side has
                # per-cell data. Let it fall through to the REPLACE
                # branch where the cell-aware handler runs.
                _row_replace_with_cells = (
                    op.kind == OpKind.REPLACE
                    and op.source_block is not None
                    and op.source_block.element_tag == "row"
                    and article_blocks is not None
                    and op.updated_index is not None
                    and op.updated_index < len(article_blocks)
                    and getattr(article_blocks[op.updated_index], "cells", None)
                )
                if not _row_replace_with_cells:
                    if op.source_block is not None:
                        reason = (
                            op.op_skip_reason
                            or op.source_block.skip_reason
                            or "auto_update=False"
                        )
                    else:
                        reason = op.op_skip_reason or "anchor in unsafe zone"
                    _record_replace_skip(report, op, reason)
                    continue

            if op.kind == OpKind.REPLACE:
                element = _find_by_xpath(root, op.source_block.element_xpath)
                if element is None:
                    report.results.append(
                        PatchResult(
                            op=op,
                            category=ResultCategory.SKIPPED,
                            reason=f"xpath not resolvable: {op.source_block.element_xpath}",
                        )
                    )
                    continue
                had_children = len(list(element)) > 0
                article_block = (
                    article_blocks[op.updated_index]
                    if article_blocks is not None
                       and op.updated_index is not None
                       and op.updated_index < len(article_blocks)
                    else None
                )
                links = getattr(article_block, "links", None) if article_block is not None else None
                emphasis = getattr(article_block, "emphasis", None) if article_block is not None else None

                # <dlentry> REPLACE — dt/dd-aware: split the article
                # text on the first separator (`:`/`-`/`–`/`—`),
                # validate that the term half still matches <dt>, and
                # rewrite the definition half into <dd> only. Inline
                # markup inside <dd> (e.g. <uicontrol>, <xref>) is
                # preserved when each child's phrase is still present
                # in the new definition; otherwise it's dropped and the
                # writer is told via a warning. The stylesheet adds
                # the separator at render time, so we never write it
                # back into the DITA. Complex dlentries (multi-term,
                # <dd> with nested ul/note) drop through to the
                # structural skip below.
                if _local(element.tag) == "dlentry":
                    new_text = op.updated_text or ""
                    result = _apply_dlentry_replace(element, new_text)
                    if result is not None:
                        inline_dropped, dlentry_warning = result
                        warning_parts = []
                        if dlentry_warning:
                            warning_parts.append(dlentry_warning)
                        report.results.append(
                            PatchResult(
                                op=op,
                                category=ResultCategory.APPLIED,
                                warning=" | ".join(warning_parts),
                            )
                        )
                        applied_in_topic += 1
                        continue
                    # Not a simple dlentry or term changed — surface for
                    # review with a clearer reason.
                    report.results.append(
                        PatchResult(
                            op=op,
                            category=ResultCategory.SKIPPED,
                            reason=(
                                "<dlentry> can't be auto-rewritten: either "
                                "the term changed, the article had no "
                                "Term/Definition separator, the dlentry "
                                "has multiple <dt>/<dd> children, or the "
                                "<dd> contains block-level structure "
                                "(<ul>, <note>, etc.). Review manually."
                            ),
                        )
                    )
                    continue

                # <row> REPLACE — cell-aware: when the article side gave
                # us per-cell text, match positionally against the
                # source <entry> children and rewrite only the cells
                # that actually changed. Inline markup in unchanged
                # cells is preserved verbatim. If we can't match
                # cleanly (different cell count, etc.), fall through
                # to the structural skip path.
                if _local(element.tag) == "row" and article_block is not None:
                    article_cells = getattr(article_block, "cells", None)
                    if article_cells:
                        result = _apply_cell_aware_row_replace(
                            element, article_cells,
                            getattr(article_block, "cell_emphasis", None),
                        )
                        if result is not None:
                            applied_cells, warning = result
                            report.results.append(
                                PatchResult(
                                    op=op,
                                    category=ResultCategory.APPLIED,
                                    warning=warning,
                                )
                            )
                            applied_in_topic += 1
                            continue
                        # Couldn't match cleanly — refuse for review.
                        report.results.append(
                            PatchResult(
                                op=op,
                                category=ResultCategory.SKIPPED,
                                reason=(
                                    "structural element <row> — cell counts "
                                    "don't match. The tool can't safely "
                                    "update this table change; please "
                                    "review and add it manually."
                                ),
                            )
                        )
                        continue

                # <note> REPLACE: per IM page 132, the body of a <note>
                # must start with a block element (<p>, <ul>, etc.) —
                # never raw text directly. Rebuild paragraph children
                # from the new text. Each \\n\\n separates paragraphs;
                # we never insert as a bare-text <note>.
                if _local(element.tag) == "note":
                    note_kind = element.get("type")
                    new_text = op.updated_text or ""
                    note_bullets = (
                        getattr(article_block, "note_bullets", None)
                        if article_block is not None else None
                    )
                    # Upgrade the existing <note> attributes from the
                    # article-side note kind when the parser identified
                    # one (e.g. a callout whose headline reads "Here's
                    # a tip" sets the article block's note_kind to
                    # "tip" even when the CSS class is the generic
                    # "--note"). Without this, the prose gets rewritten
                    # but the @type stays as whatever the source DITA
                    # author originally chose — Beta surfaced a parent
                    # topic stuck at `<note type="important">` after a
                    # rewrite to tip-style content.
                    #
                    # Route the kind through `_NOTE_KIND_TO_DITA_ATTRS`
                    # so multi-attribute mappings stay correct. A naive
                    # `element.set("type", note_kind)` would write
                    # invalid DITA for `permission` / `feature` / `pdf`
                    # callouts, which must be `type="other"` plus an
                    # `othertype` value (e.g. `othertype="role"`).
                    article_note_kind = (
                        getattr(article_block, "note_kind", None)
                        if article_block is not None else None
                    )
                    if article_note_kind:
                        new_attrs = _NOTE_KIND_TO_DITA_ATTRS.get(
                            article_note_kind.lower()
                        )
                        if new_attrs is not None:
                            current_attrs = {
                                "type": element.get("type"),
                                "othertype": element.get("othertype"),
                            }
                            if current_attrs != {
                                "type": new_attrs.get("type"),
                                "othertype": new_attrs.get("othertype"),
                            }:
                                # Clear stale @othertype before applying
                                # the new set so `tip` → `permission`
                                # transitions don't leave a stray
                                # othertype behind.
                                if "othertype" in element.attrib:
                                    del element.attrib["othertype"]
                                for k, v in new_attrs.items():
                                    element.set(k, v)
                                note_kind = new_attrs.get("type")
                    source_has_bullets = any(
                        _local(c.tag) in ("ul", "ol")
                        for c in element.iter()
                        if c is not element
                    )

                    # Refuse if the source had bullets/list children but
                    # the article side gave us flat prose (no
                    # note_bullets). Rebuilding as <p> would silently
                    # destroy the source's bullet structure. Surface
                    # for review instead.
                    if source_has_bullets and not note_bullets:
                        report.results.append(
                            PatchResult(
                                op=op,
                                category=ResultCategory.SKIPPED,
                                reason=(
                                    "<note> contains a <ul>/<ol>, but the "
                                    "article-side rewrite is flat prose. "
                                    "Applying would lose the list structure. "
                                    "Review and apply by hand if the article "
                                    "really did flatten the bullets."
                                ),
                            )
                        )
                        continue

                    # Decide whether the rebuild is risky enough to warrant
                    # a "Please verify" warning on the resulting card.
                    # A clean rebuild — plain prose source → plain prose
                    # replacement, no inline markup, no links, paragraph
                    # count matches — has nothing for the writer to
                    # verify. Beta feedback (2026-06-24): every note
                    # REPLACE was getting flagged, even simple wording
                    # changes on text-only notes, which inflated the
                    # actionable count and eroded trust in the warning.
                    is_lossless_rebuild = _note_rebuild_is_lossless(
                        element, new_text, note_bullets, links, emphasis,
                    )
                    # Snapshot existing <xref>s by their link text BEFORE
                    # clearing the note. When the rebuilt paragraphs
                    # mention the same link text again, we drop the
                    # original <xref> back in untouched — keeping
                    # outputclass, format, scope, and any other
                    # attributes the source author set.
                    original_xrefs_by_text = {}
                    for descendant in element.iter():
                        if _local(descendant.tag) != "xref":
                            continue
                        link_text = (descendant.text or "").strip()
                        if link_text:
                            original_xrefs_by_text[link_text] = descendant
                    # Clear existing children
                    for child in list(element):
                        element.remove(child)
                    element.text = None

                    if note_bullets:
                        # Rebuild as <note><ul><li>…</li></ul></note>.
                        # Matches how _insert_note_handler builds new
                        # bullet-bearing notes; preserves any existing
                        # <xref> attributes by text-match (same logic
                        # as the prose path below).
                        ul_el = ET.SubElement(element, "ul")
                        for bi, bullet_text in enumerate(note_bullets):
                            is_last = bi == len(note_bullets) - 1
                            bullet_links = links if is_last else None
                            li_el, _ = _build_inline_element(
                                "li", bullet_text, bullet_links,
                                emphasis=emphasis if is_last else None,
                            )
                            for new_xref in list(li_el.iter()):
                                if _local(new_xref.tag) != "xref":
                                    continue
                                link_text = (new_xref.text or "").strip()
                                original = original_xrefs_by_text.get(link_text)
                                if original is None:
                                    continue
                                new_href = new_xref.get("href")
                                new_xref.attrib.clear()
                                for k, v in original.attrib.items():
                                    new_xref.set(k, v)
                                if new_href:
                                    new_xref.set("href", new_href)
                            ul_el.append(li_el)
                    else:
                        # No bullets — rebuild as one <p> per paragraph,
                        # split on blank lines.
                        parts = [p.strip() for p in re.split(r"\n\n+", new_text) if p.strip()]
                        if not parts:
                            parts = [new_text.strip()] if new_text.strip() else []
                        for i, para_text in enumerate(parts):
                            is_last = i == len(parts) - 1
                            para_links = links if is_last else None
                            p_el, _unplaced = _build_inline_element(
                                "p", para_text, para_links,
                                emphasis=emphasis if is_last else None,
                            )
                            for new_xref in list(p_el.iter()):
                                if _local(new_xref.tag) != "xref":
                                    continue
                                link_text = (new_xref.text or "").strip()
                                original = original_xrefs_by_text.get(link_text)
                                if original is None:
                                    continue
                                new_href = new_xref.get("href")
                                new_xref.attrib.clear()
                                for k, v in original.attrib.items():
                                    new_xref.set(k, v)
                                if new_href:
                                    new_xref.set("href", new_href)
                            element.append(p_el)

                    report.results.append(
                        PatchResult(
                            op=op,
                            category=ResultCategory.APPLIED,
                            warning=(
                                ""
                                if is_lossless_rebuild
                                else "<note> body was rebuilt from the article "
                                "text. Verify the new note still reads "
                                "correctly."
                            ),
                        )
                    )
                    applied_in_topic += 1
                    continue

                # #4: refuse to silently discard inline markup. If the
                # existing element has child elements (<xref>, <uicontrol>,
                # <keyword>, …) and the article-side block has no link
                # metadata that would rebuild equivalent structure, try
                # to preserve the markup by re-wrapping the same phrases
                # inside the new article text. Common case: the article
                # added a prefix ("In the Buttons tab, ") to a step
                # whose only inline markup is a <uicontrol> whose text
                # still appears verbatim in the new wording. If the
                # article also <strong>-marked new phrases, wrap them in
                # <em> as we go.
                if had_children and not links:
                    ok, em_added = _replace_preserving_inline_markup(
                        element, op.updated_text or "", emphasis,
                    )
                    if ok:
                        warning_parts = [
                            "rewording applied while preserving original "
                            "inline markup (<uicontrol>, <xref>, etc.); "
                            "review that the markup still wraps the "
                            "intended phrase"
                        ]
                        if em_added:
                            warning_parts.append(
                                f"added {em_added} <em> wrap"
                                + ("s" if em_added != 1 else "")
                                + " for article-side bold/italic — "
                                "MANUALLY VERIFY each <em>: it may be "
                                "more accurate as <uicontrol> (UI button/"
                                "field/menu), <wintitle> (window/page/"
                                "dialog title), or <keyword> (technical "
                                "term)."
                            )
                        report.results.append(
                            PatchResult(
                                op=op,
                                category=ResultCategory.APPLIED,
                                warning=" | ".join(warning_parts),
                            )
                        )
                        applied_in_topic += 1
                        continue
                    # The new wording doesn't contain the phrases that
                    # were wrapped in <xref>/<uicontrol>/<keyword>/etc.,
                    # so the original markup is no longer applicable.
                    # Apply the rewording and drop the markup at this
                    # position. The article is the source of truth —
                    # if a phrase isn't there anymore, the wrapping
                    # shouldn't be either. The writer reviews the
                    # warning and re-adds markup if the new wording
                    # needs it.
                    #
                    # BUT: if the article side highlighted phrases (bold/
                    # italic), STILL wrap those in <em> after dropping
                    # the original markup. Without this, a step like
                    #   Source: Click <uicontrol>Page info</uicontrol>
                    #           in the upper-left of <uicontrol>Edit
                    #           </uicontrol> pane.
                    #   Article: Click the **Page info** tab.
                    # would lose both <uicontrol>s AND the article's
                    # bold "Page info" — Beta surfaced this.
                    em_added = 0
                    if emphasis:
                        # Clear children so the preserving function
                        # takes its emphasis-only path.
                        for c in list(element):
                            element.remove(c)
                        element.text = None
                        ok2, em_added = _replace_preserving_inline_markup(
                            element, op.updated_text or "", emphasis,
                        )
                        if not ok2:
                            # Emphasis couldn't be placed either (rare).
                            # Fall through to plain text.
                            _replace_with_inline(
                                element, op.updated_text or "", links,
                            )
                    else:
                        _replace_with_inline(
                            element, op.updated_text or "", links,
                        )
                    warning_parts = [
                        "applied the article's new wording; the "
                        "original inline markup (<xref>/<uicontrol>"
                        "/<keyword> etc.) was dropped because the "
                        "new wording doesn't contain the wrapped "
                        "phrases. Review and re-add any markup the "
                        "new wording needs."
                    ]
                    if em_added:
                        warning_parts.append(
                            f"added {em_added} <em> wrap"
                            + ("s" if em_added != 1 else "")
                            + " for article-side bold/italic — MANUALLY "
                            "VERIFY each <em>: it may be more accurate "
                            "as <uicontrol> (UI button/field/menu), "
                            "<wintitle> (window/page/dialog title), or "
                            "<keyword> (technical term)."
                        )
                    report.results.append(
                        PatchResult(
                            op=op,
                            category=ResultCategory.APPLIED,
                            warning=" | ".join(warning_parts),
                        )
                    )
                    applied_in_topic += 1
                    continue

                # If the article highlighted phrases with <strong>/<em>
                # and the source has no competing inline markup, prefer
                # the markup-preserving rebuild so the <em> wrap actually
                # makes it into the output.
                if not had_children and emphasis and not links:
                    ok, em_added = _replace_preserving_inline_markup(
                        element, op.updated_text or "", emphasis,
                    )
                    if ok:
                        report.results.append(
                            PatchResult(
                                op=op,
                                category=ResultCategory.APPLIED,
                                warning=(
                                    f"added {em_added} <em> wrap"
                                    + ("s" if em_added != 1 else "")
                                    + " for article-side bold/italic — "
                                    "MANUALLY VERIFY each <em>: it may be "
                                    "more accurate as <uicontrol> (UI "
                                    "button/field/menu), <wintitle> "
                                    "(window/page/dialog title), or "
                                    "<keyword> (technical term)."
                                ),
                            )
                        )
                        applied_in_topic += 1
                        continue
                unplaced = _replace_with_inline(element, op.updated_text or "", links)
                warning = ""
                if unplaced > 0:
                    warning = (
                        f"{unplaced} link(s) detected in the article "
                        "could not be placed (text not found in block); "
                        "add the <xref> manually"
                    )
                report.results.append(
                    PatchResult(
                        op=op,
                        category=ResultCategory.APPLIED,
                        warning=warning,
                    )
                )
                applied_in_topic += 1

            elif op.kind == OpKind.DELETE:
                parent, child = _find_parent_and_child(root, op.source_block.element_xpath)
                if parent is None or child is None:
                    report.results.append(
                        PatchResult(
                            op=op,
                            category=ResultCategory.SKIPPED,
                            reason=f"xpath not resolvable: {op.source_block.element_xpath}",
                        )
                    )
                    continue
                # Ambiguous duplicate-text xref siblings. When the source
                # has TWO siblings with the same visible text but
                # different <xref href> targets (e.g. the same
                # "Affidavit (PDF)" label listed under both "U.S.
                # members" and "Non-U.S. members" with different IDs)
                # AND the article only renders one such item, LCS
                # pairs the article-side item with whichever source
                # sibling came first and queues the other for DELETE.
                # The tool can't know which href the article actually
                # targets — refuse the DELETE and surface for review.
                if _ambiguous_xref_sibling_delete(child, root):
                    report.results.append(
                        PatchResult(
                            op=op,
                            category=ResultCategory.SKIPPED,
                            reason=(
                                "DELETE refused: this <xref>-bearing "
                                "item has a sibling with identical link "
                                "text but a different href. The tool "
                                "can't tell which href the live article "
                                "actually targets — review both items and "
                                "delete the one that doesn't apply. "
                                "Verify the surviving item's href against "
                                "the article (DITA hrefs may use the old "
                                "article-ID format and need updating to "
                                "the new 'a' + number format)."
                            ),
                        )
                    )
                    continue
                # Embedded-media preservation. A DELETE on a block that
                # contains <image>/<object>/<fig> almost always means the
                # diff couldn't align an embed (YouTube videos render as
                # <iframe>/<picture> in the article HTML but live as
                # <object>/<image> in DITA). Refuse rather than silently
                # strip the linked media. Xrefs are excluded — their
                # link text IS captured by the article parser, so the
                # diff's "not present in article" verdict is reliable.
                if _has_media_children(child):
                    report.results.append(
                        PatchResult(
                            op=op,
                            category=ResultCategory.SKIPPED,
                            reason=(
                                "DELETE refused: source element contains "
                                "<image>/<object>/<fig> children "
                                "(embedded media). The article parser does "
                                "not capture embedded videos/images and "
                                "would silently strip them. Verify whether "
                                "the media really was removed before "
                                "deleting manually."
                            ),
                        )
                    )
                    continue
                parent.remove(child)
                # Removing a <cmd> leaves an empty <step> parent (its
                # only required child gone). Drop the now-empty <step>
                # too so the patched file doesn't accumulate stray
                # placeholder steps after content was removed.
                if (
                    _local(child.tag) == "cmd"
                    and _local(parent.tag) == "step"
                    and len(parent) == 0
                ):
                    grandparent = _find_parent_of(root, parent)
                    if grandparent is not None:
                        grandparent.remove(parent)
                report.results.append(
                    PatchResult(op=op, category=ResultCategory.APPLIED)
                )
                applied_in_topic += 1

            elif op.kind == OpKind.INSERT:
                anchor_xpath = op.anchor_block.element_xpath
                article_block = (
                    article_blocks[op.updated_index]
                    if article_blocks is not None
                    and op.updated_index is not None
                    and op.updated_index < len(article_blocks)
                    else None
                )
                article_kind = (
                    getattr(article_block, "kind", None)
                    if article_block is not None else None
                )

                # Anchor propagation: when an earlier op in this same
                # topic batch already inserted a new <step> at the same
                # source anchor, redirect this list_item / sub-bullet
                # to the new step's <cmd>. Without this, article
                # sub-bullets attach to the WRONG (original-source)
                # step in the patched DITA.
                redirected = False
                if (
                    article_kind in ("list_item", "unordered_step")
                    and anchor_xpath in new_step_cmd_by_anchor
                ):
                    new_cmd = new_step_cmd_by_anchor[anchor_xpath]
                    new_parent = _find_parent_of(root, new_cmd)
                    if new_parent is not None:
                        anchor_elem = new_cmd
                        parent = new_parent
                        idx = list(parent).index(anchor_elem)
                        redirected = True

                if not redirected:
                    # Prefer the pre-resolved reference cached BEFORE
                    # any DELETEs ran. Element refs remain valid even
                    # when sibling DELETEs renumber positional xpaths.
                    anchor_elem = anchor_cache.get(id(op))
                    if anchor_elem is None:
                        anchor_elem = _find_by_xpath(root, anchor_xpath)
                    parent = _find_parent_of(root, anchor_elem) if anchor_elem is not None else None
                    if anchor_elem is None or parent is None:
                        report.results.append(
                            PatchResult(
                                op=op,
                                category=ResultCategory.SKIPPED,
                                reason=f"anchor xpath not resolvable: {anchor_xpath}",
                            )
                        )
                        continue
                    idx = list(parent).index(anchor_elem)

                # Title-anchor redirect: when the LCS-chosen anchor is a
                # topic <title> (a <title> whose parent is <concept> /
                # <task> / <reference>), inserting a new <p>/<note>/<ul>
                # as a sibling of <title> produces invalid DITA — these
                # block elements aren't allowed as direct children of
                # the topic root. Redirect the insertion to the FIRST
                # child of the corresponding body element (creating an
                # empty body if needed).
                _BODY_FOR_TOPIC = {
                    "concept": "conbody",
                    "task": "taskbody",
                    "reference": "refbody",
                }
                if (
                    _local(anchor_elem.tag) == "title"
                    and _local(parent.tag) in _BODY_FOR_TOPIC
                ):
                    body_tag = _BODY_FOR_TOPIC[_local(parent.tag)]
                    body_elem = next(
                        (c for c in parent if _local(c.tag) == body_tag),
                        None,
                    )
                    if body_elem is None:
                        body_elem = ET.SubElement(parent, body_tag)
                    # New content goes as the FIRST child of the body
                    # — the reverse-order insertion invariant keeps
                    # multiple INSERTs at the same anchor in forward
                    # article order via repeated insert(0, …).
                    #
                    # We pass an "anchor" that doesn't exist as a real
                    # sibling: by convention idx=-1 → insert at front
                    # of body. The handler's idx+1 lands at 0.
                    anchor_elem = body_elem
                    parent = body_elem
                    idx = -1

                # Snapshot <step> elements before the dispatch so we can
                # detect a newly-created step and remember it for later
                # list_item ops in this same group.
                steps_before = (
                    {id(s) for s in root.iter() if _local(s.tag) == "step"}
                    if article_kind in ("step", "unordered_step")
                    else None
                )

                outcome = _dispatch_insert(
                    root, anchor_elem, parent, idx, op, article_block,
                )

                if (
                    steps_before is not None
                    and outcome is not None
                    and outcome[0].category == ResultCategory.APPLIED
                ):
                    new_steps = [
                        s for s in root.iter()
                        if _local(s.tag) == "step" and id(s) not in steps_before
                    ]
                    if new_steps:
                        new_step = new_steps[0]
                        new_cmd = next(
                            (c for c in new_step if _local(c.tag) == "cmd"),
                            None,
                        )
                        if new_cmd is not None:
                            new_step_cmd_by_anchor[anchor_xpath] = new_cmd
                if outcome is None:
                    # Conservative default: kind not in _HANDLED_INSERT_KINDS.
                    # Don't risk producing wrong XML — surface for review.
                    article_kind = (
                        getattr(article_block, "kind", None)
                        if article_block is not None
                        else None
                    )
                    report.results.append(
                        PatchResult(
                            op=op,
                            category=ResultCategory.DETECTED,
                            reason=(
                                f"INSERT not auto-applied: article block "
                                f"kind '{article_kind}' has no DITA builder "
                                "yet; review manually"
                            ),
                        )
                    )
                    continue

                result, was_applied = outcome

                # If the article block contained an inline <img> / icon
                # and the INSERT applied, the icon itself is NOT in the
                # patched DITA (we don't know the asset href).
                # Attach a warning so the writer knows to drop the
                # <image> element back in.
                if (
                    was_applied
                    and article_block is not None
                    and getattr(article_block, "has_inline_image", False)
                ):
                    icon_warning = (
                        "the article showed an inline icon / image in this "
                        "step — the text was inserted but the <image> "
                        "element was NOT added (we don't know the matching "
                        "DITA asset href). Add the icon by hand."
                    )
                    if result.warning:
                        result = PatchResult(
                            op=result.op,
                            category=result.category,
                            reason=result.reason,
                            warning=result.warning + "; " + icon_warning,
                            code_snippet=result.code_snippet,
                            topic_id=result.topic_id,
                        )
                    else:
                        result = PatchResult(
                            op=result.op,
                            category=result.category,
                            reason=result.reason,
                            warning=icon_warning,
                            code_snippet=result.code_snippet,
                            topic_id=result.topic_id,
                        )

                report.results.append(result)
                if was_applied:
                    applied_in_topic += 1

        if applied_in_topic > 0:
            output_path = _resolve_output_path(topic_ops[0], output_dir)
            # Always write to disk — validation and track-changes both
            # need the file present. In dry-run mode the server deletes
            # these after the report is generated; see _handle_run in
            # server.py. The `dry_run` kwarg is kept on apply_ops for
            # API compatibility but no longer changes write behavior.
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _write_xml(tree, xml_decl, doctype, output_path)
            report.files_written.append(output_path)

    # Consolidation passes: one card per affected table (regardless of
    # whether the refused ops are DELETEs, REPLACEs, or INSERTs), and
    # one card per topic with feature-note refusals. Both shapes were
    # producing 5+ near-identical needs-review cards in beta feedback —
    # the writer sees the same action (look at this by hand) repeated.
    _consolidate_table_refusals(report)
    _consolidate_feature_note_refusals(report)

    # Orphan-content check: surface SKIPPED structural REPLACEs whose
    # source text has no near match in the article. Those usually mean
    # the article either removed or fully rewrote the content — the
    # safety layer correctly refused to mutate the DITA, but without
    # this pass the user never learns that the element is stale.
    _surface_orphan_sources(report, article_blocks)

    # Media advisory: the article parser doesn't reliably capture
    # <image>, video embeds, or other linked assets. We never auto-diff
    # them, but we DO surface a single advisory listing the source
    # topics that contain media so the writer knows to verify each one
    # against the live article.
    _surface_media_for_verification(report, publication)

    # Consolidated reltable reminder — emitted ONLY when:
    #   (a) The article has a reltable section, AND
    #   (b) Either the .ditamap has no reltable at all, OR the article
    #       has at least one link whose display text isn't present
    #       in the .ditamap's reltable entries.
    #
    # Beta surfaced that the prior unconditional reminder created
    # noise — Beth made no map changes but the report told her to
    # "make sure the <relcell> entries match," implying action was
    # needed. The specific stale-href advisories from the reltable
    # scanner above now cover the actionable cases; this blanket
    # reminder fires only when there's a true mismatch in coverage
    # (or the .ditamap is missing the reltable entirely).
    if reltable_first_op is not None:
        article_navtitles = set()
        if article_blocks is not None:
            in_reltable_section = False
            for ab in article_blocks:
                text = (getattr(ab, "text", "") or "").strip().lower()
                if text in (
                    "related task", "related tasks",
                    "related topic", "related topics",
                    "related article", "related articles",
                    "related link", "related links",
                    "learn more", "learn more about",
                    "see also",
                ):
                    in_reltable_section = True
                    continue
                if not in_reltable_section:
                    continue
                for link_text, _href in (getattr(ab, "links", None) or []):
                    if link_text:
                        article_navtitles.add(
                            normalize_for_match(link_text)
                        )
        ditamap_navtitles = {
            normalize_for_match(e.navtitle)
            for e in (reltable_entries or [])
            if e.navtitle
        }
        article_has_items_not_in_map = bool(
            article_navtitles - ditamap_navtitles
        )
        ditamap_missing_reltable = not ditamap_navtitles
        if article_has_items_not_in_map or ditamap_missing_reltable:
            sections_phrase = (
                " / ".join(sorted(reltable_sections))
                if reltable_sections
                else "Related tasks / Learn more / See also"
            )
            report.results.append(
                PatchResult(
                    op=reltable_first_op,
                    category=ResultCategory.MAP_EDIT,
                    reason=(
                        f"Review the <reltable> in your .ditamap. The "
                        f"article has a {sections_phrase} section with "
                        "items that aren't in the .ditamap's reltable — "
                        "add the missing <topicref> entries by hand."
                    ),
                )
            )
        else:
            # Even when there's no actionable mismatch, write a quiet
            # acknowledgment so the writer can see that the tool
            # noticed the section and reviewed it. Without this, every
            # INSERT/REPLACE in the reltable region gets silently
            # consumed by the consolidation above and vanishes from
            # the report — exactly the "tool dropped my reltable
            # content with no trace" bug the regression sweep
            # surfaced on the Event-articles runs.
            sections_phrase = (
                " / ".join(sorted(reltable_sections))
                if reltable_sections
                else "Related tasks / Learn more / See also"
            )
            count_word = (
                f"{reltable_item_count} item{'s' if reltable_item_count != 1 else ''}"
                if reltable_item_count > 0
                else "the items"
            )
            # MAP_EDIT category routes this to the Map updates section.
            # It used to be DETECTED, which surfaced the card under
            # "Add manually" inside a topic and confused writers — the
            # reason text says "no action needed" but the writer-facing
            # rewriter then said "review this change manually," a flat
            # contradiction. Map updates is the right section for any
            # reltable-related entry, even a no-action confirmation.
            report.results.append(
                PatchResult(
                    op=reltable_first_op,
                    category=ResultCategory.MAP_EDIT,
                    reason=(
                        f"Reltable section reviewed: the article's "
                        f"{sections_phrase} section was detected and "
                        f"{count_word} matched the .ditamap reltable. "
                        "No action needed — listed here so you can confirm "
                        "the match."
                    ),
                )
            )

    return report


def _reltable_topicref_snippet(*, navtitle: str, section: str) -> str:
    """Build a copy-pasteable <topicref> for a reltable <relcell>.

    We don't know the href (the article HTML's link wasn't captured),
    so we leave a clear placeholder for the user to fill in.
    """
    safe_title = html_lib.escape(navtitle, quote=False)
    safe_section = html_lib.escape(section, quote=False)
    return (
        f'<!-- add inside <relcell> for column type="{safe_section}" -->\n'
        f'<topicref href="REPLACE_WITH_URL" format="html" scope="external">\n'
        f'  <topicmeta>\n'
        f'    <navtitle>{safe_title}</navtitle>\n'
        f'  </topicmeta>\n'
        f'</topicref>'
    )


# --- INSERT dispatch (article block kind → DITA element shape) --------- #
#
# Every article kind the parser can emit needs a corresponding builder
# in _INSERT_BUILDERS. When the parser grows a new kind, this set must
# grow too — kinds not in the set route to DETECTED with a clear reason
# rather than silently producing wrong XML (which is what the old
# permissive default did).

# Kinds the INSERT dispatcher can build.
#
# Coverage notes (current state):
#   ✓ paragraph        → <p>           (general body; IM page 116)
#   ✓ note             → <note type=…> (any topic; IM page 130)
#   ✓ list_item        → <li> in <ul>  (general; IM page 119)
#   ✓ step             → <step><cmd>   in <steps> for task topics;
#                        <li> in <ol> as fallback (IM pages 45–55)
#   ✓ unordered_step   → <step><cmd>   in <steps-unordered> for task
#                        topics (IM page 48). NOTE: parser does not
#                        currently emit this kind — the article HTML
#                        doesn't distinguish "bulleted task steps" from
#                        "general bullet list." Wired through for
#                        symmetry; will activate when the parser learns
#                        to detect a checklist signal.
#   ✓ table_row        → <row><entry>… (any topic; IM page 134)
#
# NOT YET HANDLED — these route to DETECTED:
#   - dlentry          (definition lists; INSERT requires dt+dd from
#                       different article structures, hard to infer)
#   - fig / image      (figures; requires image asset handling)
#   - codeblock        (code examples; parser doesn't recognize <pre>)
#   - section          (section headings inside a body)
#   - example          (example blocks)
_HANDLED_INSERT_KINDS = {
    "paragraph", "note", "list_item", "step", "unordered_step",
    "table_row", "heading", None,
}

_BODY_TAGS = ("conbody", "refbody", "body", "taskbody")


def _link_warning_text(unplaced: int) -> str:
    if unplaced > 0:
        return (
            f"{unplaced} link(s) detected in the article could not be "
            "placed (text not found in block); add the <xref> manually"
        )
    return ""


def _insert_paragraph(
    anchor_elem, parent, idx, text, links, root=None,
) -> Tuple[ResultCategory, str]:
    # Always emit <p> for a paragraph-kind block. Using anchor_elem.tag
    # here is wrong: when the anchor is a <note>/<title>/<entry>/<cmd>,
    # we would clone that tag for the new sibling — producing untyped
    # <note> elements after a <note type="important">, etc. <p> is the
    # universally-valid block default per IM page 67.
    new_elem, unplaced = _build_inline_element("p", text, links)
    # If the anchor sits inside a <ul>/<ol>, a <p> at that level would
    # be invalid (lists allow only <li>). Walk up to the list element
    # and insert the new <p> as a sibling of the whole list instead.
    if _local(parent.tag) in ("ul", "ol") and root is not None:
        list_parent = _find_parent_of(root, parent)
        if list_parent is not None:
            list_idx = list(list_parent).index(parent)
            new_elem.tail = parent.tail
            list_parent.insert(list_idx + 1, new_elem)
            return ResultCategory.APPLIED, _link_warning_text(unplaced)
    new_elem.tail = anchor_elem.tail
    parent.insert(idx + 1, new_elem)
    return ResultCategory.APPLIED, _link_warning_text(unplaced)


def _insert_note_handler(
    anchor_elem, parent, idx, text, links, article_block,
) -> Tuple[ResultCategory, str]:
    note_kind = getattr(article_block, "note_kind", None) if article_block else None
    note_bullets = (
        getattr(article_block, "note_bullets", None) if article_block else None
    )
    new_note, unplaced = _build_note_element(
        note_kind, text, links, note_bullets=note_bullets,
    )

    # Task topic placement: per the task DTD, <step> may only contain
    # <cmd>, <info>, <stepxmp>, <stepresult>, etc. — NOT <note>
    # directly. When the anchor is the step's <cmd>, route the new
    # <note> into the step's <info>, creating <info> if it doesn't
    # exist yet.
    if _local(parent.tag) == "step":
        info_el = next(
            (c for c in parent if _local(c.tag) == "info"), None
        )
        if info_el is None:
            info_el = ET.Element("info")
            cmd_idx = next(
                (i for i, c in enumerate(parent) if _local(c.tag) == "cmd"),
                0,
            )
            parent.insert(cmd_idx + 1, info_el)
        info_el.append(new_note)
        return ResultCategory.APPLIED, _link_warning_text(unplaced)

    new_note.tail = anchor_elem.tail
    parent.insert(idx + 1, new_note)
    return ResultCategory.APPLIED, _link_warning_text(unplaced)


def _insert_li_into_list(
    root, anchor_elem, parent, idx, text, links, prefer_tag: str, emphasis=None,
) -> Tuple[ResultCategory, str]:
    """Shared logic for <li> insertion.

    Strategy:
      0. If the article text matches "Term: Description" (per IM
         page 122-123) AND `prefer_tag` is "ul" (unordered context —
         we don't reinterpret ordered steps as definitions), build a
         <dlentry> in a <dl> instead of <li> in <ul>. The existing
         dl-target dispatch handles the case where the anchor is
         already inside a <dl>; this path handles the case where the
         source has no <dl> yet but the article content is clearly
         a definition list.
      1. If anchor's parent is already <ol>/<ul>, add a new <li> sibling.
      2. If the next sibling is an <ol>/<ul>, prepend a <li> there
         (reverse-op-order tiebreak puts earlier items first).
      3. Otherwise create a fresh list of `prefer_tag`.
    """
    parent_tag = _local(parent.tag)

    if prefer_tag == "ul":
        split = _split_term_definition(text)
        if split is not None:
            return _insert_or_extend_new_dl(
                anchor_elem, parent, idx, split, links, root=root,
            )

    if parent_tag in ("ol", "ul"):
        li_el, unplaced = _build_inline_element("li", text, links, emphasis=emphasis)
        parent.insert(idx + 1, li_el)
        return ResultCategory.APPLIED, _link_warning_text(unplaced)

    sibling = parent[idx + 1] if idx + 1 < len(parent) else None
    if sibling is not None and _local(sibling.tag) in ("ol", "ul"):
        li_el, unplaced = _build_inline_element("li", text, links, emphasis=emphasis)
        sibling.insert(0, li_el)
        return ResultCategory.APPLIED, _link_warning_text(unplaced)

    # In a task topic, when the parent is <taskbody> and a <prereq>
    # exists with a <ul>/<ol> already, route the new <li> into that
    # existing list. Without this, an INSERT anchored at the topic
    # <title> (which the title-anchor redirect lands at taskbody
    # position 0) creates a fresh <ul> as a SIBLING of <prereq> —
    # invalid DITA structure (taskbody allows <prereq> but a bare
    # <ul> belongs INSIDE it). Beta surfaced this on before_you_begin.
    if parent_tag in ("taskbody", "conbody", "refbody"):
        prereq_elem = next(
            (c for c in parent if _local(c.tag) == "prereq"),
            None,
        )
        if prereq_elem is not None:
            existing_list = next(
                (
                    c for c in prereq_elem
                    if _local(c.tag) == prefer_tag
                ),
                None,
            )
            if existing_list is not None:
                li_el, unplaced = _build_inline_element(
                    "li", text, links, emphasis=emphasis,
                )
                existing_list.insert(0, li_el)
                return ResultCategory.APPLIED, _link_warning_text(unplaced)
            # No existing list in prereq — create one inside prereq.
            list_el = ET.Element(prefer_tag)
            li_el, unplaced = _build_inline_element(
                "li", text, links, emphasis=emphasis,
            )
            list_el.append(li_el)
            prereq_elem.append(list_el)
            return ResultCategory.APPLIED, _link_warning_text(unplaced)

    # Anchor is inside a task <step> (cmd is its required first child).
    # Per the task DTD, <step> only allows <cmd>, then <info>/<stepxmp>/…
    # — a bare <ul> as a sibling of <cmd> is invalid. Route the new list
    # into the step's <info>, creating <info> if it doesn't exist.
    if parent_tag == "step":
        info_el = next(
            (c for c in parent if _local(c.tag) == "info"), None
        )
        if info_el is None:
            info_el = ET.Element("info")
            # <info> must come after <cmd>. Insert right after cmd.
            cmd_idx = next(
                (i for i, c in enumerate(parent) if _local(c.tag) == "cmd"),
                0,
            )
            parent.insert(cmd_idx + 1, info_el)
        existing_list = next(
            (c for c in info_el if _local(c.tag) in ("ol", "ul")), None
        )
        if existing_list is not None and _local(existing_list.tag) == prefer_tag:
            li_el, unplaced = _build_inline_element("li", text, links, emphasis=emphasis)
            # INSERTs at the same anchor are processed in reverse article
            # order (tail-first). Each `insert(0, ...)` pushes the
            # previous-inserted item to position 1 — net result is
            # forward article order. `append` would reverse them.
            existing_list.insert(0, li_el)
            return ResultCategory.APPLIED, _link_warning_text(unplaced)
        list_el = ET.Element(prefer_tag)
        li_el, unplaced = _build_inline_element("li", text, links, emphasis=emphasis)
        list_el.append(li_el)
        info_el.append(list_el)
        return ResultCategory.APPLIED, _link_warning_text(unplaced)

    list_el = ET.Element(prefer_tag)
    li_el, unplaced = _build_inline_element("li", text, links, emphasis=emphasis)
    list_el.append(li_el)
    list_el.tail = anchor_elem.tail
    parent.insert(idx + 1, list_el)
    return ResultCategory.APPLIED, _link_warning_text(unplaced)


def _build_new_dlentry(term: str, definition: str, links):
    new_entry = ET.Element("dlentry")
    dt = ET.SubElement(new_entry, "dt")
    dt.text = term
    dd, _unplaced = _build_inline_element("dd", definition, links)
    new_entry.append(dd)
    return new_entry


def _insert_or_extend_new_dl(
    anchor_elem, parent, idx, term_def, links, root=None,
) -> Tuple[ResultCategory, str]:
    """Insert a Term: Description bullet as a <dlentry> in a freshly
    created (or recently-created sibling) <dl>.

    Same prepend pattern as <ul><li> insertion: when multiple bullets
    target the same anchor in one op batch, reverse op-order plus
    prepend produces the original op order in the final document.

    Per IM page 122-123:
      • <dt> is a short noun phrase, no trailing colon.
      • <dd> holds the description.
      • <dlentry> wraps both.

    Task-topic placement (IM step content model): <dl> can only live
    inside <info>, <stepresult>, etc. — not as a direct child of <step>.
    When the anchor's parent is <step> (anchor is typically <cmd>),
    place the new <dl> inside the step's <info> element instead, and
    create the <info> if it doesn't exist yet.
    """
    term, definition = term_def

    # Task-topic placement guard. If the would-be parent doesn't allow
    # <dl>, route into a step's <info> block instead.
    parent_tag = _local(parent.tag)
    if parent_tag in ("step", "steps", "steps-unordered"):
        if root is not None:
            placed, msg = _place_dlentry_in_step_info(
                root, anchor_elem, term, definition, links,
            )
            if placed:
                return ResultCategory.APPLIED, msg

    # Reuse a recently-created sibling <dl> if present (multi-bullet batch).
    sibling = parent[idx + 1] if idx + 1 < len(parent) else None
    if sibling is not None and _local(sibling.tag) == "dl":
        sibling.insert(0, _build_new_dlentry(term, definition, links))
        return ResultCategory.APPLIED, (
            f'added <dlentry> (term: "{term}") to a newly-created '
            "<dl> sibling. Per IM page 123 verify the term is a short "
            "noun phrase; if the bullets aren't really definitions, "
            "change the <dl> to <ul>."
        )

    # Fresh <dl> right after the anchor.
    new_dl = ET.Element("dl")
    new_dl.append(_build_new_dlentry(term, definition, links))
    new_dl.tail = anchor_elem.tail
    parent.insert(idx + 1, new_dl)
    return ResultCategory.APPLIED, (
        f'created a new <dl> with one <dlentry> (term: "{term}"); the '
        "article bullet matches a Term: Description pattern. If this "
        "content is really a flat list, change <dl> to <ul>. Per "
        "IM_list02, a <dl> with only one <dlentry> will flag in the "
        "Style and structure check — add more entries or convert to <ul>."
    )


def _place_dlentry_in_step_info(
    root, anchor_elem, term: str, definition: str, links,
) -> Tuple[bool, str]:
    """Place a new <dlentry> inside the enclosing <step>'s <info>.

    Per the DITA task DTD: <step> contains <cmd> first, then optional
    <info> (multiple allowed) before <stepxmp> / <stepresult> / etc.
    <dl> at the step level is invalid — it must live inside <info>.

    Returns (True, warning) when placed; (False, "") when no enclosing
    <step> could be found (caller falls back to default placement).
    """
    step = _find_ancestor_with_tag(root, anchor_elem, "step")
    if step is None:
        # Try a steps-unordered <step>.
        step = anchor_elem if _local(anchor_elem.tag) == "step" else None
    if step is None:
        return False, ""

    # Locate or create <info> inside <step>. <info> must come after
    # <cmd>. If a <dl> already exists inside any info, prepend the
    # new <dlentry> to it.
    info_with_dl = None
    target_info = None
    for child in step:
        if _local(child.tag) == "info":
            target_info = child  # the first info we see
            for grandchild in child:
                if _local(grandchild.tag) == "dl":
                    info_with_dl = grandchild
                    break
            if info_with_dl is not None:
                break

    if info_with_dl is not None:
        info_with_dl.insert(0, _build_new_dlentry(term, definition, links))
        return True, (
            f'added <dlentry> (term: "{term}") to the existing <dl> '
            f"inside this step's <info>."
        )

    if target_info is None:
        # Create a fresh <info> right after <cmd>.
        cmd_idx = 0
        for i, child in enumerate(step):
            if _local(child.tag) == "cmd":
                cmd_idx = i
        target_info = ET.Element("info")
        step.insert(cmd_idx + 1, target_info)

    new_dl = ET.Element("dl")
    new_dl.append(_build_new_dlentry(term, definition, links))
    target_info.append(new_dl)
    return True, (
        f'created a new <dl> with one <dlentry> (term: "{term}") inside '
        "this step's <info> element (per IM task content model — <dl> "
        "can't be a direct child of <step>). If this is really a flat "
        "list, change <dl> to <ul>."
    )


_STEP_OPTIONAL_PREFIX_RE = re.compile(r"^Optional\s*:\s*", re.IGNORECASE)


def _strip_optional_prefix(text: str) -> Tuple[str, bool]:
    """Detect and remove the Help Center "Optional:" prefix from a step
    text. Returns `(stripped_text, was_optional)`. Per IM, optional
    steps use `<cmd importance="optional">`; the "Optional:" label is
    rendered by the stylesheet, not stored in the cmd text itself.
    """
    if _STEP_OPTIONAL_PREFIX_RE.match(text):
        return _STEP_OPTIONAL_PREFIX_RE.sub("", text, count=1).lstrip(), True
    return text, False


def _insert_step_handler(
    root, anchor_elem, parent, idx, text, links, emphasis=None,
) -> Tuple[ResultCategory, str]:
    """Numbered ordered-list-item from the article → DITA step.

    Routing rules — in priority order:
      1. If the anchor sits inside an existing <step>, insert the new
         <step> after it (sibling within the same <steps>).
      2. If the anchor sits anywhere inside a <taskbody> (e.g. on the
         topic <title> when the new steps land BEFORE all existing
         steps), find or create the <steps> element in that taskbody
         and insert at position 0. The reverse-order INSERT invariant
         preserves forward article order across multiple INSERTs at
         the same anchor.
      3. If the topic is a concept/reference (no <taskbody>), fall
         back to <ol><li> so the output stays DITA-valid for that
         topic type.

    Earlier this routing only handled case (1) and fell straight to
    (3), which produced an <ol> as a sibling of <steps> inside a
    <taskbody> — invalid DITA (taskbody doesn't permit <ol> as a
    child). Beta surfaced this as the "added <ol> with new steps,
    above the <steps> element" failure.

    Per IM, an "Optional:" prefix on the article-side step is encoded
    as `<step importance="optional">` (not `<cmd importance>`) so the
    stylesheet renders the "Optional:" label consistently.
    """
    text, is_optional = _strip_optional_prefix(text)

    # Case 1: anchor is inside an existing <step> — insert after it.
    step_ancestor = _find_ancestor_with_tag(root, anchor_elem, "step")
    if step_ancestor is not None:
        steps_parent = _find_parent_of(root, step_ancestor)
        if steps_parent is not None and _local(steps_parent.tag) == "steps":
            step_idx = list(steps_parent).index(step_ancestor)
            new_step = ET.Element("step")
            if is_optional:
                new_step.set("importance", "optional")
            new_cmd, unplaced = _build_inline_element(
                "cmd", text, links, emphasis=emphasis,
            )
            new_step.append(new_cmd)
            new_step.tail = step_ancestor.tail
            steps_parent.insert(step_idx + 1, new_step)
            return ResultCategory.APPLIED, _link_warning_text(unplaced)

    # Case 2: anchor is inside (or is) a <taskbody> — find or create
    # the <steps> element and prepend. Without this, an article that
    # inserts a new step BEFORE the first existing step would route
    # to the fallback <ol> path and emit invalid DITA.
    taskbody_ancestor = _find_ancestor_with_tag(root, anchor_elem, "taskbody")
    if taskbody_ancestor is None and _local(anchor_elem.tag) == "taskbody":
        taskbody_ancestor = anchor_elem
    if taskbody_ancestor is not None:
        steps_elem = next(
            (
                c for c in taskbody_ancestor.iter()
                if _local(c.tag) == "steps"
            ),
            None,
        )
        if steps_elem is None:
            steps_elem = ET.SubElement(taskbody_ancestor, "steps")
        new_step = ET.Element("step")
        if is_optional:
            new_step.set("importance", "optional")
        new_cmd, unplaced = _build_inline_element(
            "cmd", text, links, emphasis=emphasis,
        )
        new_step.append(new_cmd)
        # Position 0 + the reverse-order INSERT invariant keeps
        # multiple INSERTs at the same anchor in their original
        # article order.
        steps_elem.insert(0, new_step)
        return ResultCategory.APPLIED, _link_warning_text(unplaced)

    # Case 3: non-task topic — fall back to <ol><li>.
    return _insert_li_into_list(
        root, anchor_elem, parent, idx, text, links, "ol", emphasis=emphasis,
    )


def _insert_list_item_handler(
    root, anchor_elem, parent, idx, text, links, emphasis=None,
) -> Tuple[ResultCategory, str]:
    return _insert_li_into_list(
        root, anchor_elem, parent, idx, text, links, "ul", emphasis=emphasis,
    )


def _insert_unordered_step_handler(
    root, anchor_elem, parent, idx, text, links, emphasis=None,
) -> Tuple[ResultCategory, str]:
    """Bulleted action item in a task topic → <step><cmd> in
    <steps-unordered>.

    Per IM page 48: task topics use <steps-unordered> for unordered
    action sequences (and may add outputclass="checklist" per IM_T10).
    Detection of "task topic" walks up to find a <steps> or
    <steps-unordered> ancestor or a <taskbody> ancestor. If none is
    found, falls back to inserting as a regular <li> in <ul>.
    """
    # Term: Description bullets become a <dlentry> in step/info even in
    # task topics — they describe a thing, they are not an action. Per
    # IM page 122-123.
    split = _split_term_definition(text)
    if split is not None and _find_ancestor_with_tag(root, anchor_elem, "taskbody") is not None:
        return _insert_or_extend_new_dl(
            anchor_elem, parent, idx, split, links, root=root,
        )

    # When the anchor is already inside a <ul>/<ol> (e.g. a bullet
    # list in <prereq>, <conbody>, or <context>), the new bullet
    # belongs in that same list as a sibling <li>. Don't promote it
    # into <steps-unordered> just because the topic is a task.
    # Beta surfaced this on before_you_begin.dita where new article
    # bullets were ending up in <steps-unordered> instead of the
    # existing <prereq>/<ul>.
    list_ancestor = _find_ancestor_with_tag(root, anchor_elem, "ul")
    if list_ancestor is None:
        list_ancestor = _find_ancestor_with_tag(root, anchor_elem, "ol")
    if list_ancestor is not None:
        return _insert_li_into_list(
            root, anchor_elem, parent, idx, text, links, "ul",
            emphasis=emphasis,
        )

    # First, prefer extending an existing <steps-unordered>.
    su_ancestor = _find_ancestor_with_tag(root, anchor_elem, "steps-unordered")
    if su_ancestor is not None:
        new_step = ET.Element("step")
        new_cmd, unplaced = _build_inline_element(
            "cmd", text, links, emphasis=emphasis,
        )
        new_step.append(new_cmd)
        su_ancestor.append(new_step)
        return ResultCategory.APPLIED, _link_warning_text(unplaced)

    # Otherwise: are we in a task topic? If so, create a fresh
    # <steps-unordered> as a sibling of the anchor.
    if _find_ancestor_with_tag(root, anchor_elem, "taskbody") is not None:
        su_el = ET.Element("steps-unordered")
        new_step = ET.Element("step")
        new_cmd, unplaced = _build_inline_element(
            "cmd", text, links, emphasis=emphasis,
        )
        new_step.append(new_cmd)
        su_el.append(new_step)
        su_el.tail = anchor_elem.tail
        parent.insert(idx + 1, su_el)
        return ResultCategory.APPLIED, _link_warning_text(unplaced)

    # Non-task topic — fall back to <ul><li>.
    return _insert_li_into_list(
        root, anchor_elem, parent, idx, text, links, "ul",
    )


def _insert_heading_handler(
    root, anchor_elem, parent, idx, text, links,
) -> Tuple[ResultCategory, str]:
    """Article <h2>/<h3> → DITA heading element.

    Per IM:
      • Concept / Reference body → <section><title>…</title></section>
        (allowed in <conbody>, <refbody>, <body>).
      • Task body → <stepsection> when inside <steps> (IM page 45),
        plain <p> otherwise. The IM doesn't bless outputclass="heading"
        as an authoring pattern.
    """
    # Task topic: use <stepsection> per IM page 45 whenever the topic
    # has a <steps> element. If the anchor is inside a <step>, the
    # stepsection goes immediately after that step (mid-procedure
    # heading). If the anchor is anywhere else in the task body
    # (title, first cmd, redirected from navtitle), the stepsection
    # is the stem sentence at the START of <steps>.
    taskbody = _find_ancestor_with_tag(root, anchor_elem, "taskbody")
    if taskbody is not None:
        # Find any <steps> in this taskbody (not just an ancestor).
        steps_in_body = None
        for child in taskbody.iter():
            if _local(child.tag) == "steps":
                steps_in_body = child
                break
        if steps_in_body is not None:
            # Per IM page 123 (rule shared with <dt>): the stylesheet
            # adds the trailing colon. Strip it from the source text.
            ss_text = text.rstrip().rstrip(":").rstrip()
            ss_el = ET.Element("stepsection")
            ss_el.text = ss_text
            step_ancestor = _find_ancestor_with_tag(root, anchor_elem, "step")
            if step_ancestor is not None and step_ancestor in list(steps_in_body):
                step_idx = list(steps_in_body).index(step_ancestor)
                if step_idx > 0:
                    # Mid-procedure heading — convention is to split
                    # into a new <steps> with its own <stepsection>,
                    # not to insert a divider between existing steps.
                    # Route to manual review so the writer can split
                    # the procedure properly.
                    return ResultCategory.DETECTED, (
                        f'article has a mid-procedure heading: '
                        f'"{text.rstrip(":").rstrip()}". By convention, '
                        "split this into a new <steps> element with the "
                        "heading as its <stepsection>, then move the "
                        "steps after the heading into the new <steps>. "
                        "The tool refuses to insert a <stepsection> "
                        "between existing steps in the same <steps>."
                    )
            # Stem sentence (anchor is the first step or outside <steps>):
            # stepsection goes at the START of <steps>.
            steps_in_body.insert(0, ss_el)
            return ResultCategory.APPLIED, (
                "inserted as <stepsection> at the start of <steps> "
                "(stem sentence) per IM page 45. Trailing colon stripped "
                "— the stylesheet adds it automatically. Verify the "
                "placement."
            )
        # No <steps> in this taskbody: plain <p> is the most
        # IM-compliant fallback.
        p_el, _unplaced = _build_inline_element("p", text, links)
        p_el.tail = anchor_elem.tail
        parent.insert(idx + 1, p_el)
        return ResultCategory.APPLIED, (
            "task topics without <steps> don't have a heading element "
            "in the IM; inserted as a plain <p>. If this should be a "
            "<stepsection>, add a <steps> block and move this in."
        )

    # Concept/reference/body topic: <section><title>.
    section_parent = parent
    section_idx = idx
    cursor = anchor_elem
    cursor_parent = parent
    # If any ancestor is itself a <section>, the new section should be
    # inserted as a sibling of THAT section, not nested inside it.
    while cursor_parent is not None and _local(cursor_parent.tag) == "section":
        cursor = cursor_parent
        cursor_parent = _find_parent_of(root, cursor)
        if cursor_parent is None:
            break
    if cursor is not anchor_elem and cursor_parent is not None:
        section_parent = cursor_parent
        section_idx = list(cursor_parent).index(cursor)

    sec_el = ET.Element("section")
    title_el = ET.SubElement(sec_el, "title")
    inline_title, _unplaced = _build_inline_element("title", text, links)
    # Use the inline-built element's text and children to populate <title>.
    title_el.text = inline_title.text
    for child in list(inline_title):
        title_el.append(child)
    sec_el.tail = anchor_elem.tail
    section_parent.insert(section_idx + 1, sec_el)
    return ResultCategory.APPLIED, ""


def _find_dl_insert_target(root, anchor_elem):
    """If `anchor_elem` lies inside a <dl>, return the (dl, dlentry)
    pair we should insert AFTER. None if the anchor isn't in a dl.

    Handles three anchor positions:
      • anchor is the <dl> itself                        → no-op (None)
      • anchor is a <dlentry>                            → use it directly
      • anchor is deeper (<dt>/<dd>/<p>/etc.)            → walk up to the
        nearest <dlentry> and use that.
    """
    if anchor_elem is None or _local(anchor_elem.tag) == "dl":
        return None

    # Walk up using _find_parent_of repeatedly to find the dlentry that
    # contains the anchor (anchor may already be a dlentry).
    current = anchor_elem
    while current is not None:
        if _local(current.tag) == "dlentry":
            dl_parent = _find_parent_of(root, current)
            if dl_parent is not None and _local(dl_parent.tag) == "dl":
                return (dl_parent, current)
            return None
        current = _find_parent_of(root, current)
    return None


# Order matters: longer/more-specific separators first so we don't split
# a "Term — Description" line on the space-hyphen-space inside the dash.
_DL_TERM_SEPARATORS = (": ", " — ", " – ", " - ")

# Per IM_list11 (dropped from the XSLT-1 Schematron but still in the IM):
# do not put trailing punctuation on a <dt>. Stylesheet adds the colon
# automatically, so the authored term must end on a word character.
_DT_TRAILING_PUNCT = ".:;,—–-"

# Per IM page 123: <dt> must be a short noun phrase, "should not require
# more than one line in the final output." Anything longer than this
# cap is probably not a real definition-list term — could be a sentence
# containing a colon for some other reason. Route to DETECTED so the
# writer decides.
_DT_MAX_LENGTH = 60


_DL_FALSE_POSITIVE_TERMS = frozenset({
    # Help Center renders DITA <note> elements with a "Note: " label.
    # The article parser surfaces that prefix as part of the block text,
    # which would otherwise match the Term:Definition pattern and emit
    # a <dlentry><dt>Note</dt><dd>...</dd></dlentry> instead of a
    # proper <note>. These prefixes never indicate a glossary entry.
    "note", "tip", "warning", "caution", "important", "remember",
    "attention", "danger", "fastpath", "restriction", "trouble",
})


def _split_term_definition(text: str) -> Optional[Tuple[str, str]]:
    """Split 'Term: Definition' (or em-dash variants) into (term, def).

    Returns None when no recognised separator appears OR when the
    resulting term violates IM constraints (empty after stripping
    trailing punctuation, or longer than a short noun phrase). The
    caller routes those to DETECTED so the writer hand-places a
    valid <dlentry>.
    """
    for sep in _DL_TERM_SEPARATORS:
        idx = text.find(sep)
        if idx <= 0:  # also rejects sep at position 0 (empty term)
            continue
        term = text[:idx].strip()
        # IM_list11: trim trailing punctuation off the term.
        term = term.rstrip(_DT_TRAILING_PUNCT).rstrip()
        definition = text[idx + len(sep):].strip()
        if not term or not definition:
            continue
        # IM page 123: <dt> must be a short noun phrase.
        if len(term) > _DT_MAX_LENGTH:
            continue
        # Reject Help-Center note-style prefixes ("Note: ...", "Tip: ...").
        # Those are inline notes the article renders with a label prefix,
        # not glossary entries.
        if term.lower() in _DL_FALSE_POSITIVE_TERMS:
            continue
        return term, definition
    return None


def _dlentry_split_is_clean(term: str, definition: str) -> bool:
    """True when the term/description split is clean enough to skip
    the verify warning. Beta feedback (2026-06-24): every dlentry
    INSERT was getting flagged, even obvious cases like
    "Google Pay: We accept Google Pay as a payment method…" where
    the term is unambiguously a 2-word noun phrase.

    Risky cases (warning still fires):
    - Term too long (>6 words) → might really be a sentence
    - Term ends with sentence-ending punctuation → likely mis-split
    - Term contains a stray internal colon → split point was wrong
    - Description too short (<3 words) → split may have lost content
    - Description empty → broken split
    """
    term = (term or "").strip()
    definition = (definition or "").strip()
    if not term or not definition:
        return False
    if len(term.split()) > 6:
        return False
    if ":" in term:
        return False
    if term[-1:] in {".", "!", "?"}:
        return False
    if len(definition.split()) < 3:
        return False
    return True


def _insert_dlentry_handler(
    anchor_dlentry, dl_parent, idx, text, links,
) -> Tuple[ResultCategory, str]:
    """Insert a new <dlentry> into a <dl> built from 'Term: Definition'.

    Per IM page 122-123:
      • <dt> is a short noun phrase, no colon (the stylesheet adds the
        colon visually)
      • <dd> holds the description; for fragments leave out the <p>
        wrapper, for sentences include one
      • <dt> must precede <dd> within the <dlentry>
    """
    split = _split_term_definition(text)
    if split is None:
        return ResultCategory.DETECTED, (
            "the anchor lives inside a <dl>, but the new article text "
            "doesn't have a clear term/description split (looked for "
            "': ', ' — ', ' – ', ' - '). Add a <dlentry> by hand with "
            "the term in <dt> and the description in <dd>."
        )
    term, definition = split

    new_entry = ET.Element("dlentry")
    dt = ET.SubElement(new_entry, "dt")
    dt.text = term
    # <dd> takes inline content. _build_inline_element handles xref
    # wrapping when the article block had links.
    dd, _unplaced = _build_inline_element("dd", definition, links)
    new_entry.append(dd)
    new_entry.tail = anchor_dlentry.tail
    dl_parent.insert(idx + 1, new_entry)
    if _dlentry_split_is_clean(term, definition):
        return ResultCategory.APPLIED, ""
    return ResultCategory.APPLIED, (
        f'inserted as a new <dlentry> (term: "{term}"); per IM page 123 '
        "the <dt> must be a short noun phrase with no trailing colon — "
        "verify the term is short enough and that the description "
        "belongs in <dd>."
    )


_TABLE_XPATH_PREFIX_RE = re.compile(r"^(.*?/table\[\d+\])")


def _table_xpath_prefix(xpath: Optional[str]) -> Optional[str]:
    """Return the `/topic[1]/.../table[N]` prefix of a row-or-deeper
    xpath, or None when the xpath doesn't point at table content."""
    if not xpath:
        return None
    m = _TABLE_XPATH_PREFIX_RE.match(xpath)
    return m.group(1) if m else None


def _apply_consolidation(
    report: "PatchReport",
    drop_indices: set,
    rewrites: Dict[int, str],
) -> None:
    """Rewrite the surviving result's reason and drop the rest. Shared
    by the table and feature-note consolidation passes."""
    if not drop_indices and not rewrites:
        return
    new_results: List["PatchResult"] = []
    for i, r in enumerate(report.results):
        if i in drop_indices:
            continue
        if i in rewrites:
            new_results.append(_dc_replace(r, reason=rewrites[i]))
        else:
            new_results.append(r)
    report.results = new_results


def _consolidate_table_refusals(report: "PatchReport") -> None:
    """Collapse every SKIPPED entry that targets the same DITA table
    into ONE card. The Premium-subscriptions report surfaced the
    problem: a single 8-column comparison table produced five separate
    needs-review cards — two row DELETEs (alignment), two row REPLACEs
    (cell-count mismatch), and one row INSERT (complex-table guard).
    Each says the same thing to the writer: "look at this table by
    hand." Showing five identical actions inflates the perceived
    workload. We keep the first refused op as the card's anchor and
    swap its reason for a single summary that names the table and
    explains the next step."""
    groups: Dict[Tuple[str, str], List[int]] = {}
    for i, r in enumerate(report.results):
        if r.category != ResultCategory.SKIPPED:
            continue
        op = r.op
        if op is None:
            continue
        block = op.source_block or op.anchor_block
        if block is None:
            continue
        prefix = _table_xpath_prefix(getattr(block, "element_xpath", None))
        if prefix is None:
            continue
        key = (getattr(block, "topic_id", ""), prefix)
        groups.setdefault(key, []).append(i)

    drop_indices: set = set()
    rewrites: Dict[int, str] = {}
    for indices in groups.values():
        if len(indices) <= 1:
            continue  # single refusal — already shows as one card
        keep = indices[0]
        n = len(indices)
        rewrites[keep] = (
            f"complex table — the tool found {n} changes here it couldn't "
            "safely apply (some combination of new rows, reworded cells, "
            "or removed rows). Compare this table to the live article "
            "and update it by hand."
        )
        for drop_at in indices[1:]:
            drop_indices.add(drop_at)
    _apply_consolidation(report, drop_indices, rewrites)


def _consolidate_feature_note_refusals(report: "PatchReport") -> None:
    """Collapse every "feature note" SKIPPED entry into ONE card —
    across all topics. The diff can split a single product callout
    across multiple refusals: a refused DELETE on the source DITA's
    `<note othertype="feature">` in one topic, plus a refused INSERT
    of the article-side feature-launcher block aligned (by LCS) into
    a different topic. To the writer they're the same product promo
    — one card, one action."""
    NEEDLE = "feature note"
    matches: List[int] = []
    for i, r in enumerate(report.results):
        if r.category != ResultCategory.SKIPPED:
            continue
        if not r.reason or NEEDLE not in r.reason:
            continue
        if r.op is None:
            continue
        matches.append(i)
    if len(matches) <= 1:
        return
    keep = matches[0]
    rewrites = {
        keep: (
            "feature note (othertype=\"feature\") — please add or "
            "update this feature note manually. The tool couldn't "
            "apply it."
        )
    }
    drop_indices = set(matches[1:])
    _apply_consolidation(report, drop_indices, rewrites)


def _table_is_complex(row_elem, parent=None) -> bool:
    """True if `row_elem` belongs to a complex table where the tool
    can't safely slot in a new row. Signals:

      1. 3+ columns — multi-column comparison tables (UI labels,
         checkmarks aligned to specific product columns).
      2. Merged cells anywhere in the surrounding rows — CALS
         `@morerows` (vertical merge) or `@namest`/`@nameend`
         (horizontal merge). Inserting a flat row into a table with
         spans would corrupt the merge alignment.

    Two-column tables with no merged cells (simple key/value or
    label/description lookups) stay auto-insertable."""
    entries = [c for c in row_elem if _local(c.tag) == "entry"]
    if len(entries) >= 3:
        return True
    # Walk the surrounding rows in the tbody/thead to detect merges.
    # When `parent` isn't supplied, fall back to scanning only the
    # anchor row — better than nothing.
    rows_to_scan = (
        [r for r in parent if _local(r.tag) == "row"]
        if parent is not None else [row_elem]
    )
    for row in rows_to_scan:
        for entry in row:
            if _local(entry.tag) != "entry":
                continue
            attrib = entry.attrib
            if attrib.get("morerows"):
                return True
            if attrib.get("namest") or attrib.get("nameend"):
                return True
            if attrib.get("spanname"):
                return True
    return False


def _insert_table_row_handler(
    anchor_elem, parent, idx, article_block,
) -> Tuple[ResultCategory, str]:
    """Insert an article <tr> as a DITA <row>.

    Two cases:
      A. Anchor is already a <row> in an existing <table> → add a
         sibling <row> in the same <tbody>.
      B. Anchor is anywhere else AND a sibling <table> already exists
         from a previous insert in this batch → prepend to its <tbody>
         (reverse op-order keeps the original sequence).
      C. Neither → build a fresh <table> per IM page 251:
            <table>
              <tgroup cols="N">
                <colspec ... /> × N
                <tbody>
                  <row><entry>cell</entry>…</row>
                </tbody>
              </tgroup>
            </table>
         The IM also supports <thead>; the article parser doesn't
         distinguish <th> from <td> reliably, so all rows currently
         go into <tbody>. Warning surfaces "move header row to <thead>
         if applicable."
    """
    cells = getattr(article_block, "cells", None) if article_block else None
    if not cells:
        return ResultCategory.SKIPPED, (
            "row INSERT detected but the article parser captured "
            "no cell data; cannot build a structurally valid <row>."
        )
    cell_emphasis = getattr(article_block, "cell_emphasis", None) if article_block else None

    anchor_tag = _local(anchor_elem.tag)

    # Case A: anchor IS a <row>. Add a sibling <row> in the same <tbody>.
    if anchor_tag == "row":
        # Complex-table guard: comparison tables (3+ columns) carry too
        # much per-cell semantics — UI element labels, checkmarks
        # aligned to specific product columns, header rows, etc. —
        # for the tool to slot in a new row without risking misaligned
        # data. Refuse and ask the writer to add manually. Two-column
        # tables (key/value lookups) remain auto-insertable.
        if _table_is_complex(anchor_elem, parent):
            return ResultCategory.SKIPPED, (
                "complex table — we cannot safely add a new row to a "
                "multi-column or merged-cell table. Please add this "
                "row manually."
            )
        new_row = _build_row(cells, cell_emphasis)
        new_row.tail = anchor_elem.tail
        parent.insert(idx + 1, new_row)
        return ResultCategory.APPLIED, (
            "new <row> inserted; bold cell text wrapped in <em>. If "
            "your existing rows wrap the label cell in <uicontrol>, "
            "swap <em> for <uicontrol> manually."
        )

    # Case B: a sibling <table> from a previous batch — prepend to its tbody.
    sibling = parent[idx + 1] if idx + 1 < len(parent) else None
    if sibling is not None and _local(sibling.tag) == "table":
        tbody = _find_descendant_with_tag(sibling, "tbody")
        if tbody is not None:
            tbody.insert(0, _build_row(cells, cell_emphasis))
            return ResultCategory.APPLIED, (
                "added <row> to a newly-created sibling <table>. "
                "Verify column count and whether the first row should "
                "be moved to <thead>."
            )

    # Case C: fresh <table>.
    ncols = len(cells)
    table = ET.Element("table")
    tgroup = ET.SubElement(table, "tgroup", attrib={"cols": str(ncols)})
    # Equal column widths as a sensible default — author tunes later.
    col_width = max(1, 100 // ncols)
    for i in range(ncols):
        ET.SubElement(tgroup, "colspec", attrib={
            "colname": f"c{i + 1}",
            "colnum": str(i + 1),
            "colwidth": f"{col_width}*",
        })
    tbody = ET.SubElement(tgroup, "tbody")
    tbody.append(_build_row(cells, cell_emphasis))
    table.tail = anchor_elem.tail
    parent.insert(idx + 1, table)
    return ResultCategory.APPLIED, (
        f'created a new <table> with {ncols} column(s) per IM page 251. '
        "All rows landed in <tbody>; if the first row is a header, move "
        "it into a <thead> manually. Column widths are equal — adjust "
        "if needed."
    )


def _build_row(cell_texts, cell_emphasis=None):
    """Build a <row> with one <entry> per cell. When per-cell emphasis
    phrases are provided, wrap each matching phrase in <em> inside the
    corresponding <entry>.
    """
    row = ET.Element("row")
    for i, text in enumerate(cell_texts):
        entry = ET.SubElement(row, "entry")
        emphases = (cell_emphasis[i] if cell_emphasis and i < len(cell_emphasis) else [])
        if emphases:
            _populate_entry_with_em_wraps(entry, text, emphases)
        else:
            entry.text = text
    return row


def _populate_entry_with_em_wraps(entry, text: str, em_phrases: List[str]) -> None:
    """Set `entry.text` to `text`, but wrap each phrase in `em_phrases`
    (found in the text in order) in a child <em> element.
    """
    # Find each phrase in left-to-right order; skip if not found or
    # if it would overlap an earlier wrap.
    spans = []
    cursor = 0
    for phrase in em_phrases:
        if not phrase:
            continue
        idx = text.find(phrase, cursor)
        if idx < 0:
            continue
        spans.append((idx, idx + len(phrase), phrase))
        cursor = idx + len(phrase)

    if not spans:
        entry.text = text
        return

    # Walk through text, dropping plain runs as entry.text / em.tail and
    # creating <em> elements for each wrap span.
    entry.text = text[:spans[0][0]] or None
    last_em = None
    for i, (start, end, phrase) in enumerate(spans):
        em_el = ET.SubElement(entry, "em")
        em_el.text = phrase
        next_start = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        em_el.tail = text[end:next_start] or None
        last_em = em_el
    # If the last em's tail is empty, leave it None — ET handles it.


def _find_descendant_with_tag(elem, tag):
    """First descendant with the given local tag, or None."""
    for descendant in elem.iter():
        if descendant is elem:
            continue
        if _local(descendant.tag) == tag:
            return descendant
    return None


def _dispatch_insert(
    root, anchor_elem, parent, idx, op, article_block,
):
    """Choose the right builder based on article block kind + anchor.

    Returns (PatchResult, applied_bool) or None when the kind is
    unhandled and the caller should route to DETECTED.
    """
    article_kind = (
        getattr(article_block, "kind", None) if article_block else None
    )
    if article_kind not in _HANDLED_INSERT_KINDS:
        return None  # signal "unhandled — caller routes to DETECTED"

    links = (
        getattr(article_block, "links", None) if article_block else None
    )
    emphasis = (
        getattr(article_block, "emphasis", None) if article_block else None
    )
    anchor_tag = _local(anchor_elem.tag)
    text = op.updated_text or ""

    # Definition-list context takes priority over the article-side
    # block kind. The Help Center HTML renders dl-shaped content as
    # bullets, paragraphs, or callouts — none of which map cleanly
    # back to <dlentry>. When the anchor sits inside a <dl> in the
    # source DITA, build a <dlentry> from "term: description" so the
    # output stays structurally valid (IM page 122).
    dl_target = _find_dl_insert_target(root, anchor_elem)
    if dl_target is not None:
        dl_parent, dl_anchor = dl_target
        dl_anchor_idx = list(dl_parent).index(dl_anchor)
        cat, msg = _insert_dlentry_handler(
            dl_anchor, dl_parent, dl_anchor_idx, text, links,
        )
    elif article_kind == "table_row" or anchor_tag == "row":
        # article_kind=="table_row" → build fresh table OR extend a
        # sibling table from a previous batch op.
        # anchor_tag=="row" → add a sibling <row> in an existing table.
        cat, msg = _insert_table_row_handler(
            anchor_elem, parent, idx, article_block,
        )
    elif article_kind == "note":
        cat, msg = _insert_note_handler(
            anchor_elem, parent, idx, text, links, article_block,
        )
    elif article_kind == "step":
        cat, msg = _insert_step_handler(
            root, anchor_elem, parent, idx, text, links, emphasis=emphasis,
        )
    elif article_kind == "list_item":
        cat, msg = _insert_list_item_handler(
            root, anchor_elem, parent, idx, text, links, emphasis=emphasis,
        )
    elif article_kind == "unordered_step":
        cat, msg = _insert_unordered_step_handler(
            root, anchor_elem, parent, idx, text, links, emphasis=emphasis,
        )
    elif article_kind == "heading":
        cat, msg = _insert_heading_handler(
            root, anchor_elem, parent, idx, text, links,
        )
    else:
        # paragraph / None → always emit <p>; if inside a list, escape
        # up to insert the paragraph as a sibling of the list element.
        cat, msg = _insert_paragraph(
            anchor_elem, parent, idx, text, links, root=root,
        )

    if cat == ResultCategory.APPLIED:
        return PatchResult(op=op, category=cat, warning=msg), True
    return PatchResult(op=op, category=cat, reason=msg), False


# --- Note construction (article callouts → DITA <note>) ----------------- #
#
# The Help Center HTML signals callouts via
#   <div class="article-content-callout
#               article-content-callout__background--{kind}"
#        data-test-selector="callout-container">
# where {kind} maps to a known DITA <note> type per IM pages 130–131.
# Unknown kinds fall back to type="important" (the IM default).

_NOTE_KIND_TO_DITA_ATTRS = {
    "warning":    {"type": "important"},
    "permission": {"type": "other", "othertype": "role"},
    "feature":    {"type": "other", "othertype": "feature"},
    "pdf":        {"type": "other", "othertype": "pdf"},
    "tip":        {"type": "tip"},
    "note":       {"type": "note"},
}


def _build_note_element(
    note_kind: Optional[str],
    text: str,
    links: Optional[List[Tuple[str, str]]],
    note_bullets: Optional[List[str]] = None,
) -> Tuple[ET.Element, int]:
    """Build a <note type="..."> body.

    Three shapes, picked by the kind of source content:
    - With `note_bullets`: emit `<note><ul><li/></ul></note>` — used
      when the article had `<p>Note:</p><ul><li/></ul>` shape and the
      parser-merge collapsed it. This preserves the bullet structure
      instead of flattening to one prose paragraph.
    - Without bullets, with `\\n\\n` in the text: one `<p>` per
      paragraph.
    - Otherwise: one `<p>` with the whole text.

    Per IM page 132: notes start with a block element (`<p>` or `<ul>`)
    — never raw text directly inside `<note>`. Inline links attach to
    the LAST paragraph (the conventional "Learn more" position).
    """
    attrs = _NOTE_KIND_TO_DITA_ATTRS.get(
        (note_kind or "").lower(),
        {"type": "important"},  # IM default
    )
    note = ET.Element("note")
    for k, v in attrs.items():
        note.set(k, v)

    if note_bullets:
        ul_el = ET.SubElement(note, "ul")
        total_unplaced = 0
        for bullet_text in note_bullets:
            # Links attach to whichever bullet contains the link text;
            # without context, attach to the last bullet by convention.
            is_last = bullet_text is note_bullets[-1]
            bullet_links = links if is_last else None
            li_el, unplaced = _build_inline_element(
                "li", bullet_text, bullet_links,
            )
            ul_el.append(li_el)
            total_unplaced += unplaced
        return note, total_unplaced

    parts = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
    if not parts:
        parts = [text.strip()] if text.strip() else []

    total_unplaced = 0
    for i, para_text in enumerate(parts):
        is_last = i == len(parts) - 1
        para_links = links if is_last else None
        p_el, unplaced = _build_inline_element("p", para_text, para_links)
        note.append(p_el)
        total_unplaced += unplaced

    return note, total_unplaced


# --- Note "extend" path (additive, never destroys existing structure) - #
#
# When a REPLACE op targets a structural <note> and the article's new
# text starts with the source note's existing text exactly, treat it as
# an extend: leave the existing <p> children alone and append new <p>
# children for the paragraphs the article appended. This is safe because
# we never modify or remove existing content — only add to the tail.

def _can_extend_note(op: DiffOp) -> bool:
    """True when a REPLACE on a <note> looks like the article just
    appended one or more paragraphs after the existing content."""
    if op.kind != OpKind.REPLACE:
        return False
    if op.source_block is None or op.source_block.element_tag != "note":
        return False
    new_text = op.updated_text or ""
    if "\n\n" not in new_text:
        return False
    source_norm = " ".join(op.source_block.text.split())
    parts = re.split(r"\n\n+", new_text)
    if len(parts) < 2:
        return False
    first_norm = " ".join(parts[0].split())
    return first_norm == source_norm and any(p.strip() for p in parts[1:])


def _extend_note(
    element: ET.Element,
    op: DiffOp,
    article_block,
) -> bool:
    """Append new <p> children to a <note> for the trailing paragraphs.

    Existing children are untouched. Returns True on success.
    """
    new_text = op.updated_text or ""
    parts = re.split(r"\n\n+", new_text)
    if len(parts) < 2:
        return False
    new_paras = [p.strip() for p in parts[1:] if p.strip()]
    if not new_paras:
        return False

    links = getattr(article_block, "links", None) if article_block else None

    # Attach the article block's links to the LAST appended paragraph
    # (Help Center callouts typically end with a "Learn more"
    # link; intermediate paragraphs rarely have inline links).
    for i, para_text in enumerate(new_paras):
        is_last = i == len(new_paras) - 1
        para_links = links if is_last else None
        new_p, _unplaced = _build_inline_element("p", para_text, para_links)
        element.append(new_p)
    return True


# Back-compat alias: older callers may import apply_replace_ops.
apply_replace_ops = apply_ops


def _op_sort_key(op: DiffOp, article_kind: Optional[str] = None):
    """Order ops within a topic so positional xpaths stay valid.

    REPLACEs first (no index shift); then DELETEs in reverse doc order
    (so earlier indices aren't affected by later deletes); then INSERTs
    in reverse doc order of their anchor.

    Within the same anchor group, step INSERTs run BEFORE list_item
    INSERTs so that the patch loop can record the newly-created <step>
    and redirect subsequent sub-bullets to anchor inside it (anchor
    propagation). Within each sub-group, reverse article order keeps
    multiple inserts at the same anchor in their original op order.
    """
    if op.kind == OpKind.REPLACE:
        return (0, op.source_block.block_index)
    if op.kind == OpKind.DELETE:
        return (1, -op.source_block.block_index)
    if op.kind == OpKind.INSERT and op.anchor_block is not None:
        is_step_like = 0 if article_kind in ("step", "unordered_step") else 1
        # Anchor: reverse doc order (later anchors first).
        # Then: step-kinds within the group come first (0 < 1).
        # Then: reverse article order so the tail-first insertion trick
        # leaves the items in forward article order at the insertion site.
        return (
            2,
            -op.anchor_block.block_index,
            is_step_like,
            -(op.updated_index or 0),
        )
    return (3, 0)


# --- XML I/O preserving DOCTYPE and XML declaration ----------------------- #

_XML_DECL_RE = re.compile(r"^\s*(<\?xml[^>]*\?>)")
_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>\[]*(?:\[[^\]]*\])?[^>]*>", re.DOTALL)


def _read_xml(path: Path) -> Tuple[ET.ElementTree, Optional[str], Optional[str]]:
    raw = path.read_text(encoding="utf-8")
    decl_match = _XML_DECL_RE.match(raw)
    xml_decl = decl_match.group(1) if decl_match else None
    doctype_match = _DOCTYPE_RE.search(raw)
    doctype = doctype_match.group(0) if doctype_match else None
    tree = ET.parse(path)
    return tree, xml_decl, doctype


def _write_xml(
    tree: ET.ElementTree,
    xml_decl: Optional[str],
    doctype: Optional[str],
    path: Path,
) -> None:
    body = ET.tostring(tree.getroot(), encoding="unicode")
    parts: List[str] = []
    if xml_decl:
        parts.append(xml_decl)
    if doctype:
        parts.append(doctype)
    parts.append(body)
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


# --- Deterministic positional xpath lookup -------------------------------- #

_STEP_RE = re.compile(r"^([^\[]+)(?:\[(\d+)\])?$")


def _find_by_xpath(root: ET.Element, xpath: str) -> Optional[ET.Element]:
    """Walk a positional xpath like '/topic/body[1]/p[2]' from root."""
    parts = xpath.lstrip("/").split("/")
    if not parts:
        return None
    root_tag = _local(root.tag)
    first_match = _STEP_RE.match(parts[0])
    if not first_match or first_match.group(1) != root_tag:
        return None

    current = root
    for step in parts[1:]:
        m = _STEP_RE.match(step)
        if not m:
            return None
        tag = m.group(1)
        idx = int(m.group(2) or 1)
        count = 0
        found: Optional[ET.Element] = None
        for child in current:
            if _local(child.tag) == tag:
                count += 1
                if count == idx:
                    found = child
                    break
        if found is None:
            return None
        current = found
    return current


def _find_parent_of(root: ET.Element, child: ET.Element) -> Optional[ET.Element]:
    """Walk root to find the parent of `child`. ElementTree elements don't
    carry parent references; this is O(n) but only called in INSERT paths
    where n is small (single topic file)."""
    for parent in root.iter():
        for c in parent:
            if c is child:
                return parent
    return None


def _find_ancestor_with_tag(
    root: ET.Element, elem: ET.Element, tag: str
) -> Optional[ET.Element]:
    """Walk upward from `elem` until we find an ancestor with local tag
    equal to `tag` (or None if there's no such ancestor)."""
    current = elem
    while True:
        parent = _find_parent_of(root, current)
        if parent is None:
            return None
        if _local(parent.tag) == tag:
            return parent
        current = parent


def _find_parent_and_child(
    root: ET.Element, xpath: str
) -> Tuple[Optional[ET.Element], Optional[ET.Element]]:
    """Resolve xpath to (parent, child). Used by DELETE since ElementTree
    elements don't carry parent references and removal goes through parent.

    Returns (None, None) if the path doesn't resolve or names the root.
    """
    parts = xpath.lstrip("/").split("/")
    if len(parts) < 2:
        return None, None
    parent_path = "/" + "/".join(parts[:-1])
    parent = _find_by_xpath(root, parent_path)
    if parent is None:
        return None, None

    m = _STEP_RE.match(parts[-1])
    if not m:
        return None, None
    tag = m.group(1)
    idx = int(m.group(2) or 1)
    count = 0
    for child in parent:
        if _local(child.tag) == tag:
            count += 1
            if count == idx:
                return parent, child
    return None, None


def _replace_text(element: ET.Element, new_text: str) -> None:
    """Replace element's content with plain text. Drops inline children."""
    for child in list(element):
        element.remove(child)
    element.text = new_text


# --- Inline <xref> handling --------------------------------------------- #
#
# When an article block carries `links` metadata (a list of (text, href)
# pairs captured from <a href> elements in the HTML), we want the patched
# DITA to mirror that with <xref> children instead of flat text. The two
# helpers below produce the mixed-content XML pattern:
#
#   <p>before text <xref href="...">link text</xref> after text</p>
#
# Substring lookup is first-occurrence; if a link's text doesn't appear in
# the block (whitespace mismatch, normalization difference), we silently
# fall back to plain text for that link rather than risk wrapping the
# wrong substring.

def _normalize_href(href: str) -> str:
    """Make Help Center relative URLs absolute so DITA xrefs are valid.

    Override `HELP_CENTER_HOST` to point at your own Help Center if
    you're deploying this against a different site.
    """
    import os
    host = os.environ.get("HELP_CENTER_HOST", "https://help.example.com")
    if href.startswith("/help/"):
        return host + href
    if href.startswith("//"):
        return "https:" + href
    return href


def _xref_format_for_href(href: str) -> str:
    """Pick @format per the Information Model.

    From IM appendix B (page 234): email links use format="other"; from
    page 253: web URLs use format="html". Telephone and other schemes
    also go to "other" — the IM only enumerates html and other.
    """
    h = href.lower()
    if h.startswith(("mailto:", "tel:")):
        return "other"
    return "html"


def _append_text_to_inline(element: ET.Element, text: str) -> None:
    """Append text to the current 'cursor' of a mixed-content element —
    either the element's .text (no children yet) or the .tail of its
    last child (children exist)."""
    if not text:
        return
    if len(element) == 0:
        element.text = (element.text or "") + text
    else:
        last = element[-1]
        last.tail = (last.tail or "") + text


def _populate_inline(
    element: ET.Element,
    text: str,
    links: Optional[List[Tuple[str, str]]],
) -> int:
    """Fill `element` with text, wrapping link substrings as <xref>.

    Returns the count of links that could not be placed (their text was
    not found in the block body). Callers surface that count as a
    warning so the user knows to add the link manually.
    """
    if not links:
        element.text = text
        return 0

    unplaced = 0
    remaining = text
    for link_text, href in links:
        if not link_text:
            continue
        idx = remaining.find(link_text)
        if idx < 0:
            unplaced += 1
            continue
        before, after = remaining[:idx], remaining[idx + len(link_text):]
        _append_text_to_inline(element, before)
        # IM-confirmed attribute set for external links:
        #   <xref href="..." scope="external" format="html|other">text</xref>
        # outputclass="button" is deliberately NOT set: per IM pages
        # 130-131 it's reserved for note-launcher CTAs (feature/PDF/role),
        # not general inline links.
        normalized_href = _normalize_href(href)
        xref = ET.SubElement(element, "xref")
        xref.text = link_text
        xref.set("href", normalized_href)
        xref.set("scope", "external")
        xref.set("format", _xref_format_for_href(normalized_href))
        remaining = after

    if remaining:
        _append_text_to_inline(element, remaining)
    return unplaced


def _build_inline_element(
    tag: str,
    text: str,
    links: Optional[List[Tuple[str, str]]],
    emphasis: Optional[List[str]] = None,
) -> Tuple[ET.Element, int]:
    """Construct a fresh element with inline mixed content. Returns
    (element, unplaced_link_count).

    If `emphasis` is provided, each phrase in it that appears in the
    text (and isn't already inside an <xref>) gets wrapped in an <em>.
    Article-side <strong>/<b>/<em>/<i> all map to DITA <em> per
    project convention.
    """
    el = ET.Element(tag)
    unplaced = _populate_inline(el, text, links)
    if emphasis:
        _apply_em_wraps(el, emphasis)
    return el, unplaced


def _apply_em_wraps(element: ET.Element, emphasis: List[str]) -> None:
    """Post-pass: wrap each emphasis phrase in `element`'s text/tails
    with an inline <em>, preserving any existing inline children.

    Strategy: walk the element's text and each child's tail. For each
    occurrence of an emphasis phrase, split the surrounding text and
    insert an <em> element at the right position. We only wrap the
    FIRST occurrence per phrase per element so a repeated word doesn't
    explode into multiple wraps.
    """
    if not emphasis:
        return
    remaining_phrases = list(emphasis)

    def _wrap_in_string(s: str) -> Optional[Tuple[str, str, str]]:
        """If any pending phrase is in `s`, return (before, phrase, after)
        and remove the phrase from the pending list. Else None."""
        for phrase in list(remaining_phrases):
            if not phrase:
                remaining_phrases.remove(phrase)
                continue
            idx = s.find(phrase)
            if idx >= 0:
                remaining_phrases.remove(phrase)
                return s[:idx], phrase, s[idx + len(phrase):]
        return None

    # Wrap in element.text first.
    while element.text:
        hit = _wrap_in_string(element.text)
        if hit is None:
            break
        before, phrase, after = hit
        element.text = before
        em = ET.Element("em")
        em.text = phrase
        em.tail = after
        element.insert(0, em)

    # Then walk each child's tail (tail = text after the closing tag
    # but before the next sibling). New <em> wraps get inserted as new
    # siblings positioned right after the child whose tail we split.
    i = 0
    while i < len(element):
        child = element[i]
        while child.tail:
            hit = _wrap_in_string(child.tail)
            if hit is None:
                break
            before, phrase, after = hit
            child.tail = before
            em = ET.Element("em")
            em.text = phrase
            em.tail = after
            element.insert(i + 1, em)
            child = em
            i += 1
        i += 1


def _replace_with_inline(
    element: ET.Element,
    text: str,
    links: Optional[List[Tuple[str, str]]],
) -> int:
    """Clear `element` and refill it with text + inline <xref> children.
    Returns the count of unplaced links."""
    for child in list(element):
        element.remove(child)
    element.text = None
    return _populate_inline(element, text, links)


# --- Output path resolution ---------------------------------------------- #

def _resolve_output_path(op: DiffOp, output_dir: Path) -> Path:
    # REPLACE/DELETE name a source_block; INSERT names an anchor_block.
    block = op.source_block or op.anchor_block
    if block is None:
        raise ValueError("cannot resolve output path: op has no block reference")
    candidate = (output_dir / block.topic_id).resolve()
    root = output_dir.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"refusing to write outside output dir: topic_id={block.topic_id!r}"
        ) from exc
    return candidate


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag
