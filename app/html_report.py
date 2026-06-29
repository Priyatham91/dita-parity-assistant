"""Render a self-contained HTML parity report.

Single .html file, no external assets, no server. Inline CSS for layout,
a few lines of vanilla JS for the category and topic filters. Drop the
file anywhere and open it in a browser.

Design goals (PoC, "show everything"):
  - Summary metrics at the top so the reader sees the scale immediately.
  - Tabs to filter ops by category (APPLIED / SKIPPED / DETECTED).
  - A topic dropdown to focus on one topic at a time.
  - Each op card shows old vs. new text side-by-side plus full provenance
    (topic, xpath, element tag, reason, warning).
  - The publication-order topic outline at the bottom for orientation.
"""

from __future__ import annotations

import datetime
import difflib
import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from app.diff_engine import DiffOp, OpKind
from app.map_parser import TopicRef
from app.patch_engine import PatchReport, PatchResult, ResultCategory
from app.publication_reconstructor import Publication
from app.schematron_validator import ValidationReport


_ICON = {
    ResultCategory.APPLIED: "✓",
    ResultCategory.SKIPPED: "⏸",
    ResultCategory.DETECTED: "⚠",
    ResultCategory.MAP_EDIT: "🗺",
}

_CATEGORY_CLASS = {
    ResultCategory.APPLIED: "applied",
    ResultCategory.SKIPPED: "skipped",
    ResultCategory.DETECTED: "detected",
    ResultCategory.MAP_EDIT: "mapedit",
}

# Writer-facing labels. The keys are technical (APPLIED/SKIPPED/etc.);
# the values appear on tabs, tiles, and op cards.
_LABEL = {
    ResultCategory.APPLIED: "Done",
    ResultCategory.SKIPPED: "Needs review",
    ResultCategory.DETECTED: "Add manually",
    ResultCategory.MAP_EDIT: "Map update",
}

# One-sentence description of each tab, shown under its header.
_LABEL_HINT = {
    ResultCategory.APPLIED: "The tool updated your DITA. Skim the change to confirm.",
    ResultCategory.SKIPPED: "The tool didn't trust this one enough to apply. Review and update by hand if it looks right.",
    ResultCategory.DETECTED: "The tool found new content but couldn't decide where it goes. Place it manually.",
    ResultCategory.MAP_EDIT: "These belong in your .ditamap (reltable), not in the topic files.",
}


# Plain-language rewrite for the technical reason strings produced by
# the patch engine. Each entry: (substring_to_match, (why_text, [steps...]))
# where `why_text` is a short explanation of why the tool didn't auto-
# apply, and `steps` is an ordered list of imperative actions the
# writer should take. Order matters — first match wins.
_REASON_REWRITES = [
    # Simple-action rewrites first — these refuse cleanly with one clear
    # ask and no step-by-step checklist. Order matters: more specific
    # needles must come before the generic structural-element catches.
    (
        "feature note (othertype",
        (
            "Add or update this feature note manually — the tool "
            "couldn't apply it.",
            [],
        ),
    ),
    (
        "complex table",
        (
            "Open this table side-by-side with the live article and "
            "update it by hand. The tool couldn't safely apply these "
            "changes.",
            [],
        ),
    ),
    (
        "cell counts",
        (
            "Add this table change manually — the tool can't safely "
            "update the row.",
            [],
        ),
    ),
    (
        "DELETE refused: source element contains <xref>",
        (
            "The DITA paragraph contains a link or embedded media "
            "(video, image). The article HTML renders media as "
            "iframes/pictures, which the tool can't compare reliably.",
            [
                "Open the live article.",
                "Check whether the media is still there.",
                "If it's gone, delete the paragraph in the topic file by hand.",
                "If it's still there, leave the DITA alone.",
            ],
        ),
    ),
    (
        "DELETE on a topic <title> refused",
        (
            "The tool never auto-deletes a topic title — that's almost "
            "always a diff misalignment, not real intent.",
            [
                "Confirm whether the topic really is gone from the article.",
                "If it's gone: delete the .dita file and remove its "
                "<topicref> from the .ditamap.",
                "If it's still in the article: leave it alone.",
            ],
        ),
    ),
    (
        "mass-deletion guard:",
        (
            "More than half of this topic's content was queued for "
            "deletion. Usually that means the article reorganized the "
            "content rather than removing it.",
            [
                "Open the article and the topic file side by side.",
                "For each item that was queued for deletion, decide: "
                "keep, reword, or remove.",
                "Apply the right edit by hand.",
            ],
        ),
    ),
    (
        "cross-tab routing refused:",
        (
            "The article block lives in one tab (Desktop or Mobile), "
            "but the diff aligned it against a topic for a different tab.",
            [
                "Identify which tab this content belongs to in the "
                "article (Desktop or Mobile).",
                "Open the matching topic file.",
                "Add the change there manually.",
            ],
        ),
    ),
    (
        "source element contains inline markup",
        (
            "The original DITA wraps text in <uicontrol>, <xref>, or "
            "<keyword>. The new article wording is plain text, so the "
            "tool can't safely rewrite without losing the markup.",
            [
                "Open the topic file at the location shown above.",
                "Replace the old wording with the new article wording.",
                "Re-wrap the same phrases in their existing "
                "<uicontrol> / <xref> / <keyword> tags.",
            ],
        ),
    ),
    (
        "structural element <note>",
        (
            "This is a multi-paragraph callout (a <note> with nested "
            "<p>s). The article's version doesn't split the same way, "
            "so the tool can't map the new wording onto the existing "
            "paragraphs.",
            [
                "Open the topic file at the location shown above.",
                "Rewrite the <note>'s body to match the new article wording.",
                "Keep the original @type and @othertype attributes "
                "(important, tip, note, role, feature, pdf).",
            ],
        ),
    ),
    (
        "structural element <dlentry>",
        (
            "This is an existing definition-list entry. The tool "
            "doesn't auto-rewrite <dlentry> elements — the term/"
            "description split is a writer judgment.",
            [
                "Open the topic file at the location shown above.",
                "Update <dt> (the term — short noun phrase, no colon) "
                "and <dd> (the description) by hand.",
            ],
        ),
    ),
    (
        "structural element <row>",
        (
            "This is an existing table row. The tool doesn't auto-"
            "rewrite rows because of cell-by-cell content alignment.",
            [
                "Open the topic file at the location shown above.",
                "Update the row's <entry> cells with the new article wording.",
                "Keep any <uicontrol> wrappers on label cells.",
            ],
        ),
    ),
    (
        "manual-review zone (related tasks",
        (
            "This content is in a Related tasks / Learn more section. "
            "Those entries live in the .ditamap's <reltable>, not in a "
            "topic body.",
            [
                "Open the .ditamap.",
                "Update the <relcell> entries to match the article's "
                "list of related links.",
            ],
        ),
    ),
    (
        "this heading lives in the .ditamap",
        (
            "This text is a tab/section heading defined in the .ditamap "
            "(<topichead> / <topicgroup>), not in a topic body.",
            [
                "Open the .ditamap.",
                "Update the matching <navtitle> directly.",
            ],
        ),
    ),
    (
        "the original REPLACE was refused for safety reasons",
        (
            "A paired 'Needs review' entry holds back the original "
            "rewrite. The new article wording is shown here so it "
            "doesn't get lost.",
            [
                "Find the matching 'Needs your attention' card (same xpath, "
                "different category).",
                "Apply this new wording by hand at that location.",
            ],
        ),
    ),
    (
        "alignment ambiguity:",
        (
            "The diff wasn't sure how this change lines up with the "
            "article — multiple plausible alignments produced different "
            "shapes.",
            [
                "Open the article and the topic file side by side.",
                "Decide the right edit by hand.",
            ],
        ),
    ),
    (
        "INSERT at <publication start>",
        (
            "The article has new content before any matching DITA topic "
            "— the tool has no anchor to place it relative to.",
            [
                "Decide which topic this content belongs in (usually the "
                "main concept topic).",
                "Add it to that topic manually.",
            ],
        ),
    ),
    (
        "INSERT not auto-applied:",
        (
            "Add this new content to the right topic by hand — the "
            "tool can't place this element type yet.",
            [],
        ),
    ),
    (
        "mid-procedure heading",
        (
            "The article has a heading in the middle of a procedure. By "
            "convention, that means a separate procedure, not a divider.",
            [
                "Split the existing <steps> at this point.",
                "Create a new <steps> element with this heading as its "
                "<stepsection> (drop the trailing colon — the stylesheet "
                "adds it).",
                "Move the steps that follow the heading into the new "
                "<steps>.",
            ],
        ),
    ),
    (
        "the anchor lives inside a <dl>",
        (
            "The article's new content sits next to an existing <dl>, "
            "but the wording doesn't split cleanly into a term + "
            "description pair.",
            [
                "Open the topic file at the location shown above.",
                "Add a new <dlentry> by hand with <dt> (the term, no "
                "colon) and <dd> (the description).",
            ],
        ),
    ),
    (
        "Reltable section reviewed",
        (
            "No action needed — the tool found a Related tasks / Learn "
            "more section in the article and it already matches your "
            ".ditamap reltable. Listed so you can confirm the match.",
            [],
        ),
    ),
    (
        "Stale reltable hrefs",
        (
            "Open your .ditamap and verify each affected <topicref href>. "
            "The article's Related tasks / Learn more section uses a "
            "different target URL — usually because the article-ID "
            "format changed (old numeric IDs like 87951 vs. new 'a' + "
            "number like a1342713). The tool never auto-rewrites .ditamap "
            "hrefs.",
            [],
        ),
    ),
    (
        "Review the <reltable> in your .ditamap",
        (
            "The article has a Related tasks / Learn more section. The "
            "list of related links belongs in the .ditamap reltable.",
            [
                "Open the .ditamap.",
                "Update the <relcell> entries to match the article's "
                "list of links.",
            ],
        ),
    ),
    (
        "Verify embedded media",
        (
            "The DITA topics contain images, videos, or <codeblock> "
            "samples. The article HTML renders these as iframes / "
            "pictures / <pre>, which the tool can't reliably compare.",
            [
                "Open the live article.",
                "Compare each image, GIF, video, and code sample "
                "against what the DITA has.",
                "Update by hand if the article changed any of them.",
            ],
        ),
    ),
    (
        "the source <",
        (
            "The wording in this DITA element doesn't appear in the "
            "article anymore. It may have been removed or fully reworded.",
            [
                "Open the live article.",
                "If the content really is gone, delete this element by hand.",
                "If it's been reworded, find the corresponding 'Needs your attention' "
                "entry and apply that new wording here.",
            ],
        ),
    ),
]


def _rewrite_reason(reason: str) -> Optional[Tuple[str, List[str]]]:
    """Map a technical reason string to (why_text, [step1, step2, ...]).

    Returns None if no rewrite matched — caller can fall back to
    showing the original reason.
    """
    if not reason:
        return None
    for needle, plain in _REASON_REWRITES:
        if needle in reason:
            return plain
    return None


# Plain-language rewrite for the warning strings on APPLIED-with-warning
# entries. Same pattern as the reason rewrites.
_WARNING_REWRITES = [
    (
        "added 1 <em> wrap",
        "Wrapped one bold/italic phrase from the article in <em>. "
        "Check whether <uicontrol> (UI element), <wintitle> (window or "
        "page title), or <keyword> (technical term) fits better.",
    ),
    (
        "<em> wrap",  # catches "added N <em> wraps"
        "Wrapped one or more bold/italic phrases from the article in "
        "<em>. Check whether <uicontrol> (UI element), <wintitle> "
        "(window or page title), or <keyword> (technical term) fits "
        "better for each one.",
    ),
    (
        "rewording applied while preserving",
        "Kept the original UI element names, links, and keywords while "
        "rewording the surrounding text. Confirm the formatting still "
        "wraps the right phrase.",
    ),
    (
        "extended <note> with new <p>",
        "Added new paragraphs to an existing callout. Check the "
        "paragraph boundaries and any links inside.",
    ),
    (
        "row INSERT detected but the article parser captured no cell data",
        "Found a new table row but couldn't read its cells. Add the row "
        "by hand.",
    ),
    (
        "new <row> inserted with plain <entry> cells",
        "Added a new table row using plain text cells. If your other "
        "rows wrap text in links or UI elements, add them by hand.",
    ),
]


def _rewrite_warning(warning: str) -> str:
    """Translate one warning string into writer-facing language.

    Warnings can carry multiple parts joined with ' | '. We translate
    each part and join the translations.
    """
    if not warning:
        return ""
    parts = [p.strip() for p in warning.split("|")]
    out: List[str] = []
    for part in parts:
        match = None
        for needle, plain in _WARNING_REWRITES:
            if needle in part:
                match = plain
                break
        out.append(match or part)
    return " ".join(out)


def _render_next_steps_panel(
    *, run_id: Optional[str], dry_run: bool, output_dir: Path,
    report: PatchReport,
    validation: Optional[ValidationReport] = None,
) -> str:
    """Top-of-report banner that tells the writer what to do next AND
    surfaces how much of the work the tool covered automatically.

    Three shapes:
      - dry_run=True + run_id set → yellow banner with the percentage
        the tool *can* apply automatically, plus a "Run migration now"
        button that POSTs to /runs/<run_id>/migrate.
      - dry_run=False → green banner with the percentage the tool DID
        apply, plus the outputs folder path and a "Start a new
        migration" button back to the assistant home.
      - no run_id (offline-only report, exported from somewhere else)
        → omit the panel; the page header alone is enough.

    The percentage is the hero number. It tells the writer in one
    glance how covered they are, and the inverse phrasing
    ("N still need your review") is the nudge to keep reviewing.
    Beta feedback (2026-06-24): writers needed a single, prominent
    "how much was applied" indicator — both as a confidence signal
    AND as a reminder that not-applied items still need attention."""
    if run_id is None:
        return ""
    if dry_run:
        migrate_action = html.escape(f"/runs/{run_id}/migrate")
        return f"""
<section class="next-steps next-steps--dryrun">
  <div class="next-steps-head">
    <span class="next-steps-eyebrow">Dry run</span>
    <h2 class="next-steps-title">Review below, then commit when ready.</h2>
    <p class="next-steps-body">
      No <code>.dita</code> files were written yet. Click
      <b>Run migration now</b> to apply the changes — patched files
      will be written to this run's <code>outputs/</code> folder.
    </p>
  </div>
  <form class="next-steps-actions" action="{migrate_action}"
        method="POST" id="next-steps-migrate-form"
        onsubmit="return window.__ditaParityCheckServer(event);">
    <button type="submit" class="next-steps-btn next-steps-btn--primary">
      Run migration now →
    </button>
  </form>
</section>
"""
    out_dir_display = html.escape(str(output_dir.resolve()))
    open_action = html.escape(f"/runs/{run_id}/open-outputs")
    return f"""
<section class="next-steps next-steps--applied">
  <div class="next-steps-head">
    <span class="next-steps-eyebrow next-steps-eyebrow--applied">Migration applied</span>
    <h2 class="next-steps-title">Your patched files are ready.</h2>
    <p class="next-steps-body">
      Patched <code>.dita</code> files are in this run's
      <code>outputs/</code> folder — copy them into your DITA repo
      to replace the originals.
    </p>
    <form class="next-steps-folder-row" action="{open_action}"
          method="POST" onsubmit="return window.__ditaParityCheckServer(event);">
      <button type="submit" class="next-steps-btn next-steps-btn--folder"
              title="Open this folder in File Explorer">
        📂 Open outputs folder
      </button>
      <code class="next-steps-path-display"
            title="Full path — copy and paste into File Explorer if the button doesn't open the folder">
        {out_dir_display}
      </code>
    </form>
  </div>
  <div class="next-steps-actions">
    <a href="/" class="next-steps-btn next-steps-btn--primary"
       onclick="return window.__ditaParityCheckServerLink(event);">
      Start a new migration →
    </a>
  </div>
</section>
"""


def write_html_report(
    out_path: Path,
    *,
    map_path: Path,
    article_path: Path,
    topic_refs: List[TopicRef],
    publication: Publication,
    updated_block_count: int,
    diff_summary: dict,
    report: PatchReport,
    output_dir: Path,
    article_label: Optional[str] = None,
    article_canonical_url: Optional[str] = None,
    validation: Optional[ValidationReport] = None,
    dry_run: bool = False,
    run_id: Optional[str] = None,
) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    parts: List[str] = []
    parts.append(_HEAD_HTML.format(title=html.escape(map_path.name)))
    parts.append(_render_next_steps_panel(
        run_id=run_id,
        dry_run=dry_run,
        output_dir=out_path.parent,
        report=report,
        validation=validation,
    ))
    parts.append(_render_header(map_path, article_path, article_label, article_canonical_url))
    parts.append(_render_summary(
        topic_refs=topic_refs,
        publication=publication,
        updated_block_count=updated_block_count,
        report=report,
        diff_summary=diff_summary,
        validation=validation,
    ))
    # The "Before you commit" roadmap sits between the summary tiles
    # and the rest of the report — it's the navigation surface that
    # tells the writer what to do, in what order, and where to find
    # each kind of item. Restored 2026-06-25 after a brief experiment
    # with hero-only navigation; the hero ended up overloaded and
    # tab intros only fired on click.
    parts.append(_render_action_items(report, validation))
    parts.append(_render_files_written(report, out_path.parent))
    parts.append(_render_track_changes(report, topic_refs))
    parts.append(_render_topic_filter(topic_refs))
    parts.append(_render_op_tabs(report))
    parts.append(_render_op_list(report.results))
    parts.append(_render_topic_outline(topic_refs, publication))
    # Schematron / style + structure section lives at the END of the
    # report — writers focus on the change cards first, then sweep
    # through any rule violations as a final pass.
    if validation is not None:
        parts.append(_render_validation(validation))
    parts.append(_SCRIPT_HTML)
    parts.append(_FOOT_HTML)

    out_path.write_text("\n".join(parts), encoding="utf-8")


# --- Section renderers --------------------------------------------------- #

def _render_header(
    map_path: Path,
    article_path: Path,
    article_label: Optional[str] = None,
    article_canonical_url: Optional[str] = None,
) -> str:
    """Header block with the live article URL as the most prominent
    element after the report title. Writers always see which article
    this report is for. Falls back to the upload filename when no URL
    is available."""
    ts = datetime.datetime.now().strftime("%b %d, %Y · %I:%M %p")

    # Decide which URL to show. Priority: an explicit canonical URL
    # > a URL that came in as the article_label > none.
    url = article_canonical_url
    if not url and article_label and article_label.startswith(("http://", "https://")):
        url = article_label

    if url:
        article_source_html = f"""
  <div class="article-source">
    <span class="article-source-label">Live article</span>
    <a class="article-source-url" href="{html.escape(url)}" target="_blank" rel="noopener">
      {html.escape(url)} <span class="external-icon" aria-hidden="true">↗</span>
    </a>
  </div>"""
    else:
        article_source_html = f"""
  <div class="article-source article-source--file">
    <span class="article-source-label">Article source</span>
    <span class="article-source-file">{html.escape(article_label or article_path.name)}</span>
    <span class="article-source-note">(uploaded HTML — no canonical URL detected)</span>
  </div>"""

    return f"""
<header class="page-header">
  <h1>DITA Parity Report</h1>{article_source_html}
  <div class="meta-line">
    <span class="meta-item"><b>Map</b> {html.escape(map_path.name)}</span>
    <span class="meta-item meta-item--muted"><b>Generated</b> {html.escape(ts)}</span>
  </div>
</header>
"""


def _render_summary(
    *,
    topic_refs: List[TopicRef],
    publication: Publication,
    updated_block_count: int,
    report: PatchReport,
    diff_summary: dict,
    validation: Optional[ValidationReport] = None,
) -> str:
    validation_count = len(validation.issues) if validation is not None else None
    # Tile bucketing matches the next-steps hero. Beta feedback
    # (2026-06-25): verify-cards belong in Done (they ARE applied;
    # the yellow note is informational), not in "Needs your attention"
    # which inflated the actionable bucket and made the report read
    # like nothing was applied when it actually was.
    attention_count = len(report.skipped) + len(report.detected)
    metrics = [
        ("Topics in map", len(topic_refs), "", ""),
        ("Done", len(report.applied), "applied",
         "Applied automatically. Cards with a yellow note are quick "
         "markup checks — skim to confirm."),
        ("Needs your attention", attention_count, "attention",
         "Cards the tool didn't apply — you'll need to write the "
         "change by hand."),
    ]
    map_edit_count = len(report.map_edits)
    if map_edit_count:
        metrics.append(("Map updates", map_edit_count, "mapedit",
                        "Update needed in your .ditamap (reltable)."))
    if validation_count is not None:
        metrics.append(("Style/structure issues", validation_count, "validation",
                        "Schematron rules flagged these on the patched files."))
    cards = "\n".join(
        f'<div class="metric {cls}" title="{html.escape(hint)}">'
        f'<div class="value">{html.escape(str(value))}</div>'
        f'<div class="label">{html.escape(label)}</div></div>'
        for label, value, cls, hint in metrics
    )
    eq = diff_summary.get("equal", 0)
    rp = diff_summary.get("replace", 0)
    dl = diff_summary.get("delete", 0)
    ins = diff_summary.get("insert", 0)
    sub = (
        f'<div class="diff-line tech-detail">'
        f'<b>Technical:</b> Diff opcodes — '
        f'<b>{eq}</b> equal · <b>{rp}</b> replace · '
        f'<b>{dl}</b> delete · <b>{ins}</b> insert'
        f'</div>'
    )
    toggle = (
        '<div class="tech-toggle">'
        '<label><input type="checkbox" id="tech-toggle"> '
        'Show technical details (xpaths, element tags, op kinds)'
        '</label>'
        '</div>'
    )
    return (
        f'<section class="summary-grid">{cards}</section>'
        f'{sub}{toggle}'
    )


def _render_action_items(
    report: PatchReport,
    validation: Optional[ValidationReport],
) -> str:
    """The "Before you commit" roadmap. Sits below the summary tiles
    and tells the writer what to do, in what order, and where to find
    each kind of item. Restored from the June 23 design after the
    2026-06-25 walk-back: the dense "kitchen sink" hero panel was
    confusing writers, and the per-tab intros only fired on click —
    leaving no top-of-report navigation. This section is the
    navigation, and the hero goes back to a clean status message.

    Bucketing matches the rest of the report:
      - Verify-cards (APPLIED with warning) → Done tab
      - SKIPPED + DETECTED → Needs your attention tab
      - MAP_EDIT → Map update tab
      - Schematron issues → Style and structure check section
    """
    applied_with_warning = [r for r in report.applied if r.warning]
    skipped = report.skipped
    detected = report.detected
    map_edits = report.map_edits
    validation_count = (
        len(validation.issues) if validation is not None else 0
    )

    items: List[Tuple[str, int, str, str]] = []
    if applied_with_warning:
        items.append((
            "Confirm the tool's judgment calls",
            len(applied_with_warning),
            "Done tab — look for yellow notes",
            "The tool made the change but guessed at something (e.g. "
            "whether a phrase should be <em> or <uicontrol>). Each card "
            "has a yellow \"Please verify\" note explaining the call.",
        ))
    if skipped or detected:
        items.append((
            "Apply these changes by hand",
            len(skipped) + len(detected),
            "Needs your attention tab",
            "Items the tool held back — either an existing DITA element "
            "it didn't trust enough to rewrite, or new article content "
            "it couldn't anchor safely. Each card explains what to do.",
        ))
    if map_edits:
        items.append((
            "Look at your .ditamap and media",
            len(map_edits),
            "Map update tab",
            "Reltable changes (Related tasks / Learn more) and embedded "
            "media reviews — both live outside the topic bodies.",
        ))
    if validation_count:
        items.append((
            "Fix style and structure issues",
            validation_count,
            "Style and structure check",
            "Schematron rules (from the IM) flagged these on the patched "
            "files — missing short descriptions, oversized notes, etc.",
        ))

    if not items:
        return (
            '<section class="action-items action-items-clear">'
            '<h2>Nothing to verify — you\'re good</h2>'
            '<p>The tool applied every change cleanly. Skim the '
            '<b>What the tool changed</b> diffs below to confirm, then '
            'commit the patched files.</p>'
            '</section>'
        )

    total = sum(c for _, c, _, _ in items)
    list_items = "".join(
        '<li class="action-item">'
        f'<div class="action-count">{c}</div>'
        '<div class="action-body">'
        f'<div class="action-label">{html.escape(label)}'
        f' <span class="action-where">→ {html.escape(where)}</span></div>'
        f'<div class="action-hint">{html.escape(hint)}</div>'
        '</div>'
        '</li>'
        for label, c, where, hint in items
    )
    return (
        '<section class="action-items">'
        f'<h2>Before you commit — {total} item{"s" if total != 1 else ""} '
        'need your attention</h2>'
        f'<ul>{list_items}</ul>'
        '</section>'
    )


def _render_validation(validation: ValidationReport) -> str:
    """Final-sweep section: Schematron / IM rule violations on the
    patched files. Lives at the END of the report so writers focus on
    the change cards first, then do one last pass on DITA quality."""
    intro = (
        '<p class="section-sub">A final pass against the Information '
        'Model rules. These flag DITA-quality issues like missing short '
        'descriptions or invalid prolog structure. Many are pre-existing '
        'in the converted DITA — worth fixing as you bring the topic to '
        'parity.</p>'
    )
    if validation.skipped_reason:
        return (
            '<section class="validation-section final-section">'
            '<div class="final-marker">Final pass</div>'
            '<h2>Style &amp; structure check</h2>' + intro +
            f'<div class="vempty">Skipped: {html.escape(validation.skipped_reason)}</div>'
            '</section>'
        )
    sch_name = validation.sch_path.name if validation.sch_path else "(none)"
    counts = validation.by_severity
    err = counts.get("error", 0)
    warn = counts.get("warning", 0)
    info = counts.get("info", 0)
    if not validation.issues:
        return (
            '<section class="validation-section final-section">'
            '<div class="final-marker">Final pass</div>'
            '<h2>Style &amp; structure check</h2>' + intro +
            '<div class="vempty good">'
            '<b>✓ No issues</b> — every patched file passes Schematron.'
            f'<span class="muted tech-detail"> Ruleset: <code>{html.escape(sch_name)}</code></span>'
            '</div>'
            '</section>'
        )

    # Group issues by topic_file for easier scanning. Each file gets a
    # collapsible-style block; writers can sweep through topic by topic.
    by_topic: dict[str, list] = {}
    for issue in validation.issues:
        by_topic.setdefault(issue.topic_file, []).append(issue)

    blocks = []
    issue_num = 0
    for topic_file, issues in by_topic.items():
        # Per-topic header with severity tally.
        t_err = sum(1 for i in issues if i.role == "error")
        t_warn = sum(1 for i in issues if i.role == "warning")
        t_info = sum(1 for i in issues if i.role not in ("error", "warning"))
        tally = []
        if t_err:
            tally.append(f'<span class="vtally vtally-error">{t_err} error{"s" if t_err != 1 else ""}</span>')
        if t_warn:
            tally.append(f'<span class="vtally vtally-warning">{t_warn} warning{"s" if t_warn != 1 else ""}</span>')
        if t_info:
            tally.append(f'<span class="vtally">{t_info} info</span>')
        items = []
        for issue in issues:
            issue_num += 1
            role_class = issue.role if issue.role in ("error", "warning") else "info"
            items.append(
                f'<li class="vissue v-{html.escape(role_class)}">'
                f'<div class="vissue-row">'
                f'<span class="vissue-badge v-{html.escape(role_class)}-badge">{html.escape(issue.role.upper() or "INFO")}</span>'
                f'<span class="vissue-rule">{html.escape(issue.rule_id or "(rule)")}</span>'
                f'</div>'
                f'<div class="vissue-msg">{html.escape(issue.message)}</div>'
                f'<div class="vissue-loc tech-detail"><b>Location:</b> <code>{html.escape(issue.location)}</code></div>'
                f'</li>'
            )
        blocks.append(
            f'<div class="vtopic">'
            f'<div class="vtopic-head">'
            f'<code class="vtopic-file">{html.escape(topic_file)}</code>'
            f'<span class="vtally-group">{"".join(tally)}</span>'
            f'</div>'
            f'<ul class="vissue-list">{"".join(items)}</ul>'
            f'</div>'
        )

    total_tally = []
    if err:
        total_tally.append(f'<b>{err}</b> error{"s" if err != 1 else ""}')
    if warn:
        total_tally.append(f'<b>{warn}</b> warning{"s" if warn != 1 else ""}')
    if info:
        total_tally.append(f'<b>{info}</b> info')
    tally_line = " · ".join(total_tally)

    return (
        '<section class="validation-section final-section">'
        '<div class="final-marker">Final pass</div>'
        '<h2>Style &amp; structure check</h2>' + intro +
        f'<div class="vsummary">{tally_line} across {len(by_topic)} file{"s" if len(by_topic) != 1 else ""}.'
        f'<span class="muted tech-detail"> Ruleset: <code>{html.escape(sch_name)}</code></span></div>'
        f'{"".join(blocks)}'
        '</section>'
    )


def _render_files_written(report: PatchReport, base: Path) -> str:
    if not report.files_written:
        return (
            '<section class="files-section empty">'
            '<h2>Patched topic files</h2>'
            '<p class="muted">The tool didn\'t change any topic files this '
            'run. Either your DITA already matches the article, or every '
            'difference needs your manual review (see the cards below).</p>'
            '</section>'
        )
    items = []
    for f in report.files_written:
        try:
            rel = f.resolve().relative_to(base.resolve())
            href = str(rel).replace("\\", "/")
        except ValueError:
            href = f.as_uri()
        items.append(
            f'<li><a href="{html.escape(href)}"><b>{html.escape(_friendly_topic(f.name))}</b></a> '
            f'<code class="tech-detail">{html.escape(f.name)}</code></li>'
        )
    return (
        '<section class="files-section">'
        f'<h2>Patched topic files ({len(report.files_written)})</h2>'
        '<p class="muted">The tool updated these. Open each one and confirm '
        'the changes before committing.</p>'
        f'<ul>{"".join(items)}</ul>'
        '</section>'
    )


# --- Track-changes view (input XML vs patched output XML) --------------- #

_DECL_DOCTYPE_RE = re.compile(
    r"^\s*(<\?xml[^>]*\?>)?\s*(<!DOCTYPE[^>\[]*(?:\[[^\]]*\])?[^>]*>)?\s*",
    re.DOTALL,
)


def _pretty_print_xml(path: Path) -> str:
    """Parse an XML file and return a pretty-printed string for diffing.

    Strips the DOCTYPE (we don't validate against it for display
    purposes) so external DTD references can't make us fail. Uses
    ElementTree.indent for consistent formatting on both input and
    output sides — without that, formatting differences would dominate
    the diff.
    """
    try:
        text = path.read_text(encoding="utf-8")
        # ET parses DOCTYPE-tolerantly but `parse` reads from disk and
        # sometimes hits DTD resolution. Strip prelude and parse the body.
        body = _DECL_DOCTYPE_RE.sub("", text, count=1)
        root = ET.fromstring(body)
        ET.indent(root, space="  ")
        return ET.tostring(root, encoding="unicode")
    except Exception as exc:  # noqa: BLE001
        return f"(could not pretty-print: {exc})"


def _render_file_diff(input_path: Path, output_path: Path) -> str:
    """Render a unified diff with green/red inline highlighting."""
    input_lines = _pretty_print_xml(input_path).splitlines()
    output_lines = _pretty_print_xml(output_path).splitlines()

    rendered: List[str] = []
    for line in difflib.ndiff(input_lines, output_lines):
        if not line:
            continue
        marker, content = line[:2], line[2:]
        escaped = html.escape(content)
        if marker == "+ ":
            rendered.append(f'<span class="d-add">+ {escaped}</span>')
        elif marker == "- ":
            rendered.append(f'<span class="d-del">- {escaped}</span>')
        elif marker == "? ":
            continue  # difflib intraline hint marker
        else:
            rendered.append(f'<span class="d-ctx">  {escaped}</span>')
    return "\n".join(rendered)


def _render_track_changes(
    report: PatchReport, topic_refs: List[TopicRef]
) -> str:
    """Per-file unified diff for each topic the patch engine modified."""
    if not report.files_written:
        return ""

    # Map output basename -> input path (resolved from the topic ref).
    inputs_by_name = {tr.resolved_path.name: tr.resolved_path for tr in topic_refs}

    blocks: List[str] = []
    n_with_diff = 0
    for out_path in report.files_written:
        input_path = inputs_by_name.get(out_path.name)
        if input_path is None or not input_path.exists():
            blocks.append(
                f'<details class="file-changes"><summary>{html.escape(out_path.name)}</summary>'
                '<p class="muted">No matching input file found; cannot show diff.</p>'
                '</details>'
            )
            continue
        diff_html = _render_file_diff(input_path, out_path)
        n_with_diff += 1
        blocks.append(
            '<details class="file-changes" open>'
            f'<summary>{html.escape(out_path.name)}</summary>'
            f'<pre class="diff-pre">{diff_html}</pre>'
            '</details>'
        )

    return (
        '<section class="changes-section">'
        f'<h2>What the tool changed in each topic file ({n_with_diff} '
        f'file{"s" if n_with_diff != 1 else ""})</h2>'
        '<p class="muted">Green = the tool added this. Red, strikethrough = '
        'the tool removed this. Gray = unchanged context for orientation.</p>'
        + "".join(blocks)
        + '</section>'
    )


def _render_topic_filter(topic_refs: List[TopicRef]) -> str:
    """Topic filter dropdown. Sits directly above the tabs so writers
    can narrow the scope, then read just the changes for that topic.
    The tab counts above this filter update live to match the scope —
    so "Needs review (3)" really does mean three items for THIS topic.
    """
    options = ['<option value="">All topics</option>']
    for tr in topic_refs:
        options.append(
            f'<option value="{html.escape(tr.topic_id)}">'
            f'{html.escape(_friendly_topic(tr.topic_id))}'
            f'</option>'
        )
    return (
        '<section class="changes-section">'
        '<div class="section-header">'
        '<h2>All change cards</h2>'
        '<p class="section-sub">Every change the tool detected, grouped by category. '
        'Filter to one topic to focus your review — the counts on each tab update to match.</p>'
        '</div>'
        '<div class="topic-filter">'
        '<label for="topic-select">Focus on one topic</label>'
        '<select id="topic-select">' + "".join(options) + '</select>'
        '</div>'
        '</section>'
    )


def _render_op_tabs(report: PatchReport) -> str:
    """Tab buttons. Each carries a stable `data-base-label` so the JS
    can rewrite its count when the topic filter changes (currently the
    counts are baked at build time and stay frozen on filter — which
    confuses writers about what's actually in scope).

    The "Needs your attention" tab spans both SKIPPED and DETECTED
    categories. They describe the same writer action ("handle this by
    hand") from two directions — a refused REPLACE on an existing DITA
    element vs. a refused INSERT for new article content — and splitting
    them across two tabs inflates perceived workload."""
    map_tab = ""
    if report.map_edits:
        map_label = f'{_ICON[ResultCategory.MAP_EDIT]} {_LABEL[ResultCategory.MAP_EDIT]}'
        map_tab = (
            f'<button data-tab="mapedit" data-base-label="{html.escape(map_label)}">'
            f'{map_label} <span class="tab-count">({len(report.map_edits)})</span>'
            f'</button>'
        )
    applied_label = f'{_ICON[ResultCategory.APPLIED]} {_LABEL[ResultCategory.APPLIED]}'
    attention_label = "⚠ Needs your attention"
    # Tab counts match the tiles and hero. APPLIED (clean OR with
    # verify note) lives under Done. "Needs your attention" is only
    # the genuinely held-back cards (SKIPPED + DETECTED).
    attention_count = len(report.skipped) + len(report.detected)
    return f"""
<nav class="tabs" role="tablist">
  <button data-tab="all" data-base-label="All changes" class="active">All changes <span class="tab-count">({len(report.results)})</span></button>
  <button data-tab="applied" data-base-label="{html.escape(applied_label)}">{applied_label} <span class="tab-count">({len(report.applied)})</span></button>
  <button data-tab="attention" data-base-label="{html.escape(attention_label)}">{attention_label} <span class="tab-count">({attention_count})</span></button>
  {map_tab}
</nav>
"""


def _render_op_list(results: Sequence[PatchResult]) -> str:
    if not results:
        return '<section class="op-list empty"><p class="muted">No ops to show.</p></section>'
    cards = "\n".join(_render_op_card(i, r) for i, r in enumerate(results, 1))
    return f'<section class="op-list">{cards}</section>'


def _action_verb(op: DiffOp, category: ResultCategory) -> str:
    """Writer-friendly summary of what changed, e.g. 'Reworded' or
    'Removed' instead of REPLACE/DELETE/INSERT.
    """
    if category == ResultCategory.MAP_EDIT:
        return "Update your .ditamap"
    if op.kind == OpKind.REPLACE:
        if category == ResultCategory.APPLIED:
            return "Reworded"
        if category == ResultCategory.SKIPPED:
            return "Wording changed (needs your review)"
        return "Wording changed (add manually)"
    if op.kind == OpKind.INSERT:
        if category == ResultCategory.APPLIED:
            return "Added"
        if category == ResultCategory.SKIPPED:
            return "New content (needs your review)"
        return "New content to add"
    if op.kind == OpKind.DELETE:
        if category == ResultCategory.APPLIED:
            return "Removed"
        if category == ResultCategory.SKIPPED:
            return "Looks removed (needs your review)"
        return "Looks removed (verify)"
    return op.kind.value.title()


def _friendly_topic(topic_id: str) -> str:
    """Strip the .dita suffix for display."""
    if not topic_id:
        return ""
    if topic_id == "<ditamap>":
        return "(your DITA map)"
    return topic_id.rsplit(".dita", 1)[0].replace("_", " ")


def _friendly_xpath(xpath: str) -> str:
    """Turn an engineer-y xpath into a navigation breadcrumb a writer
    can use to find the spot in their DITA editor.

    /task/taskbody[1]/steps[1]/step[5]/info[1]/dl[1]/dlentry[2]
        → "step 5 → info → dl → dlentry 2"

    /concept/conbody[1]/note[1]
        → "note"

    Drops the topic root (/task, /concept, /reference) and the always-
    present taskbody/conbody/refbody wrapper, since those are the
    only thing the topic file contains anyway. Drops [1] positional
    indices because they're always implicit; keeps [N] for N>1.
    """
    if not xpath or not xpath.startswith("/"):
        return xpath
    parts = [p for p in xpath.strip("/").split("/") if p]

    def _name(part: str) -> str:
        # Strip the [N] suffix so set lookups succeed.
        return part.split("[", 1)[0]

    # Drop the topic-type root and the body wrapper.
    skip_first = {"task", "concept", "reference", "topic", "glossentry"}
    skip_second = {"taskbody", "conbody", "refbody", "body"}
    if parts and _name(parts[0]) in skip_first:
        parts = parts[1:]
    if parts and _name(parts[0]) in skip_second:
        parts = parts[1:]
    # Beautify each step: drop [1], keep [N] for N>1 as " N".
    pretty: List[str] = []
    for p in parts:
        if "[" in p and p.endswith("]"):
            name, _, rest = p.partition("[")
            num = rest.rstrip("]")
            if num == "1":
                pretty.append(name)
            else:
                pretty.append(f"{name} {num}")
        else:
            pretty.append(p)
    return " → ".join(pretty) if pretty else "(top of topic)"


def _render_advisory_card(i: int, r: PatchResult, cls: str, icon: str) -> str:
    """Card for run-level advisories with no diff op — like the media
    verification reminder and the stale-reltable-href advisory.

    Routes the reason through `_render_op_explanation` so the writer-
    facing rewrites + per-item structured layout (`_extract_affected_
    items` + `.op-affected-list`) fire here too. Beta feedback
    (2026-06-23): the stale-reltable-href advisory was hitting this
    code path and dumping the entire reason inline as a wall of text,
    because we used to call `html.escape(r.reason)` directly here."""
    topic_id = getattr(r, "topic_id", None) or ""
    topic_label = (
        f'<span class="op-topic">in <b>{html.escape(_friendly_topic(topic_id))}</b></span>'
        if topic_id else ""
    )
    explanation_html = _render_op_explanation(r)
    return f"""
<article class="op op-{cls} op-advisory" data-category="{cls}" data-topic="{html.escape(topic_id)}">
  <header class="op-header">
    <span class="op-num">#{i}</span>
    <span class="op-icon">{icon}</span>
    <span class="op-action">Verify manually</span>
    {topic_label}
  </header>
  {explanation_html}
</article>
"""


def _render_op_card(i: int, r: PatchResult) -> str:
    op = r.op
    cls = _CATEGORY_CLASS[r.category]
    icon = _ICON[r.category]

    # Run-level advisories have no diff op — render a simpler card.
    if op is None:
        return _render_advisory_card(i, r, cls, icon)

    kind = op.kind.value.upper()
    action = _action_verb(op, r.category)

    # Topic id is used for the topic-filter; INSERT uses anchor topic.
    friendly_loc = ""  # human-readable breadcrumb under the header
    if op.source_block is not None:
        topic_id = op.source_block.topic_id
        location = f"{op.source_block.topic_id} :: {op.source_block.element_xpath}"
        friendly_topic = _friendly_topic(topic_id)
        friendly_loc = _friendly_xpath(op.source_block.element_xpath)
    elif op.anchor_block is not None:
        topic_id = op.anchor_block.topic_id
        location = f"after {op.anchor_block.topic_id} :: {op.anchor_block.element_xpath}"
        friendly_topic = _friendly_topic(topic_id) + " (insert near here)"
        friendly_loc = "after " + _friendly_xpath(op.anchor_block.element_xpath)
    else:
        topic_id = ""
        location = "<publication start>"
        friendly_topic = "Top of the article"

    diff_html = _render_diff_pair(op)
    explanation_html = _render_op_explanation(r)
    meta_html = _render_op_meta(r)
    snippet_html = ""
    if r.code_snippet:
        snippet_html = (
            '<div class="op-snippet">'
            '<span class="op-snippet-label">Paste into your .ditamap</span>'
            f'{html.escape(r.code_snippet)}'
            '</div>'
        )

    # Two-line header. Line 1 is the headline: icon + bold action verb
    # (the one thing a writer scans for first). Line 2 is context:
    # which topic, where in the topic. Card number recedes to the
    # right edge. Technical xpath stays behind the tech-detail toggle.
    location_pointer = (
        f'<span class="op-loc">{html.escape(friendly_loc)}</span>'
        if friendly_loc else ''
    )
    sep = '<span class="op-sep">·</span>' if location_pointer else ''
    return f"""
<article class="op op-{cls}" data-category="{cls}" data-topic="{html.escape(topic_id)}">
  <header class="op-header">
    <div class="op-headline">
      <span class="op-icon">{icon}</span>
      <span class="op-action">{html.escape(action)}</span>
      <span class="op-num">#{i}</span>
    </div>
    <div class="op-context">
      <span class="op-topic">in <b>{html.escape(friendly_topic)}</b></span>
      {sep}
      {location_pointer}
      <code class="op-location tech-detail">{html.escape(kind)} @ {html.escape(location)}</code>
    </div>
  </header>
  {diff_html}
  {explanation_html}
  {meta_html}
  {snippet_html}
</article>
"""


def _render_affected_item_html(item: str) -> str:
    """Format one affected-list entry. Each entry from the patch engine
    has the shape:

        [Section] "Display text"
            .ditamap: url
            article:  url

    so the first line is the label and any indented continuation lines
    are key/value pairs. Render the label bold, then each follow-up
    line as a muted key + monospace value so the writer can compare
    the two URLs at a glance."""
    lines = [ln for ln in item.splitlines() if ln.strip()]
    if not lines:
        return ""
    label = html.escape(lines[0].strip())
    detail_html_parts: List[str] = []
    for raw in lines[1:]:
        line = raw.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            detail_html_parts.append(
                f'<div class="op-affected-row">'
                f'<span class="op-affected-key">{html.escape(key.strip())}</span>'
                f'<span class="op-affected-value">{html.escape(value.strip())}</span>'
                f'</div>'
            )
        else:
            detail_html_parts.append(
                f'<div class="op-affected-row">'
                f'<span class="op-affected-value">{html.escape(line)}</span>'
                f'</div>'
            )
    details = "".join(detail_html_parts)
    return (
        f'<div class="op-affected-label">{label}</div>'
        f'{details}'
    )


def _extract_affected_items(reason: str) -> List[str]:
    """If the reason text carries a structured per-item list (intro
    paragraph + a blank line + "Affected ...:" header + bullet lines
    starting with "• "), pull the bullet lines out so the renderer
    can lay them out as a real <ul><li> block instead of running them
    inline. Returns [] when the reason has no such list. Beta feedback
    (2026-06-23): the stale-reltable-href advisory was rendering as a
    wall of text because the bullet markers + line breaks collapsed
    inside the inline action sentence."""
    if not reason or "• " not in reason:
        return []
    # Anything after the LAST blank-line break is treated as the list
    # — works for both "Affected links:\n• …" and any future advisory
    # that follows the same intro-then-bullets pattern.
    chunks = reason.split("\n\n")
    if len(chunks) < 2:
        return []
    tail = chunks[-1]
    items: List[str] = []
    current: List[str] = []
    for line in tail.splitlines():
        if line.lstrip().startswith("•"):
            if current:
                items.append("\n".join(current).rstrip())
            current = [line.lstrip().lstrip("•").lstrip()]
        elif current:
            current.append(line)
    if current:
        items.append("\n".join(current).rstrip())
    return [it for it in items if it]


def _render_op_explanation(r: PatchResult) -> str:
    """Two-section explanation for each card:

        What to do  → numbered, imperative steps (lead with action)
        Why         → one short explanation in muted color

    Always visible (not behind the tech toggle) — this is what the
    writer reads first.
    """
    parts: List[str] = []
    if r.reason:
        rewrite = _rewrite_reason(r.reason)
        affected_items = _extract_affected_items(r.reason)
        if rewrite is not None:
            why_text, steps = rewrite
            if len(steps) <= 1:
                # Single-action card: no numbered checklist, no muted
                # "why" line. The action sentence carries everything
                # the writer needs to act. When the original reason
                # carried a structured per-item list (e.g. the stale-
                # reltable-href advisory's "Affected links:" block),
                # surface it as a real <ul><li> right under the action
                # line so the writer doesn't have to expand the tech-
                # detail toggle to see what's affected.
                action_text = steps[0] if steps else why_text
                items_html = ""
                if affected_items:
                    li_html = "".join(
                        f'<li>{_render_affected_item_html(item)}</li>'
                        for item in affected_items
                    )
                    items_html = (
                        f'<ul class="op-affected-list">{li_html}</ul>'
                    )
                parts.append(
                    f'<div class="op-explanation">'
                    f'<div class="op-action-line">{html.escape(action_text)}</div>'
                    f'{items_html}'
                    f'</div>'
                )
            else:
                steps_html = "".join(
                    f'<li>{html.escape(s)}</li>' for s in steps
                )
                parts.append(
                    f'<div class="op-explanation">'
                    f'<div class="op-action-label">What to do</div>'
                    f'<ol class="op-steps">{steps_html}</ol>'
                    f'<div class="op-why">'
                    f'<span class="op-why-label">Why:</span> '
                    f'{html.escape(why_text)}'
                    f'</div>'
                    f'</div>'
                )
        else:
            # No rewrite matched; show the raw reason but flagged as such.
            parts.append(
                f'<div class="op-explanation">'
                f'<div class="op-action-label">What to do</div>'
                f'<div class="op-steps-fallback">'
                f'Review this change manually. The tool wasn\'t sure '
                f'how to handle it.</div>'
                f'<div class="op-why">'
                f'<span class="op-why-label">Technical reason:</span> '
                f'{html.escape(r.reason)}'
                f'</div>'
                f'</div>'
            )
    if r.warning:
        plain = _rewrite_warning(r.warning)
        parts.append(
            f'<div class="op-warning">'
            f'<b>Please verify:</b> {html.escape(plain)}'
            f'</div>'
        )
    return "".join(parts)


def _render_diff_pair(op: DiffOp) -> str:
    old_text = op.source_block.text if op.source_block is not None else None
    new_text = op.updated_text

    left = (
        f'<div class="diff-side diff-old"><label>Currently in your DITA</label>'
        f'<pre>{html.escape(old_text)}</pre></div>'
        if old_text is not None
        else '<div class="diff-side diff-old muted"><label>—</label>'
             '<pre>(nothing here yet — this is new content)</pre></div>'
    )
    right = (
        f'<div class="diff-side diff-new"><label>In the updated article</label>'
        f'<pre>{html.escape(new_text)}</pre></div>'
        if new_text is not None
        else '<div class="diff-side diff-new muted"><label>—</label>'
             '<pre>(no longer in the article)</pre></div>'
    )
    return f'<div class="op-diff">{left}{right}</div>'


def _render_op_meta(r: PatchResult) -> str:
    """Renders the per-op metadata block. Everything here is technical
    detail — wrapped in `.tech-detail` so it's hidden by default and
    only appears when the writer flips the toggle."""
    op = r.op
    if op is None:
        return ""
    rows = []
    if op.source_block is not None:
        rows.append(("Topic file", op.source_block.topic_id))
        rows.append(("Element", f"<{op.source_block.element_tag}>"))
        rows.append(("Xpath", op.source_block.element_xpath))
        if op.source_block.note_type:
            rows.append(("Note type", op.source_block.note_type))
    if op.kind == OpKind.INSERT and op.anchor_block is not None:
        rows.append(("Anchor topic file", op.anchor_block.topic_id))
        rows.append(("Anchor xpath", op.anchor_block.element_xpath))
    if r.reason:
        rows.append(("Original reason (technical)", r.reason))
    if r.warning:
        rows.append(("Original warning (technical)", r.warning))

    if not rows:
        return ""

    dl_items = "".join(
        f"<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>"
        for k, v in rows
    )
    return f'<dl class="op-meta tech-detail">{dl_items}</dl>'


def _render_topic_outline(topic_refs: List[TopicRef], publication: Publication) -> str:
    if not topic_refs:
        return ""
    items = []
    for tr in topic_refs:
        count = sum(1 for b in publication.blocks if b.topic_id == tr.topic_id)
        items.append(
            f'<li><b>{html.escape(_friendly_topic(tr.topic_id))}</b> '
            f'<span class="muted">— {count} item(s)</span>'
            f'<code class="tech-detail"> ({html.escape(tr.topic_id)})</code></li>'
        )
    return (
        '<section class="topic-outline">'
        '<h2>Topics in your map (in publication order)</h2>'
        f'<ol>{"".join(items)}</ol>'
        '</section>'
    )


# --- Boilerplate (head, script, foot) ------------------------------------ #

_HEAD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DITA Parity Report — {title}</title>
<style>
  :root {{
    --bg: #f7f7f8;
    --surface: #ffffff;
    --text: #1f2328;
    --muted: #6e7681;
    --border: #d0d7de;
    --accent: #0969da;
    --applied: #1a7f37;
    --applied-bg: #dafbe1;
    --skipped: #9a6700;
    --skipped-bg: #fff8c5;
    --detected: #cf222e;
    --detected-bg: #ffebe9;
    --diff-old-bg: #fff8f8;
    --diff-new-bg: #f3fbf3;
    --validation: #8250df;
    --validation-bg: #f3eafd;
    --mapedit: #116329;
    --mapedit-bg: #dafbe1;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    font-size: 14px; line-height: 1.5; }}
  body {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
  code, pre {{ font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-size: 13px; }}
  h1 {{ margin: 0 0 4px; font-size: 24px; }}
  h2 {{ margin: 24px 0 12px; font-size: 16px; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.04em; font-weight: 600; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .muted {{ color: var(--muted); }}

  /* Top-of-report "what to do next" panel. Two flavours: dry-run
     (yellow, prompts to commit) and applied (green, points at the
     patched files). Replaced the old yellow "DRY RUN — re-submit
     without the checkbox" strip after Beta feedback that writers
     didn't know what to do after a run finished. */
  .next-steps {{ display: flex; flex-wrap: wrap; gap: 18px;
    align-items: center; justify-content: space-between;
    border-radius: 10px; padding: 16px 20px; margin-bottom: 18px; }}
  .next-steps--dryrun {{ background: #fff8c5; border: 2px solid #d4a72c; }}
  .next-steps--applied {{ background: #dafbe1; border: 2px solid #1a7f37; }}
  .next-steps-head {{ flex: 1 1 60%; min-width: 280px; }}
  .next-steps-eyebrow {{ display: inline-block; font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.08em; color: #7d5300;
    background: #ffeaa7; padding: 2px 10px; border-radius: 999px;
    margin-bottom: 6px; }}
  .next-steps-eyebrow--applied {{ color: #1a7f37; background: #b1f1c1; }}
  .next-steps-title {{ margin: 4px 0 6px; font-size: 17px; font-weight: 700;
    color: var(--text); text-transform: none; letter-spacing: 0;
    padding: 0; border: none; }}
  .next-steps-body {{ margin: 0; color: var(--text); font-size: 13px;
    line-height: 1.55; }}
  .next-steps-body code {{ background: rgba(255, 255, 255, 0.55);
    padding: 1px 5px; border-radius: 3px; }}
  /* "Open outputs folder" button + path display row on the applied
     panel. Browsers block plain file:// links from http://localhost,
     so a button POSTing to /runs/<id>/open-outputs is the reliable
     way to pop File Explorer. Path stays visible next to the button
     so writers can copy-paste if needed. */
  .next-steps-folder-row {{ display: flex; align-items: center; gap: 10px;
    flex-wrap: wrap; margin-top: 10px; }}
  .next-steps-btn--folder {{ background: var(--applied); color: #fff;
    padding: 8px 14px; border-radius: 6px; border: none; cursor: pointer;
    font-family: inherit; font-size: 13px; font-weight: 600;
    line-height: 1.2; flex: 0 0 auto; }}
  .next-steps-btn--folder:hover {{ background: #15662c; }}
  .next-steps-path-display {{ flex: 1 1 280px; padding: 6px 10px;
    background: rgba(255, 255, 255, 0.65); border: 1px solid rgba(0, 0, 0, 0.1);
    border-radius: 6px; font-family: ui-monospace, SFMono-Regular, Menlo,
    Consolas, monospace; font-size: 12px; color: var(--text);
    word-break: break-all; user-select: all; }}
  .next-steps-actions {{ flex: 0 0 auto; }}
  .next-steps-btn {{ display: inline-block; padding: 10px 18px;
    border-radius: 8px; border: none; cursor: pointer; font-size: 14px;
    font-weight: 600; text-decoration: none; line-height: 1.2;
    font-family: inherit; }}
  .next-steps-btn--primary {{ background: #0969da; color: #fff; }}
  .next-steps-btn--primary:hover {{ background: #0858c2; text-decoration: none; }}

  .page-header {{ border-bottom: 1px solid var(--border); padding-bottom: 20px; margin-bottom: 20px; }}
  .page-header h1 {{ margin: 0 0 14px 0; }}
  .article-source {{ background: #f6f8fa; border: 1px solid var(--border); border-left: 4px solid var(--accent);
    border-radius: 6px; padding: 10px 14px; margin-bottom: 12px; display: flex; flex-direction: column;
    gap: 4px; }}
  .article-source--file {{ border-left-color: var(--muted); }}
  .article-source-label {{ font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--muted); }}
  .article-source-url {{ font-size: 15px; font-weight: 500; color: var(--accent); word-break: break-all;
    line-height: 1.4; }}
  .article-source-url:hover {{ text-decoration: underline; }}
  .external-icon {{ display: inline-block; margin-left: 4px; font-size: 0.85em; color: var(--muted); }}
  .article-source-file {{ font-size: 14px; color: var(--text); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  .article-source-note {{ font-size: 12px; color: var(--muted); font-style: italic; }}
  .meta-line {{ display: flex; gap: 24px; flex-wrap: wrap; color: var(--muted); font-size: 13px; }}
  .meta-item b {{ color: var(--text); font-weight: 600; margin-right: 6px; }}
  .meta-item--muted {{ color: var(--muted); }}
  .meta-line code {{ color: var(--text); background: var(--surface); border: 1px solid var(--border);
    padding: 1px 6px; border-radius: 4px; }}

  .summary-grid {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin: 16px 0; }}
  .metric {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    padding: 12px; text-align: center; }}
  .metric .value {{ font-size: 28px; font-weight: 700; line-height: 1; }}
  .metric .label {{ font-size: 12px; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.04em; margin-top: 6px; }}
  .metric.applied    .value {{ color: var(--applied);    }}
  .metric.skipped    .value {{ color: var(--skipped);    }}
  .metric.detected   .value {{ color: var(--detected);   }}
  .metric.attention  .value {{ color: var(--skipped);    }}
  .metric.validation .value {{ color: var(--validation); }}
  .metric.mapedit    .value {{ color: var(--mapedit);    }}
  .diff-line {{ color: var(--muted); font-size: 13px; margin-bottom: 16px; }}

  .files-section ul {{ margin: 0; padding-left: 18px; }}
  .files-section li {{ margin: 4px 0; }}
  .files-section.empty p {{ font-style: italic; }}

  .changes-section {{ margin: 28px 0 12px; }}
  .section-header h2 {{ margin: 0 0 4px; font-size: 22px; }}
  .section-sub {{ margin: 0 0 14px; color: var(--muted); font-size: 14px; line-height: 1.5; }}

  .topic-filter {{ background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 14px; display: flex; flex-direction: column;
    gap: 6px; max-width: 520px; margin-bottom: 8px; }}
  .topic-filter label {{ font-size: 11px; font-weight: 700; letter-spacing: 0.08em;
    text-transform: uppercase; color: var(--muted); }}
  .topic-filter select {{ font: inherit; font-size: 14px; padding: 8px 10px;
    border: 1px solid var(--border); border-radius: 6px; background: white;
    cursor: pointer; }}
  .topic-filter select:focus {{ outline: 2px solid var(--accent); outline-offset: 1px; }}

  .tabs {{ display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin: 12px 0 16px;
    flex-wrap: wrap; }}
  .tabs button {{ font: inherit; cursor: pointer; padding: 10px 16px; border: none; background: transparent;
    color: var(--muted); border-bottom: 2px solid transparent; margin-bottom: -1px; font-size: 14px; }}
  .tabs button:hover {{ color: var(--text); }}
  .tabs button.active {{ color: var(--text); border-bottom-color: var(--accent); font-weight: 600; }}
  .tab-count {{ font-variant-numeric: tabular-nums; opacity: 0.85; margin-left: 2px; }}

  .op-list .op {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    margin-bottom: 12px; overflow: hidden; }}
  .op.op-applied  {{ border-left: 4px solid var(--applied);  }}
  .op.op-skipped  {{ border-left: 4px solid var(--skipped);  }}
  .op.op-detected {{ border-left: 4px solid var(--detected); }}
  .op.op-mapedit  {{ border-left: 4px solid var(--mapedit);  }}
  .op-list.empty p {{ font-style: italic; padding: 12px; }}

  .op-header {{ padding: 12px 16px 10px; background: #fafbfc;
    border-bottom: 1px solid var(--border); }}
  .op-headline {{ display: flex; align-items: center; gap: 10px; }}
  .op-headline .op-action {{ flex: 1; }}
  .op-context {{ display: flex; align-items: center; gap: 8px;
    flex-wrap: wrap; margin-top: 4px; padding-left: 26px; }}
  .op-num {{ color: var(--muted); font-size: 12px; font-weight: 500;
    font-variant-numeric: tabular-nums; }}
  .op-icon {{ font-size: 18px; line-height: 1; }}
  .op-sep {{ color: var(--border); }}
  .op-loc {{ color: var(--muted); font-size: 13px; }}
  .op-kind {{ font-weight: 600; font-size: 12px; padding: 2px 8px; border-radius: 999px;
    background: var(--bg); color: var(--text); }}
  .op-applied  .op-kind {{ background: var(--applied-bg);  color: var(--applied);  }}
  .op-skipped  .op-kind {{ background: var(--skipped-bg);  color: var(--skipped);  }}
  .op-detected .op-kind {{ background: var(--detected-bg); color: var(--detected); }}
  .op-mapedit  .op-kind {{ background: var(--mapedit-bg);  color: var(--mapedit);  }}

  .op-snippet {{ background: #f6f8fa; border-top: 1px solid var(--border);
    padding: 10px 14px; margin: 0; font-family: ui-monospace, monospace;
    font-size: 12px; white-space: pre-wrap; overflow-x: auto; }}
  .op-snippet-label {{ display: block; font-size: 11px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 4px;
    font-weight: 600; font-family: -apple-system, sans-serif; }}
  .op-location {{ color: var(--muted); font-size: 12px; flex: 1; overflow-wrap: anywhere; }}
  .op-action {{ font-weight: 700; font-size: 16px; color: var(--text); line-height: 1.3; }}
  .op-topic {{ color: var(--muted); font-size: 13px; }}
  .op-topic b {{ color: var(--text); font-weight: 600; }}

  /* Plain-language explanation under the diff */
  .op-explanation {{ background: #f0f6fc; border-top: 1px solid var(--border);
    padding: 14px 16px; }}
  /* Legacy classes (kept for any single-paragraph rewrites) */
  .op-explanation-headline {{ font-weight: 600; margin-bottom: 4px; }}
  .op-explanation-action {{ font-size: 13px; color: var(--text); }}

  /* New two-section card layout: "What to do" steps + muted "Why" */
  .op-action-label {{ font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.06em; font-weight: 700; color: var(--accent);
    margin: 0 0 6px; }}
  /* Single-action card: one clear ask, no checklist. Used when a
     refusal boils down to "please do this manually". */
  .op-action-line {{ font-size: 14px; font-weight: 500; color: var(--text);
    line-height: 1.5; }}

  /* Structured per-item list under the action sentence (e.g. the
     stale-reltable-href advisory's "Affected links" block). Each
     <li> shows a bold label + key/value rows comparing the two
     URLs side-by-side. Beta feedback (2026-06-23): the previous
     wall-of-text rendering hid the affected items inside the inline
     reason string and writers had to expand the tech-detail toggle
     to see what was actually broken. */
  .op-affected-list {{ list-style: none; margin: 12px 0 0; padding: 0; }}
  .op-affected-list li {{ background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; padding: 10px 12px; margin: 6px 0; }}
  .op-affected-label {{ font-weight: 600; font-size: 13px; color: var(--text);
    margin-bottom: 6px; word-break: break-word; }}
  .op-affected-row {{ display: flex; gap: 8px; font-size: 12px;
    line-height: 1.5; margin-top: 2px; }}
  .op-affected-key {{ flex: 0 0 70px; color: var(--muted); font-weight: 600;
    text-transform: uppercase; font-size: 11px; letter-spacing: 0.04em;
    padding-top: 1px; }}
  .op-affected-value {{ flex: 1 1 auto; color: var(--text);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px; word-break: break-all; }}
  .op-steps {{ margin: 0; padding: 0 0 0 22px; font-size: 14px;
    color: var(--text); }}
  .op-steps li {{ margin: 4px 0; line-height: 1.45; }}
  .op-steps-fallback {{ font-size: 14px; color: var(--text); padding: 0; }}
  /* "Why:" line under the steps. Beta feedback (2026-06-23): the
     previous treatment used `var(--muted)` for both label AND body,
     making the explanation read like greyed-out fine print right
     after the (full-strength) numbered steps. Match the "What to do"
     pattern instead — accent-coloured label + full-strength body —
     so writers can actually skim the reason without straining. */
  .op-why {{ margin-top: 10px; padding-top: 10px;
    border-top: 1px dashed var(--border); font-size: 13px;
    color: var(--text); line-height: 1.5; }}
  .op-why-label {{ font-weight: 700; color: var(--accent);
    text-transform: uppercase; font-size: 11px;
    letter-spacing: 0.06em; margin-right: 6px; }}

  /* Location pointer in card header */
  .op-location-friendly {{ display: inline-flex; align-items: center;
    gap: 4px; color: var(--muted); font-size: 12px; font-family: ui-monospace,
    SFMono-Regular, Menlo, Consolas, monospace; padding: 1px 6px;
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 4px; }}
  .op-warning {{ background: #fff8c5; border-top: 1px solid var(--border);
    padding: 12px 14px; font-size: 13px; color: #7d5300; }}
  .op-warning b {{ color: #7d5300; }}

  /* Technical details: hidden by default, shown when #tech-toggle is
     checked. Marked !important because some component classes (e.g.
     .op-meta with display:grid) come after .tech-detail in the cascade
     and would otherwise override the hide. The toggle is the single
     entry point for revealing engineering data. */
  .tech-detail {{ display: none !important; }}
  body.show-tech .tech-detail {{ display: revert !important; }}
  body.show-tech .op-header .tech-detail {{ display: inline !important; }}
  body.show-tech dl.tech-detail {{ display: grid !important; }}

  .tech-toggle {{ margin: 8px 0 16px; font-size: 13px; color: var(--muted); }}
  .tech-toggle label {{ cursor: pointer; user-select: none; }}
  .tech-toggle input {{ margin-right: 6px; vertical-align: middle; }}

  /* "Before you commit" roadmap — June 23 design, restored 2026-06-25.
     One row per non-empty category with count + label + where + brief
     explanation. */
  .action-items {{ background: var(--surface); border: 1px solid var(--border);
    border-left: 4px solid var(--skipped); border-radius: 8px;
    padding: 14px 18px; margin: 16px 0 24px; }}
  .action-items h2 {{ margin: 0 0 12px; font-size: 16px; color: var(--text);
    text-transform: none; letter-spacing: 0; padding: 0; border: none; }}
  .action-items ul {{ list-style: none; padding: 0; margin: 0; }}
  .action-items.action-items-clear {{ border-left-color: var(--applied); }}
  .action-items.action-items-clear h2 {{ color: var(--applied); }}
  .action-item {{ display: flex; align-items: flex-start; gap: 14px;
    padding: 10px 0; border-top: 1px solid var(--border); }}
  .action-item:first-child {{ border-top: none; padding-top: 0; }}
  .action-count {{ flex-shrink: 0; min-width: 38px; height: 38px;
    border-radius: 8px; background: var(--skipped-bg); color: var(--skipped);
    font-size: 18px; font-weight: 700; display: flex;
    align-items: center; justify-content: center; padding: 0 8px; }}
  .action-body {{ flex: 1; }}
  .action-label {{ font-weight: 600; font-size: 15px; }}
  .action-where {{ color: var(--accent); font-weight: 600; font-size: 13px; }}
  .action-hint {{ color: var(--muted); font-size: 13px; margin-top: 2px; }}

  .op-diff {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0; }}
  .diff-side {{ padding: 10px 14px; }}
  .diff-side label {{ display: block; font-size: 11px; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.04em; margin-bottom: 6px; font-weight: 600; }}
  .diff-side pre {{ margin: 0; white-space: pre-wrap; word-break: break-word; }}
  .diff-old {{ background: var(--diff-old-bg); border-right: 1px solid var(--border); }}
  .diff-new {{ background: var(--diff-new-bg); }}

  .op-meta {{ margin: 0; padding: 10px 14px; border-top: 1px solid var(--border);
    display: grid; grid-template-columns: 140px 1fr; gap: 4px 12px; }}
  .op-meta dt {{ color: var(--muted); font-size: 12px; font-weight: 600; }}
  .op-meta dd {{ margin: 0; font-size: 13px; overflow-wrap: anywhere; }}

  .topic-outline ol {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    padding: 12px 12px 12px 32px; }}
  .topic-outline li {{ margin: 4px 0; }}

  /* Track-changes view */
  .changes-section .file-changes {{ background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; margin-bottom: 10px; }}
  .changes-section .file-changes > summary {{ padding: 10px 14px; cursor: pointer; font-weight: 600;
    font-family: ui-monospace, monospace; font-size: 13px; }}
  .changes-section .file-changes[open] > summary {{ border-bottom: 1px solid var(--border); }}
  .diff-pre {{ margin: 0; padding: 12px 14px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px; line-height: 1.5; white-space: pre-wrap; overflow-x: auto; }}
  .diff-pre .d-add {{ background: #dafbe1; color: #1a7f37; display: block; padding: 0 6px; border-left: 3px solid var(--applied); }}
  .diff-pre .d-del {{ background: #ffebe9; color: #cf222e; display: block; padding: 0 6px; border-left: 3px solid var(--detected); text-decoration: line-through; }}
  .diff-pre .d-ctx {{ display: block; padding: 0 6px; color: var(--muted); }}

  .final-section {{ margin-top: 40px; padding-top: 28px; border-top: 2px solid var(--border); }}
  .final-marker {{ display: inline-block; font-size: 11px; font-weight: 700;
    letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted);
    background: var(--bg); padding: 4px 10px; border-radius: 999px;
    border: 1px solid var(--border); margin-bottom: 12px; }}
  .validation-section h2 {{ margin: 0 0 6px; }}
  .vsummary {{ background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 16px; margin: 12px 0 20px; font-size: 15px; }}
  .vempty {{ background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 16px; }}
  .vempty.good {{ background: var(--applied-bg); border-color: var(--applied);
    color: var(--applied); }}
  .vempty.good b {{ color: var(--applied); }}

  .vtopic {{ background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; margin-bottom: 16px; overflow: hidden; }}
  .vtopic-head {{ display: flex; justify-content: space-between; align-items: center;
    padding: 10px 16px; background: #fafbfc; border-bottom: 1px solid var(--border);
    flex-wrap: wrap; gap: 8px; }}
  .vtopic-file {{ font-size: 14px; color: var(--text); font-weight: 600;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  .vtally-group {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .vtally {{ font-size: 12px; font-weight: 600; padding: 3px 9px; border-radius: 999px;
    background: var(--bg); color: var(--muted); }}
  .vtally-error {{ background: var(--detected-bg); color: var(--detected); }}
  .vtally-warning {{ background: var(--skipped-bg); color: var(--skipped); }}

  .validation-section ul.vissue-list {{ list-style: none; padding: 12px 16px; margin: 0; }}
  .vissue {{ padding: 10px 0; border-bottom: 1px solid var(--border); }}
  .vissue:last-child {{ border-bottom: none; padding-bottom: 0; }}
  .vissue:first-child {{ padding-top: 0; }}
  .vissue-row {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }}
  .vissue-badge {{ font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 999px;
    letter-spacing: 0.06em; background: var(--bg); color: var(--muted); }}
  .v-error-badge {{ background: var(--detected-bg); color: var(--detected); }}
  .v-warning-badge {{ background: var(--skipped-bg); color: var(--skipped); }}
  .vissue-rule {{ font-weight: 600; font-family: ui-monospace, monospace; font-size: 13px;
    color: var(--text); }}
  .vissue-msg {{ font-size: 14px; line-height: 1.5; color: var(--text); }}
  .vissue-loc {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}

  @media (max-width: 800px) {{
    .summary-grid {{ grid-template-columns: repeat(3, 1fr); }}
    .op-diff {{ grid-template-columns: 1fr; }}
    .diff-old {{ border-right: none; border-bottom: 1px solid var(--border); }}
    .op-meta {{ grid-template-columns: 1fr; }}
    .op-meta dt {{ margin-top: 6px; }}
  }}
</style>
</head>
<body>
"""


_SCRIPT_HTML = """
<script>
(function () {
  const tabs = document.querySelectorAll('.tabs button');
  const ops = document.querySelectorAll('.op');
  const topicSelect = document.getElementById('topic-select');
  let activeTab = 'all';
  let activeTopic = '';

  // Recompute per-category counts from the ops currently in scope
  // (after the topic filter) and rewrite each tab button's count.
  // Without this the counts stay frozen at "all topics" totals and
  // a writer who filters to one topic still sees the global numbers
  // — a real usability bug surfaced in beta feedback.
  function recountTabs() {
    const counts = {all: 0, applied: 0, attention: 0, mapedit: 0};
    ops.forEach(op => {
      if (activeTopic && op.dataset.topic !== activeTopic) return;
      counts.all++;
      const cat = op.dataset.category;
      // All APPLIED (with or without verify note) live under Done.
      // "Needs your attention" is the truly held-back queue.
      if (cat === 'applied') counts.applied++;
      else if (cat === 'skipped' || cat === 'detected') counts.attention++;
      else if (cat === 'mapedit') counts.mapedit++;
    });
    tabs.forEach(btn => {
      const t = btn.dataset.tab;
      const base = btn.dataset.baseLabel || btn.textContent.replace(/\\s*\\(\\d+\\)\\s*$/, '');
      const c = counts[t] !== undefined ? counts[t] : 0;
      const span = btn.querySelector('.tab-count');
      if (span) {
        span.textContent = '(' + c + ')';
      } else {
        btn.innerHTML = base + ' <span class="tab-count">(' + c + ')</span>';
      }
    });
  }

  function applyFilters() {
    ops.forEach(op => {
      const cat = op.dataset.category;
      let matchTab;
      if (activeTab === 'all') {
        matchTab = true;
      } else if (activeTab === 'attention') {
        // Held-back only; verify-cards live under Done.
        matchTab = cat === 'skipped' || cat === 'detected';
      } else {
        matchTab = cat === activeTab;
      }
      const matchTopic = !activeTopic || op.dataset.topic === activeTopic;
      op.style.display = (matchTab && matchTopic) ? '' : 'none';
    });
  }

  tabs.forEach(btn => {
    btn.addEventListener('click', () => {
      tabs.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeTab = btn.dataset.tab;
      applyFilters();
    });
  });

  if (topicSelect) {
    topicSelect.addEventListener('change', () => {
      activeTopic = topicSelect.value;
      recountTabs();
      applyFilters();
    });
  }
  // Initial state: show the intro for the default active tab. Without
  // this, the writer has to click a tab before any definition appears.
  applyFilters();

  const techToggle = document.getElementById('tech-toggle');
  if (techToggle) {
    techToggle.addEventListener('change', () => {
      document.body.classList.toggle('show-tech', techToggle.checked);
    });
  }
})();

// Health-check the assistant before submitting the "Run migration" form.
// The report works as a standalone .html file (you can email or open it
// from disk) but the migration button only does anything when the local
// assistant is running. Without this check, a writer who clicks the
// button while the assistant is closed sees a confusing browser-level
// "this site can't be reached" page. Instead, we ping the server first
// and surface a clear "open the assistant first" alert.
//
// We treat ANY HTTP response as "server is alive" — the stdlib
// BaseHTTPRequestHandler we use returns 501 for unimplemented HEAD,
// which a stricter `res.ok` check would mis-classify as "server down."
// Only an actual network failure (fetch rejects) means the assistant
// isn't running.
window.__ditaParityCheckServer = function (ev) {
  ev.preventDefault();
  const form = ev.target;
  fetch('/', {method: 'GET', cache: 'no-store'})
    .then(() => { form.submit(); })
    .catch(() => {
      window.alert(
        "The DITA Parity Assistant must be running to apply the " +
        "migration.\\n\\nIf the assistant is closed, open it and try " +
        "again. The black assistant window must stay open while you " +
        "click Run migration now."
      );
    });
  return false;
};
// Same gate for plain anchor links (e.g. "Start a new migration" on the
// applied-report panel). Without it, a writer who closed the assistant
// and clicked the link saw a browser-level 404 instead of a clear
// reminder to reopen the assistant first.
window.__ditaParityCheckServerLink = function (ev) {
  ev.preventDefault();
  const link = ev.currentTarget;
  const href = link.getAttribute('href') || '/';
  fetch('/', {method: 'GET', cache: 'no-store'})
    .then(() => { window.location.href = href; })
    .catch(() => {
      window.alert(
        "The DITA Parity Assistant must be running to start a new " +
        "migration.\\n\\nIf the assistant is closed, open it and try " +
        "again. The black assistant window must stay open to use " +
        "the tool."
      );
    });
  return false;
};
</script>
"""


_FOOT_HTML = "</body></html>"
