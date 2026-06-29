"""Parse Help Center HTML into a flat list of article blocks.

The Help Center renders articles with a small, stable set of semantic
containers we can target by `data-test-selector`. Compared with the
legacy plain-text export, parsing the live HTML gives us:

  - Explicit block boundaries (no one-line-per-block guesswork)
  - Note (callout) detection without prefix heuristics — they live in
    a `callout-container` div with a class modifier naming the type
  - Steps as proper ordered-list items
  - Inline markup retained as visible text (we still flatten for now)

This is intentionally a focused parser for the current Help
Center templates. If they change markup, this needs updating; the
mapping is in `_CONTAINER_SELECTORS` and `_CALLOUT_KIND_RE`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import List, Optional


@dataclass
class HtmlArticleBlock:
    """One logical content block extracted from a Help Center page."""
    text: str
    kind: str = "paragraph"   # paragraph | note | step | table_row | list_item | heading | tab_label
    note_kind: Optional[str] = None      # callout modifier (permission/warning/...)
    cells: Optional[List[str]] = None    # per-cell text for table_row blocks
    # Parallel list to `cells` — each entry is a list of bold/italic
    # phrases captured from <strong>/<b>/<em>/<i> inside that <td>/<th>.
    # The patch engine uses this to wrap each phrase in DITA <em> when
    # building the <entry>. Empty list = no emphasis in that cell.
    cell_emphasis: Optional[List[List[str]]] = None
    # Inline links captured as (text, href) pairs in document order.
    # Used to build <xref> elements when generating DITA suggestions.
    links: Optional[List[tuple]] = None
    # Inline emphasis phrases captured from <strong>/<b>/<em>/<i> in
    # the article. the source DITA uses <em> for all visual emphasis, so
    # the patch engine wraps these phrases in <em> when rebuilding an
    # element. A future layer (or human review) can upgrade <em> to
    # <uicontrol>/<wintitle>/<keyword> where semantically appropriate.
    emphasis: Optional[List[str]] = None
    # Populated when `expand_blocks_for_diff` merges a "Note:" / "Tip:"
    # label block + following list_items into a single note block. Each
    # entry is one original bullet's text. The patch engine uses this
    # to build a proper `<note><ul><li/></ul></note>` structure instead
    # of flattening the bullets into a single prose `<p>`.
    note_bullets: Optional[List[str]] = None
    # True if the article HTML for this block contained one or more
    # inline <img> elements (UI icon glyphs next to a button label, a
    # screenshot inside a step, etc.). We never auto-insert the image
    # element itself — we don't know the DITA href for it — but we DO
    # warn the writer that an icon/picture was present at this position
    # so they can place it manually after applying the new content.
    has_inline_image: bool = False
    # True specifically when this block has a content screenshot (e.g.
    # class="article-content__image"), as opposed to a small UI-icon
    # glyph (li-icon / svg next to a button label). Screenshots warrant
    # an explicit "image to add manually" advisory in the report; icon
    # glyphs do not (they're part of normal <uicontrol> text in DITA).
    has_screenshot: bool = False
    # Tab section affinity. When the block lives inside a <div
    # role="tabpanel" id="panel-X-...">, section_id is that id; outside
    # all tabpanels it's None. Used by the patch engine to refuse
    # inserts that would cross from one tab's content into another
    # tab's DITA topic.
    section_id: Optional[str] = None


# Container <div data-test-selector="..."> values we treat as blocks.
_CONTAINER_SELECTORS = {
    "preRenderedMarkup-container": "paragraph",
    "callout-container": "note",
}

# Article-content-callout__background--{kind}  -> kind
_CALLOUT_KIND_RE = re.compile(r"article-content-callout__background--(\w+)")

# Callout kinds whose <h3 ...callout__headline> is a publish-time
# marker phrase ("Important to know", "Who can use this feature?",
# "Here's a tip"). For THESE only, the headline is suppressed.
# For every other callout kind (pdf, feature, role, etc.) the
# headline carries real body content (a filename, feature name, role
# name) and must be captured — otherwise the source DITA shows that
# content as a heading paragraph inside the <note>, the article
# parser drops it, and the diff sees a phantom REPLACE.
# Map article-side callout headline marker phrases to the DITA
# `<note @type>` value the IM expects. Some Help Center callouts share
# the generic `--note` CSS class but the visible headline ("Here's a
# tip", "Important", etc.) is what tells the writer which note type
# to use. Without this mapping a content REPLACE on a `<note>` would
# write the new prose but leave the (now-wrong) `@type` attribute in
# place. Order doesn't matter — first substring match wins per item.
_HEADLINE_TO_NOTE_KIND = (
    ("here's a tip", "tip"),
    ("heres a tip", "tip"),
    ("important to know", "important"),
    ("important", "important"),
    ("caution", "caution"),
    ("warning", "warning"),
    ("remember to", "remember"),
    ("remember", "remember"),
    ("attention", "attention"),
    ("who can use this feature", "permission"),
)


def _note_kind_from_headline(headline: str):
    """Return a normalized note_kind ('tip', 'important', etc.) when
    `headline` is a recognized callout marker phrase, else None.
    Matches are case-insensitive and apostrophe-insensitive so curly
    quotes don't break the lookup."""
    if not headline:
        return None
    h = headline.lower().replace("’", "'").strip().rstrip(":.!?")
    for phrase, kind in _HEADLINE_TO_NOTE_KIND:
        if phrase in h:
            return kind
    return None


_MARKER_CALLOUT_KINDS = frozenset({
    "permission",
    "important",
    "tip",
    "warning",
    "caution",
    "remember",
    "attention",
    "note",
})

# Ordered-list items at the article-content level become "step" blocks.
_ORDERED_LIST_ITEM_CLASS = "article-content__ordered-list-item"
# Unordered-list items at the article-content level become "unordered_step"
# blocks — the patch engine routes those into <steps-unordered> for task
# topics and falls back to <ul><li> for concept/reference.
_UNORDERED_LIST_ITEM_CLASS = "article-content__unordered-list-item"

# We do NOT include callout headlines in the body text — they are the
# publish-time marker phrases ("Who can use this feature?", etc.) and
# are already handled by IM_note05 prefix stripping if we keep them.
_HEADLINE_CLASS = "article-content-callout__headline"

_WS_RE = re.compile(r"\s+")


def _inside_wrapper(stack) -> bool:
    return any(s.get("wrapper") for s in stack)


def _inside_cell(stack) -> bool:
    """True when any ancestor on the stack is an open <td>/<th>.

    Used to suppress the "<p>/<li> inside a rich-text wrapper is its own
    block" rule when those tags appear *inside* a table cell. Otherwise
    table cell content gets emitted as standalone paragraph blocks and
    never makes it into the row's cells list, leaving rows with only
    their first-cell text.
    """
    return any("cell_buffer" in s for s in stack)


def _mark_wrapper_delegated(stack) -> None:
    for s in reversed(stack):
        if s.get("wrapper"):
            s["children_emitted"] = (s.get("children_emitted") or 0) + 1
            return


def _flush_pending_list_item_text(stack, blocks, panel_stack) -> None:
    """When a nested list opens inside an open list-item container,
    emit the parent's buffered text as a block FIRST.

    Without this, an article like
        <li>Outer body text<ul><li>Inner</li></ul></li>
    emits blocks in the order [Inner, Outer body] (the inner closes
    first), which scrambles the diff: the source DITA has the outer
    text BEFORE the nested element, so SequenceMatcher can't pair
    matching content across the two orderings and produces phantom
    INSERTs / DELETEs.
    """
    for parent in reversed(stack):
        kind = parent.get("kind")
        if not parent.get("container") or kind not in (
            "step", "unordered_step", "list_item"
        ):
            continue
        buffered = "".join(parent.get("buffer") or []).translate(_INVISIBLE_MAP)
        buffered = _WS_RE.sub(" ", buffered).strip()
        if not buffered:
            return
        blocks.append(
            HtmlArticleBlock(
                text=buffered,
                kind=kind,
                links=parent.get("links") or None,
                emphasis=parent.get("emphasis") or None,
                section_id=panel_stack[-1] if panel_stack else None,
            )
        )
        # Reset the parent's accumulators so its </li> close doesn't
        # re-emit the same text.
        parent["buffer"] = []
        parent["links"] = []
        parent["emphasis"] = []
        return


def _enclosing_list_item_kind(stack) -> Optional[str]:
    """Return 'step'/'unordered_step' if a list-item container is open.

    Article bullets are rendered as
        <li class="article-content__...-list-item">
          <div ...="preRenderedMarkup-container">
            <p>bullet text</p>
          </div>
        </li>
    so the inner <p> is what carries the text. Without this lookup the
    <p>-inside-wrapper handler would tag the block as a plain paragraph
    and the patch engine would never reach the step/list-item builders.
    """
    for s in reversed(stack):
        kind = s.get("kind")
        if kind in ("step", "unordered_step"):
            return kind
    return None

# Invisible characters that Help Center editors sometimes paste into content
# (BOM, zero-widths). Stripped at ingest so they never reach the diff or
# the patched .dita output.
_INVISIBLE_MAP = str.maketrans({
    "﻿": None,
    "​": None,
    "‌": None,
    "‍": None,
})


def extract_canonical_url(html_text: str) -> Optional[str]:
    """Pull the article's canonical URL out of the Help Center HTML.

    Help Center pages put their canonical link in `<meta property="og:url"
    content="...">` (Open Graph). When a writer uploads a saved HTML
    file instead of pasting a URL, we use this to surface the live
    article URL in the report header — so a reader always knows which
    live article the report is comparing against.

    Returns the URL string or None if not found.
    """
    if not html_text:
        return None
    # Match either attribute order:
    #   <meta property="og:url" content="https://...">
    #   <meta content="https://..." property="og:url">
    m = re.search(
        r'<meta[^>]*property=["\']og:url["\'][^>]*content=["\']([^"\']+)["\']',
        html_text, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip() or None
    m = re.search(
        r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:url["\']',
        html_text, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip() or None
    # Fallback: <link rel="canonical" href="...">
    m = re.search(
        r'<link[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']',
        html_text, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip() or None
    return None


def parse_help_center_html(html_text: str) -> List[HtmlArticleBlock]:
    parser = _Parser()
    parser.feed(html_text)
    _retag_stem_sentences(parser.blocks)
    return parser.blocks


def _retag_stem_sentences(blocks: List[HtmlArticleBlock]) -> None:
    """Re-tag stem-sentence paragraphs as headings so the patch engine
    routes them to <stepsection> in task topics.

    Pattern: a `paragraph` block whose text ends with ":" and whose
    very next block is a `step` (ordered list item) is a stem sentence
    introducing a series of steps — e.g. "To do X, complete these
    steps:". Per IM page 33-45, this becomes <stepsection> in a task
    topic. We tag it as kind="heading" so the heading dispatch fires.
    """
    for i, block in enumerate(blocks):
        if block.kind != "paragraph":
            continue
        text = (block.text or "").rstrip()
        if not text.endswith(":"):
            continue
        # Find the next block with the same section_id (skip blank /
        # other section blocks).
        j = i + 1
        while j < len(blocks) and not (blocks[j].text or "").strip():
            j += 1
        if j >= len(blocks):
            continue
        nxt = blocks[j]
        if nxt.section_id != block.section_id:
            continue
        if nxt.kind == "step":
            blocks[i] = HtmlArticleBlock(
                text=block.text,
                kind="heading",
                note_kind=block.note_kind,
                cells=block.cells,
                links=block.links,
                emphasis=block.emphasis,
                section_id=block.section_id,
            )


def blocks_to_strings(blocks: List[HtmlArticleBlock]) -> List[str]:
    """Flatten parsed blocks to the list[str] shape the diff engine accepts."""
    texts, _ = expand_blocks_for_diff(blocks)
    return texts


_NOTE_LABEL_ONLY_RE = re.compile(
    r"^\s*(?:note|tip|warning|caution|important|remember|attention)"
    r"\s*[:\-–—]?\s*$",
    re.IGNORECASE,
)


def expand_blocks_for_diff(
    blocks: List[HtmlArticleBlock],
) -> tuple[List[str], List[HtmlArticleBlock]]:
    """Return (texts, origins) parallel lists.

    Two transforms:

    1. Callouts containing bullet runs are split into one entry per
       bullet, so each split line aligns 1:1 against a DITA <li>.

    2. A short "Note:" / "Tip:" / etc. block followed by consecutive
       list_item blocks is MERGED into a single block. The Help Center
       renders an in-step note as
           <p>Note:</p><ul><li>…</li><li>…</li></ul>
       which the parser emits as N+1 separate blocks, but the source
       DITA stores the same content as a single collapsed
           <note><ul><li>…</li><li>…</li></ul></note>
       block. Without the merge, LCS can't pair 1 source block to N
       article blocks and the article side gets re-INSERTed as a new
       step + bullets — producing a duplicate.
    """
    texts: List[str] = []
    origins: List[HtmlArticleBlock] = []
    # A "list-item-like" kind is any block the parser emits for one item
    # in a list:
    #   - `list_item`     → `<li>` nested inside a rich-text wrapper
    #   - `unordered_step` → `<li class="article-content__unordered-list-item">`
    #                       (top-level Help Center unordered list)
    #   - `step`          → ordered counterpart of the above
    # The note-label merge below was originally only checking `list_item`,
    # which missed the Available-payment-methods beta bug: the article
    # rendered "Important:" as a paragraph followed by a top-level
    # unordered list (kind=`unordered_step`), so the merge skipped and
    # each bullet became a phantom <p> INSERT next to the source
    # <note><ul><li> that already held the same content.
    LIST_ITEM_KINDS = ("list_item", "unordered_step", "step")
    i = 0
    while i < len(blocks):
        b = blocks[i]
        # Note-label + consecutive list-item blocks → one merged block.
        if (
            b.text
            and _NOTE_LABEL_ONLY_RE.match(b.text)
            and i + 1 < len(blocks)
            and blocks[i + 1].kind in LIST_ITEM_KINDS
        ):
            j = i + 1
            bullets: List[str] = []
            while j < len(blocks) and blocks[j].kind in LIST_ITEM_KINDS:
                if blocks[j].text:
                    bullets.append(blocks[j].text)
                j += 1
            if bullets:
                combined = " ".join(bullets)
                texts.append(combined)
                # Synthesize a fresh origin block tagged kind="note"
                # so the patch engine routes the INSERT to the note
                # builder (and gets the bullet list via note_bullets).
                origins.append(
                    HtmlArticleBlock(
                        text=combined,
                        kind="note",
                        note_kind=b.note_kind,
                        note_bullets=list(bullets),
                        section_id=b.section_id,
                    )
                )
                i = j
                continue
        if b.kind == "note" and ("•" in b.text or " • " in f" {b.text} "):
            for line in _split_bullets(b.text):
                if line:
                    texts.append(line)
                    origins.append(b)
        else:
            if b.text:
                texts.append(b.text)
                origins.append(b)
        i += 1
    return texts, origins


def _split_bullets(text: str) -> List[str]:
    """Split text on the bullet character, returning trimmed, non-empty pieces."""
    pieces = re.split(r"\s*•\s*", text)
    return [_WS_RE.sub(" ", p).strip(" \t ") for p in pieces if p.strip()]


# --- Parser ------------------------------------------------------------- #

class _Parser(HTMLParser):
    """SAX-style walker that emits blocks as containers close.

    State is a stack of dicts. Each "container" pushed onto the stack
    knows what kind of block it represents and accumulates text in its
    own buffer. Text from inner tags (<strong>, <em>, etc.) flows into
    the innermost container's buffer. <br> inside a container becomes
    a newline so we can later split bullet runs.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[HtmlArticleBlock] = []
        self._stack: List[dict] = []
        # Count of currently-open elements that should suppress text capture
        # (callout headlines, <script>, <style>). Nested-safe via a counter.
        self._suppress_depth: int = 0
        # Whether we are inside the primary <article data-test-selector="article">
        # element. Anything outside (e.g. "People also viewed" collapsibles) is
        # not part of the article body and must not become a block.
        self._in_article: bool = False
        # Stack of <div role="tabpanel" id="..."> ids currently open.
        # The innermost (last) entry is the active section for any
        # block emitted while parsing.
        self._panel_stack: List[str] = []
        # Tab-label text captured from tabs__tab-label spans but not
        # yet emitted. In Help Center HTML the buttons (with the labels)
        # appear BEFORE all panels, while in source DITA the navtitle
        # for each tab is INTERLEAVED with its topic content. We buffer
        # the labels here and re-emit them at panel-open time so the
        # article-side block order matches the source-side order.
        # Without this, SequenceMatcher's LCS can't align both orderings
        # at once and the diff degenerates into wrong cross-tab matches.
        self._pending_tab_labels: List[str] = []
        # True once we've finished parsing every tabpanel in the article
        # (the panel stack went from non-empty back to empty). Per the
        # IM conversion convention, any content that comes AFTER all
        # tabs goes to the post-tab .dita topic. Blocks emitted while
        # this flag is True (and the panel stack is empty) get a
        # synthetic section_id="__post_tab__" so the topic-binding
        # layer can route them to the right .dita file.
        self._tabs_have_closed: bool = False
        self._post_tab_section_id: str = "__post_tab__"

    # --- Stack helpers --- #
    def _innermost_container(self) -> Optional[dict]:
        # Cell buffers take precedence: when we're inside <td>/<th>, text
        # belongs to the cell, not the row container.
        for entry in reversed(self._stack):
            if "cell_buffer" in entry or "buffer" in entry:
                return entry
        return None

    # --- Tag handlers --- #
    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")
        tsel = attrs_dict.get("data-test-selector", "")
        entry: dict = {"tag": tag}

        if tag in ("script", "style"):
            entry["suppress"] = True
            self._suppress_depth += 1

        elif tag == "article" and tsel == "article":
            entry["enters_article"] = True
            self._in_article = True

        # The article title <h1 data-test-selector="heading-title"> lives
        # OUTSIDE the <article> element, so handle it before the in-article
        # gate. Without this, the diff sees no article-side title and emits
        # a DELETE on the DITA <title>.
        elif tag == "h1" and tsel == "heading-title":
            entry["container"] = True
            entry["kind"] = "paragraph"
            entry["buffer"] = []

        elif not self._in_article:
            # Outside the primary article: still track depth via the stack
            # so end tags match up, but don't recognize any block containers.
            self._stack.append(entry)
            return

        elif tag == "div" and attrs_dict.get("role") == "tabpanel":
            # Marks the start of one tab's content. id="panel-1-..." /
            # "panel-2-...". We push the id so any block emitted while
            # the panel is open carries that section_id.
            panel_id = attrs_dict.get("id") or ""
            if panel_id:
                entry["panel_id"] = panel_id
                self._panel_stack.append(panel_id)
                # Flush the next pending tab label as the FIRST block
                # of this panel. This makes the article block stream
                # interleave labels with content the same way the
                # source DITA interleaves navtitles with topic content,
                # so SequenceMatcher's LCS can align both correctly.
                if self._pending_tab_labels:
                    label = self._pending_tab_labels.pop(0)
                    self.blocks.append(
                        HtmlArticleBlock(
                            text=label,
                            kind="tab_label",
                            section_id=panel_id,
                        )
                    )

        elif tag == "div" and tsel == "preRenderedMarkup-container":
            # Rich-text wrapper. If it contains <p>/<li> children, each of
            # those becomes its own block and the wrapper emits nothing.
            # If it contains only flat text (no child containers fire),
            # we fall back to one block for the wrapper. Capture mode for
            # delegation is tracked via children_emitted on the wrapper.
            entry["wrapper"] = True
            entry["kind"] = "paragraph"
            entry["buffer"] = []
            entry["children_emitted"] = 0

        elif tag == "div" and tsel == "callout-container":
            # Notes keep the single-block behavior — the reconstructor's
            # note-collapse rule already aligns multi-paragraph DITA notes
            # against a single article callout block.
            entry["container"] = True
            entry["kind"] = "note"
            entry["buffer"] = []
            m = _CALLOUT_KIND_RE.search(cls)
            entry["note_kind"] = m.group(1) if m else None

        elif tag == "li" and _ORDERED_LIST_ITEM_CLASS in cls:
            _flush_pending_list_item_text(self._stack, self.blocks, self._panel_stack)
            entry["container"] = True
            entry["kind"] = "step"
            entry["buffer"] = []

        elif tag == "li" and _UNORDERED_LIST_ITEM_CLASS in cls:
            _flush_pending_list_item_text(self._stack, self.blocks, self._panel_stack)
            entry["container"] = True
            entry["kind"] = "unordered_step"
            entry["buffer"] = []

        # <h2>/<h3>/<h4> inside a rich-text wrapper signal a section
        # heading — the Help Center renders subsection titles
        # this way. Each becomes its own block tagged kind="heading" so
        # the patch engine can build a DITA <section><title>...</section>
        # rather than a stray <p>. Same wrapper-delegation rules as <p>.
        elif tag in ("h2", "h3", "h4") and _inside_wrapper(self._stack) and not _inside_cell(self._stack):
            entry["container"] = True
            entry["kind"] = "heading"
            entry["buffer"] = []
            _mark_wrapper_delegated(self._stack)

        # Inside a rich-text wrapper, <p> is the unit. The wrapper marks
        # itself "delegated" so it won't also emit a fallback block.
        # But suppress this when we're inside a <td>/<th>: table cells
        # capture their content into the row's cells list via cell_buffer,
        # and a competing paragraph container would steal the text.
        elif tag == "p" and _inside_wrapper(self._stack) and not _inside_cell(self._stack):
            entry["container"] = True
            # Inherit step / unordered_step from the enclosing <li> if any;
            # otherwise this is a plain paragraph inside a rich-text wrapper.
            entry["kind"] = _enclosing_list_item_kind(self._stack) or "paragraph"
            entry["buffer"] = []
            _mark_wrapper_delegated(self._stack)

        # <li> inside a list inside the wrapper becomes a list_item block.
        # Same cell-guard as <p>: lists in table cells stay with the cell.
        elif tag == "li" and _inside_wrapper(self._stack) and not _inside_cell(self._stack):
            _flush_pending_list_item_text(self._stack, self.blocks, self._panel_stack)
            entry["container"] = True
            entry["kind"] = "list_item"
            entry["buffer"] = []
            _mark_wrapper_delegated(self._stack)

        # Inline link capture. We don't make <a> a container; instead we
        # record (text, href) so the patch engine can build <xref>s later.
        elif tag == "a":
            href = attrs_dict.get("href", "")
            if href:
                entry["link_capture"] = href
                entry["link_text_start"] = None  # filled when text arrives

        # Inline emphasis capture. <strong>/<b>/<em>/<i> all map to
        # DITA <em> in the project's convention. Record the wrapped text so
        # the patch engine can re-wrap it in <em> when rebuilding an
        # element. Same pattern as link_capture: we don't make these
        # tags containers, we just observe.
        elif tag in ("strong", "b", "em", "i"):
            entry["emphasis_capture"] = True

        # Inline images / icons. The Help Center renders UI button
        # icons ("✱ More"), inline screenshots, and other media inside
        # step text as <img>, <li-icon>, or <svg>. We CAN'T auto-place
        # these in the DITA (we don't know the matching href in the
        # the asset catalog), but we MUST flag the writer to add
        # them manually. Mark the innermost block container with
        # has_inline_image so the emitter can attach the flag to the
        # resulting block.
        #
        # Distinguish screenshots from icon glyphs:
        #   - article-content__image  → content screenshot. Warrants
        #     a per-image advisory in the report.
        #   - li-icon / small inline svg / icon-class → UI-icon glyph.
        #     Surfaced via the icon-presence advisory when it's in a
        #     new or rewritten line.
        elif tag in ("img", "picture", "svg", "li-icon"):
            is_screenshot = (
                tag in ("img", "picture")
                and "article-content__image" in cls
            )
            for ancestor in reversed(self._stack):
                if ancestor.get("container") or ancestor.get("wrapper"):
                    ancestor["has_inline_image"] = True
                    if is_screenshot:
                        ancestor["has_screenshot"] = True
                    break

        # Collapsible section header. The Help Center uses an expandable
        # button per section ("Available games", "Streaks", …); the visible
        # label lives inside this <span>. Without capturing it, the DITA
        # topic title (which exists for each section) has nothing to align
        # against and the diff becomes ambiguous around section boundaries.
        #
        # kind="expandable_header" distinguishes these from regular
        # paragraphs so the routing layer can use them as section
        # boundaries (each expandable maps to its own .dita topic in an
        # FAQ-style map) and emit a "new topic needed" advisory when
        # an expandable's text doesn't match any source topic title.
        elif tag == "span" and "article-content__collapsible-trigger-text" in cls:
            entry["container"] = True
            entry["kind"] = "expandable_header"
            entry["buffer"] = []

        # Tab labels (Desktop / Mobile / etc.). On an interactive Help
        # Center page, the tabbed sections are represented by
        # <button class="tabs__tab"><span class="tabs__tab-label">Label</span>.
        # We capture the label text into a pending queue and re-emit it
        # at panel-open time (see the tabpanel branch above) so the
        # tab_label appears immediately before its panel's content,
        # matching how DITA interleaves navtitles with topic content.
        # The span itself does NOT produce a block here.
        elif tag == "span" and "tabs__tab-label" in cls:
            entry["container"] = True
            entry["kind"] = "tab_label_pending"
            entry["buffer"] = []

        elif tag == "tr":
            # Each table row becomes its own block. Cells are captured
            # individually so the patch engine can build proper <entry>
            # children when the diff detects a new row.
            entry["container"] = True
            entry["kind"] = "table_row"
            entry["buffer"] = []
            entry["cells"] = []

        elif tag in ("td", "th"):
            # Cell collector. handle_data writes into the innermost cell
            # buffer when one is open; on close, the text is appended to
            # the parent row's cells list. cell_emphasis collects bold/
            # italic phrases from <strong>/<b>/<em>/<i> inside this cell
            # so the patch engine can wrap them in DITA <em> when
            # building the new <entry>.
            entry["cell_buffer"] = []
            entry["cell_emphasis"] = []

        elif tag == "h3" and _HEADLINE_CLASS in cls:
            # Headline inside a callout. For most callout kinds the
            # headline is a stylesheet decoration ("Who can use this
            # feature?", "Important to know", "Here's a tip") — those
            # are publish-time markers, not body content, so we
            # suppress capture. But for FILE-like callouts (PDF
            # downloads, attached documents, video assets), the
            # headline IS the body content (the filename or asset
            # name) and must be captured — otherwise the diff sees
            # the source's filename <p> as extra content and triggers
            # a phantom REPLACE.
            enclosing_callout = None
            enclosing_kind = None
            for ancestor in reversed(self._stack):
                if "note_kind" in ancestor:
                    enclosing_callout = ancestor
                    enclosing_kind = ancestor.get("note_kind")
                    break
            # Also capture the headline text into a local buffer on
            # the entry so handle_endtag can match it against the
            # known marker phrases. This lets us upgrade a generic
            # `--note` CSS class to a specific `<note @type="tip">`
            # when the headline reads "Here's a tip". Beta surfaced
            # this on Article 4 where the parent topic's note kept
            # @type="important" even though the article showed it as
            # a tip.
            entry["headline_capture"] = True
            entry["headline_text"] = []
            entry["enclosing_callout"] = enclosing_callout
            # Only suppress the headline if this is a known
            # marker-phrase callout kind. For every other kind (file
            # downloads, feature highlights, role gates, etc.) the
            # headline is real body content and must flow into the
            # parent callout buffer like any other text.
            if enclosing_kind in _MARKER_CALLOUT_KINDS:
                entry["suppress"] = True
                self._suppress_depth += 1
            elif enclosing_kind is None:
                # No enclosing callout we recognise — default to
                # suppressing (preserves the prior behaviour for
                # any stray headline outside a callout).
                entry["suppress"] = True
                self._suppress_depth += 1
            else:
                # Recognised non-marker callout — capture the
                # headline as body text.
                pass

        elif tag == "br":
            # Treat <br> as a soft separator so bullet runs become splittable.
            container = self._innermost_container()
            if container is not None and self._suppress_depth == 0:
                container["buffer"].append("\n")
            # <br> is void — don't push to stack.
            return

        self._stack.append(entry)

    def handle_endtag(self, tag: str) -> None:
        # Find the matching opener; unwind unmatched intermediates.
        while self._stack and self._stack[-1]["tag"] != tag:
            self._pop_one()
        if not self._stack:
            return
        self._pop_one()

    def handle_startendtag(self, tag, attrs) -> None:  # e.g. <br/>
        self.handle_starttag(tag, attrs)
        # <br> is the only void we care about and is handled in starttag.

    def handle_data(self, data: str) -> None:
        # Capture callout headline text BEFORE the suppress check.
        # The h3.callout-headline branch sets suppress=True for marker
        # callouts so the body text doesn't include the publish-time
        # heading — but we still need the heading text for the note-
        # type upgrade (e.g. headline "Here's a tip" → type="tip"
        # even when the CSS class is the generic `--note`).
        for entry in reversed(self._stack):
            if entry.get("headline_capture"):
                entry["headline_text"].append(data)
                break
        if self._suppress_depth > 0:
            return
        container = self._innermost_container()
        if container is None:
            return
        target_key = "cell_buffer" if "cell_buffer" in container else "buffer"
        container[target_key].append(data)

        # Inline link text: if we're inside an <a href>, remember the text
        # along with the href so the patch can later wrap it in <xref>.
        for entry in reversed(self._stack):
            if "link_capture" in entry:
                if "link_text" not in entry:
                    entry["link_text"] = []
                entry["link_text"].append(data)
                break

        # Inline emphasis text: same idea, but for <strong>/<b>.
        for entry in reversed(self._stack):
            if entry.get("emphasis_capture"):
                if "emphasis_text" not in entry:
                    entry["emphasis_text"] = []
                entry["emphasis_text"].append(data)
                break

    # --- Internal: emit block when its container closes --- #
    def _pop_one(self) -> None:
        entry = self._stack.pop()
        if entry.get("suppress"):
            self._suppress_depth -= 1
        if entry.get("enters_article"):
            self._in_article = False
        if entry.get("panel_id") and self._panel_stack and self._panel_stack[-1] == entry["panel_id"]:
            self._panel_stack.pop()
            # If the stack just emptied, every panel we've seen so far
            # is closed. Mark subsequent blocks as post-tab content.
            if not self._panel_stack:
                self._tabs_have_closed = True

        # Closing the callout headline: match the captured headline
        # text against marker phrases and (if matched) upgrade the
        # enclosing callout's note_kind. The text is captured in a
        # parallel buffer separate from the suppressed body buffer, so
        # the body block emitted later still doesn't include the
        # headline as content.
        if entry.get("headline_capture"):
            headline = "".join(entry.get("headline_text") or [])
            headline = headline.translate(_INVISIBLE_MAP)
            headline = _WS_RE.sub(" ", headline).strip()
            new_kind = _note_kind_from_headline(headline)
            if new_kind is not None:
                enclosing = entry.get("enclosing_callout")
                if enclosing is not None:
                    enclosing["note_kind"] = new_kind
            # Fall through to the rest of _pop_one (suppress depth
            # tracking, etc.) — don't return early.

        # Closing an <a>: forward the (text, href) pair to the nearest
        # ancestor container so it can be attached to the emitted block.
        if "link_capture" in entry:
            link_text = "".join(entry.get("link_text") or [])
            # The block text we'll search later was already whitespace-
            # collapsed by _pop_one. Normalize the link text the same way
            # so a captured "Save  changes" still finds the body's
            # "Save changes".
            link_text = link_text.translate(_INVISIBLE_MAP)
            link_text = _WS_RE.sub(" ", link_text).strip()
            if link_text:
                href = entry["link_capture"]
                for parent in reversed(self._stack):
                    if "buffer" in parent or "cell_buffer" in parent:
                        parent.setdefault("links", []).append((link_text, href))
                        break
            return

        # Closing a <strong>/<b>: forward the emphasis text up to the
        # nearest container with a buffer, same shape as link forwarding.
        # Inside a <td>/<th>, the emphasis goes onto the cell's own
        # list (cell_emphasis) so the patch engine can later wrap the
        # phrase in <em> within the corresponding <entry>.
        if entry.get("emphasis_capture"):
            em_text = "".join(entry.get("emphasis_text") or [])
            em_text = em_text.translate(_INVISIBLE_MAP)
            em_text = _WS_RE.sub(" ", em_text).strip()
            if em_text:
                for parent in reversed(self._stack):
                    if "cell_buffer" in parent:
                        parent.setdefault("cell_emphasis", []).append(em_text)
                        break
                    if "buffer" in parent:
                        parent.setdefault("emphasis", []).append(em_text)
                        break
            return

        # Closing a <td>/<th>: drain the cell buffer into the parent row's
        # cells list, don't emit a standalone block. Also drain the
        # cell's emphasis list (bold/italic phrases) into a parallel
        # cell_emphasis list on the row so the patch engine knows which
        # phrases to wrap in <em> when building the entry.
        if "cell_buffer" in entry:
            cell_text = "".join(entry["cell_buffer"]).translate(_INVISIBLE_MAP)
            cell_text = _WS_RE.sub(" ", cell_text).strip()
            cell_emphasis = entry.get("cell_emphasis") or []
            for parent_entry in reversed(self._stack):
                if parent_entry.get("kind") == "table_row":
                    parent_entry["cells"].append(cell_text)
                    parent_entry.setdefault("cell_emphasis", []).append(cell_emphasis)
                    break
            return

        # Closing a wrapper (preRenderedMarkup-container). If any child
        # container emitted a block, the wrapper itself emits nothing —
        # the children own the content. Otherwise fall back to one block.
        if entry.get("wrapper"):
            if (entry.get("children_emitted") or 0) > 0:
                return
            text = "".join(entry["buffer"]).translate(_INVISIBLE_MAP)
            text = re.sub(r"[ \t\f\v\xa0]+", " ", text)
            text = re.sub(r" *\n *", "\n", text).strip()
            if not text:
                return
            # Inherit list-item kind from the enclosing <li> when the
            # wrapper sits directly inside a top-level list item. The
            # common Help Center pattern is
            #     <li class="…unordered-list-item">
            #       <div data-test-selector="preRenderedMarkup-container">
            #         bullet text
            #       </div>
            #     </li>
            # without an explicit <p> inside the wrapper. Without this
            # inheritance the wrapper emits as kind="paragraph", which
            # then prevents the note-label merge in
            # expand_blocks_for_diff (it only merges Note/Important +
            # list-item-like followers, never + bare paragraphs). The
            # Available-payment-methods beta bug was rooted here:
            # "Important:" + 4 paragraphs ended up unmerged and each
            # bullet INSERTed as a phantom <p> next to the existing
            # <note><ul><li>.
            kind = _enclosing_list_item_kind(self._stack) or "paragraph"
            self.blocks.append(
                HtmlArticleBlock(
                    text=text,
                    kind=kind,
                    links=entry.get("links") or None,
                    emphasis=entry.get("emphasis") or None,
                    has_inline_image=bool(entry.get("has_inline_image")),
                    has_screenshot=bool(entry.get("has_screenshot")),
                    section_id=(
                        self._panel_stack[-1] if self._panel_stack
                        else (
                            self._post_tab_section_id
                            if self._tabs_have_closed else None
                        )
                    ),
                )
            )
            return

        if not entry.get("container"):
            return

        if self._panel_stack:
            section_id = self._panel_stack[-1]
        elif self._tabs_have_closed:
            # All tabs are closed and this block is at top level —
            # by IM convention it belongs to the post-tab .dita topic.
            section_id = self._post_tab_section_id
        else:
            section_id = None

        # tabs__tab-label spans are buffered, not emitted. We re-emit
        # them when their tabpanel opens so the article block stream
        # interleaves labels with content (matching source DITA order).
        if entry.get("kind") == "tab_label_pending":
            text = "".join(entry["buffer"]).translate(_INVISIBLE_MAP)
            text = _WS_RE.sub(" ", text).strip()
            if text:
                self._pending_tab_labels.append(text)
            return

        # Table rows are emitted from their cells, not from accumulated text.
        if entry.get("kind") == "table_row":
            cells = entry.get("cells", [])
            cell_emphasis_all = entry.get("cell_emphasis") or []
            # Filter empty cells AND the parallel emphasis lists.
            filtered = [
                (c, cell_emphasis_all[i] if i < len(cell_emphasis_all) else [])
                for i, c in enumerate(cells)
                if c
            ]
            if not filtered:
                return
            cells = [c for c, _ in filtered]
            cell_emphasis = [e for _, e in filtered]
            text = " ".join(cells).strip()
            self.blocks.append(
                HtmlArticleBlock(
                    text=text, kind="table_row", cells=cells,
                    cell_emphasis=cell_emphasis if any(cell_emphasis) else None,
                    section_id=section_id,
                )
            )
            return

        text = "".join(entry["buffer"]).translate(_INVISIBLE_MAP)
        # Collapse whitespace except newlines (we use newlines to mark <br>
        # boundaries for later bullet splitting).
        text = re.sub(r"[ \t\f\v\xa0]+", " ", text)
        text = re.sub(r" *\n *", "\n", text).strip()
        if not text:
            # Forward inline-image / screenshot flags to the most-
            # recently emitted block. Without this, a screenshot that
            # sits inside an <li> AFTER the li's <p> already delegated
            # its content (the common Help Center pattern) would lose
            # its flag — the wrapping <li> pops without emitting and
            # the flag goes with it. Beta surfaced this as "the tool
            # didn't flag the new image in the article."
            if (
                (entry.get("has_inline_image") or entry.get("has_screenshot"))
                and self.blocks
            ):
                last = self.blocks[-1]
                if entry.get("has_inline_image"):
                    last.has_inline_image = True
                if entry.get("has_screenshot"):
                    last.has_screenshot = True
            return
        block = HtmlArticleBlock(
            text=text,
            kind=entry.get("kind", "paragraph"),
            note_kind=entry.get("note_kind"),
            links=entry.get("links") or None,
            emphasis=entry.get("emphasis") or None,
            has_inline_image=bool(entry.get("has_inline_image")),
            has_screenshot=bool(entry.get("has_screenshot")),
            section_id=section_id,
        )
        self.blocks.append(block)
