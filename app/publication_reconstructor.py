"""Reconstruct the linear publication text from DITA topics in map order.

Each emitted Block is one logical DITA content unit (title, paragraph,
list item, note). Every block carries the ownership metadata the diff
engine and patch engine need:

  - topic_id        which topic the block came from
  - element_xpath   precise location inside that topic's XML tree
  - block_index     sequential index in the full publication

We deliberately do NOT walk into related-links or sections marked as
"Related tasks" / "Learn more" content. Those are flagged auto_update=False
upstream of the diff so they never get auto-patched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple
import re
import xml.etree.ElementTree as ET

from app.map_parser import MapLabel, TopicRef


# Block-bearing DITA elements we extract text from.
BLOCK_TAGS = {
    "title", "shortdesc", "p", "li", "note",
    "cmd", "info", "stepresult", "stepsection",
    "dlentry",
    # Table rows: each <row> in a DITA <table> becomes one block. The row's
    # text concatenates its <entry> contents, which matches how the article
    # HTML parser emits one block per <tr>.
    "row",
}

# Block tags whose content is structural (children carry semantic roles like
# dt/dd). REPLACE on these would destroy the structure, so they are marked
# auto_update=False at reconstruction time. The diff still detects changes
# in them; the patch engine just won't apply the change.
STRUCTURAL_BLOCK_TAGS = {"dlentry", "row"}

# Tags whose content we skip entirely (handled separately or not yet supported).
SKIP_TAGS = {
    "related-links", "linklist", "linkpool",
    "prolog", "topicmeta",
}

# Section titles (case-insensitive) that mark "do not auto-update" zones.
MANUAL_REVIEW_TITLES = {
    # Headings that mark a reltable section in the article. Individual
    # entries are not diffed; the writer is routed to the .ditamap.
    "related task", "related tasks",
    "related topic", "related topics",
    "related article", "related articles",
    "related link", "related links",
    "learn more", "learn more about",
}


@dataclass(frozen=True)
class Block:
    block_index: int           # position in the full publication, 0-based
    topic_id: str              # href from the map
    topic_path: Path           # resolved path on disk
    element_xpath: str         # e.g. "/topic/body/p[2]"
    element_tag: str           # local tag name, e.g. "p", "note", "title"
    text: str                  # rendered text content (whitespace preserved)
    note_type: Optional[str] = None    # for <note>, the type attribute value
    auto_update: bool = True   # False if patch engine must not auto-apply
    skip_reason: Optional[str] = None  # populated when auto_update=False
    # Why this block is unsafe — split apart because the rules differ for
    # different ops. INSERT after a structural block (a sibling row, a new
    # dlentry) is safe; REPLACE/DELETE on that same block is not.
    in_manual_zone: bool = False  # Related tasks / Learn more / See also
    structural: bool = False       # multi-child structural element (row/dlentry)
    # Hrefs of any <xref> descendants inside this block, in document
    # order. Used by the patch engine to:
    #   1. Refuse a DELETE when a sibling shares the same visible text
    #      but a different href (genuinely ambiguous — tool can't pick
    #      a winner).
    #   2. Flag an EQUAL pairing whose source href doesn't match the
    #      article's link href (the DITA href is stale — common after
    #      the article-ID format change from /87951 to /a1342713).
    xref_hrefs: Tuple[str, ...] = ()
    # Nesting depth of this block's containing entry in the .ditamap
    # (0 = top-level topic). Topic blocks inherit their TopicRef's
    # depth; synthesized <ditamap> navtitle blocks carry the depth of
    # the topichead/topicgroup that produced them. The tab-routing
    # builder uses this to detect when a topic drops out of a
    # navtitle's scope (sibling vs descendant), preventing post-tab
    # content from inheriting the last tab's section binding.
    map_depth: int = 0


@dataclass
class Publication:
    """The full reconstructed publication and its ownership index."""
    blocks: List[Block] = field(default_factory=list)

    @property
    def texts(self) -> List[str]:
        return [b.text for b in self.blocks]


def _local(tag: str) -> str:
    """Strip XML namespace prefix from a tag."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _element_text(elem: ET.Element) -> str:
    """Collect all visible text from an element, including children's tails."""
    parts: List[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        parts.append(_element_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _collect_xref_hrefs(elem: ET.Element) -> Tuple[str, ...]:
    """Return the hrefs of every <xref> descendant of `elem` in
    document order. Empty tuple when there are none. Used by the
    patch engine to detect (a) duplicate-text link siblings with
    different hrefs (ambiguous DELETEs) and (b) EQUAL pairings whose
    DITA href doesn't match the article's link href (stale href)."""
    hrefs: List[str] = []
    for descendant in elem.iter():
        if descendant is elem:
            continue
        if _local(descendant.tag) == "xref":
            href = descendant.attrib.get("href")
            if href:
                hrefs.append(href)
    return tuple(hrefs)


def _leading_inline_text(elem: ET.Element) -> str:
    """Return text + inline-child text from `elem` up to (but not
    including) the first block-bearing child.

    Used to recover the leading text of an <li> that mixes inline
    content with a child block element (e.g. `<li>Foo bar
    <note>Aside.</note></li>` — the "Foo bar" portion). Without this,
    such leading text is silently dropped (the `<note>` is the only
    block emitted), the article's matching bullet has no source
    counterpart, and it gets re-INSERTed as a duplicate.
    """
    parts: List[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        if _local(child.tag) in BLOCK_TAGS:
            break
        parts.append(_element_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


_BLOCK_CHILDREN_FOR_COLLAPSE = {
    "p", "li", "dt", "dd", "stepsection", "step", "cmd",
}


def _dlentry_collapsed_text(elem: ET.Element) -> str:
    """Render a <dlentry> source-side as the Help Center stylesheet
    renders it: "Term: Definition". Per IM page 123 the colon is added
    automatically by the stylesheet, so the article HTML shows "X: Y"
    even though the DITA author writes <dt>X</dt><dd>Y</dd>. Joining
    with ": " here makes source-side text match article-side text
    exactly, so the diff doesn't emit phantom REPLACE ops for what is
    really the same content.

    Falls back to plain element text if the <dlentry> has an unusual
    shape (no <dt> or no <dd>).
    """
    dt_text = ""
    dd_text = ""
    for child in elem:
        tag = _local(child.tag)
        if tag == "dt" and not dt_text:
            dt_text = _element_text(child).strip()
        elif tag == "dd" and not dd_text:
            dd_text = _element_text(child).strip()
    if dt_text and dd_text:
        return f"{dt_text}: {dd_text}"
    return _element_text(elem).strip()


# Block-level tags inside a <dd> that rule out an auto-applied REPLACE
# (a text rewrite would silently destroy these). Mirrors the patch
# engine's _DD_BLOCK_TAGS — kept here too so the diff layer can mark
# such dlentries structural without round-tripping through the patch
# engine.
_DD_BLOCK_TAGS_FOR_RECONSTRUCTION = (
    "p", "ul", "ol", "dl", "note", "table", "fig", "codeblock",
)


def _is_simple_dlentry_shape(elem: ET.Element) -> bool:
    """True when a <dlentry> has the shape the patch engine's
    auto-REPLACE handler can rewrite: exactly one <dt> + one <dd>,
    with <dd> containing only text and inline markup (no nested
    blocks). Complex dlentries (multi-term, nested ul/note in <dd>,
    etc.) stay structural so REPLACE surfaces them for review.
    """
    dts = [c for c in elem if _local(c.tag) == "dt"]
    dds = [c for c in elem if _local(c.tag) == "dd"]
    if len(dts) != 1 or len(dds) != 1:
        return False
    dd = dds[0]
    for descendant in dd.iter():
        if descendant is dd:
            continue
        if _local(descendant.tag) in _DD_BLOCK_TAGS_FOR_RECONSTRUCTION:
            return False
    return True


def _collapsed_block_text(elem: ET.Element) -> str:
    """Flatten an element's block children with separators between them.

    Used to collapse multi-paragraph structural elements (<note>, etc.)
    into a single source-side block. Without separators, paragraphs
    like <p>foo.pdf</p><p>Please review</p> concatenate to "foo.pdfPlease
    review" which (a) is unreadable in the report and (b) prevents the
    substring check downstream from recognising that the article's
    "Please review…" is contained in the source. Joining with a single
    space mirrors how the Help Center HTML parser collapses callout
    paragraphs (newlines between paragraphs, later normalized).
    """
    parts: List[str] = []
    if elem.text and elem.text.strip():
        parts.append(elem.text.strip())
    for child in elem:
        tag = _local(child.tag)
        if tag in ("ul", "ol"):
            for li in child:
                sub = _element_text(li).strip()
                if sub:
                    parts.append(sub)
        elif tag in _BLOCK_CHILDREN_FOR_COLLAPSE:
            sub = _element_text(child).strip()
            if sub:
                parts.append(sub)
        else:
            sub = _element_text(child).strip()
            if sub:
                parts.append(sub)
        if child.tail and child.tail.strip():
            parts.append(child.tail.strip())
    return " ".join(parts)


def _row_text(elem: ET.Element) -> str:
    """Canonical text for a DITA <row>: join each <entry>'s visible text
    with a single space, AND join block children inside each <entry>
    (multiple <p> elements) with a single space too.

    Compact XML serialization puts no whitespace between </entry> and
    the next <entry>, and an <entry> that contains multiple <p>/<ul>/<li>
    elements has no whitespace between them either. The default
    tail-walking concat collapses cells AND inner paragraphs into one
    run-on string ("PinpointPinpoint is a…"). The HTML article parser
    joins both with spaces, so we mirror that here.
    """
    cell_texts: List[str] = []
    for child in elem:
        if _local(child.tag) != "entry":
            continue
        # If the cell has block children (<p>, <ul>, <li>, etc.) join
        # them with spaces; otherwise fall back to plain flat text.
        has_block_child = any(
            _local(g.tag) in ("p", "li", "ul", "ol", "dl", "dlentry",
                              "dt", "dd", "note")
            for g in child
        )
        if has_block_child:
            cell_texts.append(_collapsed_block_text(child).strip())
        else:
            cell_texts.append(_element_text(child).strip())
    return " ".join(c for c in cell_texts if c)


def _has_block_descendants(elem: ET.Element) -> bool:
    """True if any descendant (not the element itself) is in BLOCK_TAGS.

    We emit a block only for the *leaf-most* block-bearing element. If a
    <note> contains a <p>, we emit the <p> (the leaf) and skip the <note>,
    avoiding duplicate blocks for the same text and avoiding destructive
    REPLACEs that would clobber the <note>'s structured children.
    """
    for descendant in elem.iter():
        if descendant is elem:
            continue
        if _local(descendant.tag) in BLOCK_TAGS:
            return True
    return False


def _note_has_only_paragraph_descendants(elem: ET.Element) -> bool:
    """True when every block descendant of `elem` is a <p>.

    Used to decide whether REPLACE on a <note> with children is safe.
    A note like `<note><p>...</p><p>...</p></note>` can be safely
    rewritten by clearing the <p>s and rebuilding them from the new
    text — no structural information is lost. A note containing a
    <ul>/<ol>/<table> can't (the article-side collapsed text doesn't
    preserve the bullet/row boundaries), so REPLACE stays refused for
    those.
    """
    saw_paragraph = False
    for descendant in elem.iter():
        if descendant is elem:
            continue
        tag = _local(descendant.tag)
        if tag in BLOCK_TAGS:
            if tag != "p":
                return False
            saw_paragraph = True
    return saw_paragraph


def _build_xpath(path_stack: List[str]) -> str:
    return "/" + "/".join(path_stack)


def reconstruct(entries: list) -> Publication:
    pub = Publication()

    for entry in entries:
        if isinstance(entry, MapLabel):
            _emit_map_label_block(entry, pub)
        else:
            _extract_topic_blocks(entry, pub)

    return pub


def _emit_map_label_block(label: MapLabel, pub: Publication) -> None:
    """Synthesize a publication block for a topichead/topicgroup navtitle.

    The text exists only in the .ditamap; there is no topic file to patch
    and no element_xpath inside a topic. We still emit it so the diff's
    sequential alignment treats the article's section heading as already
    accounted for. Marked auto_update=False because any change a user
    makes to this label belongs in the .ditamap, not a topic body.
    """
    pub.blocks.append(
        Block(
            block_index=len(pub.blocks),
            topic_id="<ditamap>",
            topic_path=Path("<ditamap>"),
            element_xpath=f"<topichead navtitle='{label.text}'>",
            element_tag="navtitle",
            text=label.text,
            auto_update=False,
            skip_reason=(
                "this heading lives in the .ditamap "
                "(<topichead>/<topicgroup>/<navtitle>), not in a topic body; "
                "edit the map directly"
            ),
            in_manual_zone=True,   # block REPLACE/DELETE/INSERT alike
            structural=False,
            map_depth=label.depth,
        )
    )


def _extract_topic_blocks(ref: TopicRef, pub: Publication) -> None:
    tree = ET.parse(ref.resolved_path)
    root = tree.getroot()

    # Track sibling indices per parent so we can build positional xpaths.
    # Stack entries: (element, child_counters_by_tag)
    def walk(
        elem: ET.Element,
        path_stack: List[str],
        in_manual_zone: bool,
        parent_tag: Optional[str] = None,
    ) -> None:
        tag = _local(elem.tag)

        if tag in SKIP_TAGS:
            return

        # A <table>'s own <title> ("List of Available games") has no
        # counterpart in HTML tables — the article-side just has the
        # surrounding section heading. Including it as a block shifts
        # the alignment by one and cascades into wrong REPLACEs on every
        # row of the table. Skip it. Topic / section / fig titles are
        # still emitted by the BLOCK_TAGS path below.
        if tag == "title" and parent_tag == "table":
            return

        # If this element is a <section> with a title in MANUAL_REVIEW_TITLES,
        # everything inside it is flagged auto_update=False.
        manual_here = in_manual_zone
        if tag == "section":
            title_text = _section_title(elem)
            if title_text and title_text.strip().lower() in MANUAL_REVIEW_TITLES:
                manual_here = True

        # Tables align at the row level. A DITA <row> often has a <p>
        # nested inside an <entry> for long cell descriptions. The
        # leaf-only rule below would see those descendants, refuse to
        # emit the <row>, then cherry-pick the inner <p>s as standalone
        # blocks — losing the cell label (the first <entry>) and
        # producing phantom INSERT ops on the article side. Emit the
        # row as an opaque block with combined entry text and don't
        # recurse into it.
        if tag == "row":
            text = _row_text(elem).strip()
            if text:
                # Rows are always structural — auto_update must be False
                # regardless of manual_here so REPLACE/DELETE land in the
                # structural-skip path before the inline-markup guard.
                pub.blocks.append(
                    Block(
                        block_index=len(pub.blocks),
                        topic_id=ref.topic_id,
                        topic_path=ref.resolved_path,
                        element_xpath=_build_xpath(path_stack),
                        element_tag="row",
                        text=text,
                        note_type=None,
                        auto_update=False,
                        skip_reason=(
                            "manual-review zone (related tasks / learn more)"
                            if manual_here
                            else (
                                "structural element <row> "
                                "— diff alignment is unreliable for "
                                "multi-child blocks; review manually"
                            )
                        ),
                        in_manual_zone=manual_here,
                        structural=True,
                        xref_hrefs=_collect_xref_hrefs(elem),
                        map_depth=ref.depth,
                    )
                )
            return  # don't recurse into row children

        # A <note> that contains structured children (<p>, <ul>, <ol>) is
        # rendered by the Help Center as ONE callout block of text — our
        # HTML parser also emits a single block for it. The leaf-only
        # rule would normally emit each <p> child separately, producing
        # a 3-vs-1 alignment mismatch and a chain of phantom REPLACE/
        # DELETE ops. Treat the <note> as the leaf for this purpose, mark
        # it structural so REPLACE is refused (we don't know which inner
        # <p> would get the new text anyway), and stop recursing.
        note_with_children = (
            tag == "note" and _has_block_descendants(elem)
        )
        if note_with_children:
            # Use the block-aware collapse so child <p>/<li> text is
            # separated by spaces (readable AND substring-friendly).
            text = _collapsed_block_text(elem).strip()
            if text:
                note_type = elem.attrib.get("type")
                note_othertype = elem.attrib.get("othertype")
                # REPLACE on a note is now broadly safe to apply: the
                # patch engine knows how to rebuild both shapes from
                # the article side — flat prose becomes <p> children,
                # and Note + bullets metadata becomes <ul><li> children.
                # The patch engine itself refuses at apply time if the
                # source has bullets but the article side didn't
                # provide bullet structure (would lose information),
                # so we don't need a blanket diff-level refusal here.
                if manual_here:
                    auto_update = False
                    structural = True
                    skip_reason = "manual-review zone (related tasks / learn more)"
                elif note_othertype == "feature":
                    # Feature notes carry product callouts with custom
                    # styling and an embedded link that isn't reliably
                    # round-trippable through the article HTML. Refuse
                    # and flag for the writer to add by hand.
                    auto_update = False
                    structural = True
                    skip_reason = (
                        "feature note (othertype=\"feature\") — please "
                        "add this feature note to the topic manually; "
                        "the tool couldn't apply it."
                    )
                else:
                    auto_update = True
                    structural = False
                    skip_reason = None
                pub.blocks.append(
                    Block(
                        block_index=len(pub.blocks),
                        topic_id=ref.topic_id,
                        topic_path=ref.resolved_path,
                        element_xpath=_build_xpath(path_stack),
                        element_tag=tag,
                        text=text,
                        note_type=note_type,
                        auto_update=auto_update,
                        skip_reason=skip_reason,
                        in_manual_zone=manual_here,
                        structural=structural,
                        xref_hrefs=_collect_xref_hrefs(elem),
                        map_depth=ref.depth,
                    )
                )
            return  # do not recurse into inner <p>/<ul> children

        # An <li> that mixes leading inline text with a child block element
        # (e.g. `<li>Brand kit to automatically … <note>You can edit…</note></li>`)
        # falls through to the "has block descendants" branch below, where
        # only the inner block is emitted — the leading text is silently
        # lost. Emit it explicitly so the diff can pair it against the
        # article's matching bullet, otherwise the article side re-INSERTs
        # the same text and the writer ends up with a duplicate row.
        if tag == "li" and _has_block_descendants(elem):
            leading = _leading_inline_text(elem).strip()
            if leading:
                pub.blocks.append(
                    Block(
                        block_index=len(pub.blocks),
                        topic_id=ref.topic_id,
                        topic_path=ref.resolved_path,
                        element_xpath=_build_xpath(path_stack),
                        element_tag="li",
                        text=leading,
                        note_type=None,
                        auto_update=not manual_here,
                        skip_reason=(
                            "manual-review zone (related tasks / learn more)"
                            if manual_here else None
                        ),
                        in_manual_zone=manual_here,
                        structural=False,
                        xref_hrefs=_collect_xref_hrefs(elem),
                        map_depth=ref.depth,
                    )
                )
            # Continue: the recursion below will emit the inner block
            # element (e.g. the <note>) as its own block at its own xpath.

        if tag in BLOCK_TAGS and not _has_block_descendants(elem):
            if tag == "row":
                text = _row_text(elem).strip()
            elif tag == "dlentry":
                # Render as "Term: Definition" so the source-side text
                # matches how the Help Center stylesheet renders a
                # <dlentry> (IM page 123: stylesheet auto-adds the
                # colon). Without this, the source becomes
                # "TermDefinition" — no separator — and the diff
                # emits a phantom REPLACE for every dlentry.
                text = _dlentry_collapsed_text(elem).strip()
            else:
                text = _element_text(elem).strip()

            # When a topic's <title> text matches the preceding map-level
            # navtitle (a tabbed section like Desktop / Mobile), emitting
            # both produces two source blocks for one article-side label
            # and the diff fires phantom DELETEs on the topic title. Skip
            # the topic title in that case — the navtitle already covers
            # this alignment point.
            if tag == "title" and pub.blocks:
                last_block = pub.blocks[-1]
                if (
                    last_block.element_tag == "navtitle"
                    and normalize_for_match(last_block.text)
                    == normalize_for_match(text)
                ):
                    text = ""

            if text:
                note_type = elem.attrib.get("type") if tag == "note" else None
                # Simple-shape <dlentry> (one dt + one dd, dd is leaf-only)
                # is now safe to auto-apply: the patch engine has a
                # dlentry-aware REPLACE handler that rewrites only the
                # <dd> text while leaving <dt> alone, and inline markup
                # in <dd> is preserved when each child's phrase still
                # appears in the new article wording. Complex dlentries
                # (multi-term, <dd> with nested ul/note/etc.) stay
                # structural so they surface for review.
                if tag == "dlentry" and _is_simple_dlentry_shape(elem):
                    structural = False
                else:
                    structural = tag in STRUCTURAL_BLOCK_TAGS
                auto_update = (not manual_here) and (not structural)
                if manual_here:
                    skip_reason = "manual-review zone (related tasks / learn more)"
                elif structural:
                    skip_reason = (
                        f"structural element <{tag}> "
                        "— diff alignment is unreliable for multi-child blocks; "
                        "review manually"
                    )
                else:
                    skip_reason = None
                pub.blocks.append(
                    Block(
                        block_index=len(pub.blocks),
                        topic_id=ref.topic_id,
                        topic_path=ref.resolved_path,
                        element_xpath=_build_xpath(path_stack),
                        element_tag=tag,
                        text=text,
                        note_type=note_type,
                        auto_update=auto_update,
                        skip_reason=skip_reason,
                        in_manual_zone=manual_here,
                        structural=structural,
                        xref_hrefs=_collect_xref_hrefs(elem),
                        map_depth=ref.depth,
                    )
                )

        # Recurse with positional indexing per tag.
        seen: dict[str, int] = {}
        for child in elem:
            child_tag = _local(child.tag)
            seen[child_tag] = seen.get(child_tag, 0) + 1
            child_step = f"{child_tag}[{seen[child_tag]}]"
            walk(child, path_stack + [child_step], manual_here, parent_tag=tag)

    root_tag = _local(root.tag)
    walk(root, [root_tag], in_manual_zone=False, parent_tag=None)


def _section_title(section_elem: ET.Element) -> Optional[str]:
    for child in section_elem:
        if _local(child.tag) == "title":
            return _element_text(child)
    return None


def build_topic_to_section(publication, article_blocks):
    """Return {topic_id → tabpanel section_id} for topics that correspond
    to a tab in the article.

    Lives here (not in patch_engine) so the diff engine can consult it
    too — without it the diff aligns by raw text only and cross-tab
    content can match against the wrong topic.

    Matching: the parser emits each tab button label as a
    kind="tab_label" block, in document order. The first unique
    section_id seen in subsequent blocks is the panel that label opens.
    A DITA topic whose <title> text matches a tab label (or whose
    preceding ditamap navtitle does) gets bound to that panel's id.
    Topics with no matching label remain unbound (pre-tab and post-tab
    content).
    """
    if publication is None or not article_blocks:
        return {}

    tab_labels = [
        b for b in article_blocks if getattr(b, "kind", None) == "tab_label"
    ]
    if not tab_labels:
        return {}

    seen = set()
    panel_ids: List[str] = []
    for b in article_blocks:
        sid = getattr(b, "section_id", None)
        if sid and sid not in seen:
            seen.add(sid)
            panel_ids.append(sid)
    if not panel_ids:
        return {}

    label_to_section = {}
    for tl, pid in zip(tab_labels, panel_ids):
        key = normalize_for_match(tl.text)
        if key:
            label_to_section[key] = pid

    topic_to_section = {}
    seen_topics = set()
    # Tab-binding scope tracking. Two ways a topic gets bound to a
    # tab section:
    #   (a) MapLabel match: a <topichead><navtitle>Desktop</navtitle>
    #       MapLabel appears in publication order; subsequent topics
    #       under it inherit panel-1.
    #   (b) Title match: a topic's own <title> text matches a tab
    #       label (e.g. Desktop.dita has <title>Desktop</title>).
    #
    # In either case we remember the map_depth of the bound topic.
    # When a later topic appears at a LOWER depth, we've exited the
    # tab topicgroup; subsequent topics are "post-tab" per IM
    # convention (Fix #14) and get bound to "__post_tab__" so the
    # article-side blocks the parser stamped with the same
    # section_id route here.
    POST_TAB_SECTION = "__post_tab__"
    current_navtitle_section = None
    current_navtitle_depth: Optional[int] = None
    tab_binding_depth: Optional[int] = None
    post_tab_active = False
    for blk in publication.blocks:
        if blk.topic_id == "<ditamap>":
            key = normalize_for_match(blk.text or "")
            if key and key in label_to_section:
                current_navtitle_section = label_to_section[key]
                current_navtitle_depth = blk.map_depth
                tab_binding_depth = blk.map_depth
                post_tab_active = False
            else:
                current_navtitle_section = None
                current_navtitle_depth = None
            continue
        # Depth-based scope exit. When the publication drops below
        # the depth at which we last bound a tab topic, we've left
        # the topicgroup. Two cases:
        #   - navtitle-bound: blk.map_depth ≤ navtitle's depth.
        #   - title-bound: blk.map_depth < the bound topic's depth.
        if (
            current_navtitle_section is not None
            and current_navtitle_depth is not None
            and blk.map_depth <= current_navtitle_depth
        ):
            current_navtitle_section = None
            current_navtitle_depth = None
            post_tab_active = True
        if (
            tab_binding_depth is not None
            and blk.map_depth < tab_binding_depth
        ):
            post_tab_active = True
            tab_binding_depth = None
        if current_navtitle_section is not None and blk.topic_id not in topic_to_section:
            topic_to_section[blk.topic_id] = current_navtitle_section
        elif (
            post_tab_active
            and blk.topic_id not in topic_to_section
        ):
            topic_to_section[blk.topic_id] = POST_TAB_SECTION
        if blk.topic_id in seen_topics:
            continue
        if blk.element_xpath.endswith("/title[1]"):
            key = normalize_for_match(blk.text or "")
            # Topic <title> matching a tab label overrides any
            # inherited binding — gives the topic its own explicit
            # tab even when the navtitle differs.
            if key in label_to_section:
                topic_to_section[blk.topic_id] = label_to_section[key]
                tab_binding_depth = blk.map_depth
            seen_topics.add(blk.topic_id)
    return topic_to_section


_WS_RE = re.compile(r"\s+")

# Typography characters that are visually/semantically identical between
# the legacy article rendering and the source DITA but differ at the byte
# level. We fold both sides through this map before comparing so that
# typography-only differences don't surface as REPLACE ops.
#
# Trade: the patch engine receives the article's text verbatim, so when a
# REPLACE *does* fire (because something semantically changed), the DITA
# will adopt whatever typography the article uses. We only suppress the
# REPLACE when both sides reduce to the same normalized string.
_TYPOGRAPHY_MAP = str.maketrans({
    "‘": "'",   # left single quote  '
    "’": "'",   # right single quote '
    "“": '"',   # left double quote  "
    "”": '"',   # right double quote "
    "–": "-",   # en-dash  –
    "—": "-",   # em-dash  —
    "…": "...", # ellipsis …
    "\xa0":   " ",   # non-breaking space
    # Invisible characters: BOM and zero-widths. Help Center editors paste
    # content that sometimes contains these; without stripping, they create
    # phantom REPLACE ops (the only diff is an invisible character) and end
    # up written into the patched .dita.
    "﻿": None,  # BOM / zero-width no-break space
    "​": None,  # zero-width space
    "‌": None,  # zero-width non-joiner
    "‍": None,  # zero-width joiner
})


_PUNCT_RIGHT_RE = re.compile(r"\s+([)\],.;:!?])")
_PUNCT_LEFT_RE = re.compile(r"([(\[])\s+")
# Strip whitespace BEFORE an opening paren/bracket too. Source DITA
# authors sometimes write `<uicontrol>X</uicontrol>(Y)` with no space
# while the article HTML renders `X (Y)` with a space. Without this,
# such rows mismatch on the diff, SequenceMatcher can't pair them as
# equal, and the article side gets re-inserted as a duplicate row
# alongside the unchanged source row.
_PUNCT_BEFORE_OPEN_RE = re.compile(r"\s+([(\[])")

# Strip the "Optional:" prefix the Help Center stylesheet adds to
# steps marked `<cmd importance="optional">`. The DITA <cmd> text
# itself never contains "Optional:" — it's a render-time decoration.
# Without stripping, source step `Preview how your ad...` won't match
# the article's `Optional: Preview how your ad...` and SequenceMatcher
# emits a phantom REPLACE / drops the source block.
_OPTIONAL_PREFIX_RE = re.compile(r"^Optional\s*:\s*", re.IGNORECASE)

# Strip Help-Center-rendered note labels ("Note: ...", "Tip: ...",
# "Warning: ..."). Source DITA <note> elements store body text only;
# the label is added at render time by the stylesheet. Without this,
# a source <note>You can edit any...</note> won't match the article's
# "Note: You can edit any..." and the article version gets re-INSERTed
# as a duplicate. The required separator (colon/dash) prevents
# false-positives on regular sentences like "Note that you must...".
_NOTE_LABEL_PREFIX_RE = re.compile(
    r"^(?:note|tip|warning|caution|important|remember|attention)"
    r"\s*[:\-–—]\s+",
    re.IGNORECASE,
)

# Treat the common "term — description" separators (colon, hyphen,
# en-dash, em-dash, with surrounding spaces) as equivalent. Source DITA
# `<dlentry>` renders as "Term: Description" via the stylesheet, while
# the Help Center article often displays the same content as
# "Term – Description" or "Term - Description". Without this
# normalization, every dlentry whose only difference is the separator
# style reads as a phantom REPLACE and lands in "Needs review."
_DL_SEPARATOR_RE = re.compile(r"\s*[:\-–—]\s+")

# Trailing sentence-ending punctuation that the Help Center adds
# automatically but source DITA often omits. Without stripping these
# in the comparison key, "Sales Navigator helps you..." (source) and
# "Sales Navigator helps you...." (article) produce different LCS
# keys, can't EQUAL-pair, and end up as a "leave the source AND
# insert a duplicate" pattern. Beta surfaced this on the Premium
# subscriptions article where every bullet had a trailing period in
# the live content but not in the source DITA.
_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?]+$")


def normalize_for_match(text: str) -> str:
    """Whitespace- and typography-insensitive comparison key.

    Used only as the diff engine's equality test. The Block.text field
    keeps the original characters so the patch engine can show the user
    exactly what was in the source.

    Also strips whitespace adjacent to common punctuation, because
    source DITA is often hand-formatted with a space between a closing
    tag and the next punctuation:
        <uicontrol>Live</uicontrol> ).
    flattens to "Live )." while the article's rendering is "Live)."
    — semantically identical but byte-different. Without this rule
    every such row reads as a REPLACE.
    """
    text = text.translate(_TYPOGRAPHY_MAP)
    text = _WS_RE.sub(" ", text).strip()
    text = _PUNCT_RIGHT_RE.sub(r"\1", text)
    text = _PUNCT_LEFT_RE.sub(r"\1", text)
    text = _PUNCT_BEFORE_OPEN_RE.sub(r"\1", text)
    text = _OPTIONAL_PREFIX_RE.sub("", text)
    text = _NOTE_LABEL_PREFIX_RE.sub("", text)
    text = _DL_SEPARATOR_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    # Strip trailing sentence-ending punctuation. The source DITA
    # convention is no terminal period on list items / table cells,
    # while the rendered article almost always has one. They're
    # semantically identical — treat them as equal for diff matching.
    text = _TRAILING_PUNCT_RE.sub("", text).strip()
    return text
