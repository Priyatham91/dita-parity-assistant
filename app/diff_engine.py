"""Sequential, order-preserving diff between reconstructed publication blocks
and an updated source article.

Design constraints (from the migration spec):
  - Preserve publication order. The .ditamap is the source of truth.
  - No fuzzy global matching. We use exact equality on normalized text,
    aligned by Myers-style LCS (difflib.SequenceMatcher with autojunk=False).
  - No cross-topic line matching. Inputs are flat sequences; alignment is
    purely positional within those sequences.
  - Deterministic. Same inputs always produce the same ops in the same order.

The output is a list of per-block DiffOp records that the patch engine can
consume one-to-one: each REPLACE/DELETE names exactly one owning element;
each INSERT names exactly one anchor element to attach after.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from typing import Dict, List, Optional, Sequence

from app.publication_reconstructor import (
    Block,
    Publication,
    build_topic_to_section,
    normalize_for_match,
)


# IM_note05: <note> content must not begin with these marker phrases. They
# are publish-time decorations rendered around the note body. Legacy article
# exports include them inline ("Important to know: ...") even though the
# DITA <note> body itself is prefix-free. Strip the prefixes from the
# article side at split time so the diff aligns notes correctly and any
# REPLACE we apply writes Schematron-compliant body text.
#
# We deliberately exclude the bare "Note" prefix even though IM_note05 lists
# it: regular paragraphs ("Note that you must...") would match it as a
# false positive. The other three phrases are product-specific markers
# unlikely to appear at the start of a non-note paragraph.
# Match the marker followed by a separator and body content. The trailing
# \s+ requires actual content after the marker.
_NOTE_PREFIX_RE = re.compile(
    # Help-Center-specific note labels can render without a separator
    # ("Important to know\n body"). The bare "Note" / "Tip" / "Warning"
    # labels ONLY get stripped when followed by an explicit separator —
    # `:`, `-`, `–`, `—` — so a normal sentence starting with "Note
    # that you must..." (no separator) is not mis-stripped.
    r"^(?:"
        r"(?:important to know|here\W*s a tip|who can use this feature)\s*[:\-–—]?\s+"
        r"|"
        r"(?:note|tip|warning|caution|important|remember|attention)\s*[:\-–—]\s+"
    r")",
    re.IGNORECASE,
)

# Match the marker as the WHOLE line (optionally followed by punctuation).
# These are publish-time decorations with no body content; we drop them so
# they don't get aligned as if they were real article blocks.
_NOTE_MARKER_ONLY_RE = re.compile(
    r"^(?:important to know|here\W*s a tip|who can use this feature)"
    r"\s*[:\-–—?.!]*\s*$",
    re.IGNORECASE,
)


# Words shorter than this are too common (articles, prepositions, etc.)
# to count as evidence that two blocks describe the same thing.
_OVERLAP_MIN_WORD_LEN = 4

# Jaccard threshold below which a REPLACE pair is treated as a
# misalignment rather than a rewrite. Real rewrites preserve key nouns
# (>= 0.20 in practice); unrelated-block pairings score near zero.
_OVERLAP_MIN_JACCARD = 0.15

# Stricter threshold for the DELETE safety net. We want "is this
# exact content elsewhere in the article?" — not "do these share a
# domain?" Two unrelated paragraphs in one article can share 20-30%
# of their content words just because they're about the same product.
_DELETE_SAFETY_JACCARD = 0.70

# Below this normalized length, substring fallback doesn't fire — a
# very short candidate (e.g. "Note:") would otherwise match many
# longer paragraphs that happen to start with it.
_DELETE_SAFETY_MIN_SUBSTRING_LEN = 40

# Inside an "ambiguous" replace segment (source has more blocks than
# article), a paired REPLACE whose two sides share at least this many
# content words by Jaccard is still safe to auto-apply: the high
# overlap means it's a real rewrite, not a misalignment artifact.
# Without this escape hatch, a 1:1 rewrite buried inside a wider
# unmatched range gets refused alongside the genuinely-ambiguous pairs.
_AMBIGUOUS_REPLACE_OK_JACCARD = 0.55

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _content_words(text: str) -> set:
    """Lowercased content words used for the overlap test."""
    return {
        w.lower()
        for w in _WORD_RE.findall(text)
        if len(w) >= _OVERLAP_MIN_WORD_LEN
    }


def _looks_like_same_block(a: str, b: str) -> bool:
    """True if `a` and `b` share enough content words to be the same block."""
    words_a = _content_words(a)
    words_b = _content_words(b)
    if not words_a or not words_b:
        # Too short to extract content words — fall back to default
        # SequenceMatcher pairing (don't reject).
        return True
    intersection = words_a & words_b
    union = words_a | words_b
    return (len(intersection) / len(union)) >= _OVERLAP_MIN_JACCARD


def _is_high_overlap_pair(a: str, b: str) -> bool:
    """True when two blocks share enough content to be a confident 1:1 rewrite.

    Used to rescue a clear REPLACE pair from an "ambiguous" segment-wide
    refusal. The threshold is much higher than `_looks_like_same_block`
    because the cost of a false positive here is auto-applying the wrong
    rewrite, not just demoting one pair to delete+insert.

    Three signals each grant confidence:
      (a) High Jaccard (≥ 0.55) — both texts share most content words.
      (b) Containment — one side's content words are a subset of the
          other's. Common for terse rewrites ("Click the Save button
          in the upper-right corner." → "Click Save."): the article
          shortened the source, every article word still appears in
          the source. Beta surfaced this on Article 4's Desktop step.
      (c) Either side too short to fingerprint (e.g. < 2 content words).
    """
    words_a = _content_words(a)
    words_b = _content_words(b)
    if not words_a or not words_b:
        return False
    # Containment: one side is a (non-empty) subset of the other AND
    # the smaller side has at least 2 content words. The lower bound
    # prevents single-word coincidences from passing (e.g. both texts
    # share the word "save" but are otherwise unrelated).
    smaller, larger = (
        (words_a, words_b) if len(words_a) <= len(words_b)
        else (words_b, words_a)
    )
    if len(smaller) >= 2 and smaller.issubset(larger):
        return True
    intersection = words_a & words_b
    union = words_a | words_b
    return (len(intersection) / len(union)) >= _AMBIGUOUS_REPLACE_OK_JACCARD


def _strip_note_prefix(line: str) -> str:
    """Return the line with any leading IM_note05 marker removed.

    - "Important to know: body"  -> "body"
    - "Important to know body"   -> "body"
    - "Important to know?"       -> ""   (marker-only, drop)
    - "Important to know"        -> ""   (marker-only, drop)
    - "Important things to do"   -> unchanged (no marker match)
    """
    if _NOTE_MARKER_ONLY_RE.match(line):
        return ""
    return _NOTE_PREFIX_RE.sub("", line, count=1)


class OpKind(str, Enum):
    EQUAL = "equal"
    REPLACE = "replace"
    DELETE = "delete"
    INSERT = "insert"


@dataclass(frozen=True)
class DiffOp:
    kind: OpKind

    # Source side (reconstructed publication). None for INSERT.
    source_block: Optional[Block]

    # Updated side (source article line/block). None for DELETE/EQUAL-with-no-change.
    updated_text: Optional[str]
    updated_index: Optional[int]    # position in the updated article block list

    # For INSERT: the source block this insert is anchored AFTER.
    # None means "insert at very beginning of the publication".
    anchor_block: Optional[Block] = None

    # Forwarded from the source block (or anchor block for inserts).
    # The patch engine uses this to decide whether to skip the op.
    safe_to_apply: bool = True

    # Optional reason set by the diff engine itself (e.g. alignment
    # ambiguity when SequenceMatcher pairs unequal-length ranges). When
    # set, the patch engine prefers it over the source block's reason.
    op_skip_reason: Optional[str] = None

    def __repr__(self) -> str:  # compact for logs
        src = f"src#{self.source_block.block_index}" if self.source_block else "-"
        upd = f"upd#{self.updated_index}" if self.updated_index is not None else "-"
        return f"DiffOp({self.kind.value} {src} {upd} safe={self.safe_to_apply})"


def split_updated_article(text: str) -> List[str]:
    """Block-segment the updated article.

    Legacy Help Center exports place one logical block per line: a
    paragraph, a list item, a heading, a definition entry. Blank lines
    are purely visual and do not merge content. We therefore treat each
    non-blank line as its own block. Internal whitespace is preserved;
    leading/trailing whitespace is stripped. Note marker prefixes
    ("Important to know:", "Here's a tip:", "Who can use this feature:")
    are stripped per IM_note05 so the body matches DITA <note> content.
    """
    blocks: List[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        normalized = _strip_note_prefix(stripped)
        if normalized:
            blocks.append(normalized)
    return blocks


def diff(
    publication: Publication,
    updated_blocks: Sequence[str],
    article_blocks: Optional[Sequence] = None,
) -> List[DiffOp]:
    """Run a sequential block diff and emit per-block ops.

    When `article_blocks` is provided (and contains `section_id` per
    block plus enough info to build a topic→section map), the matcher
    is made section-aware: text that's identical across different
    tabs becomes non-equal at the SequenceMatcher layer, so the
    article's panel-1 ("Desktop") content can't be matched against a
    source block that belongs to the panel-2 ("Mobile") topic.

    Without this, identical wording duplicated across both tabs
    (very common in Help Center articles) gets paired arbitrarily —
    the second tab's text "consumes" the first tab's article block
    and the first tab loses an INSERT it should have received.
    """
    source_blocks = publication.blocks
    topic_to_section = build_topic_to_section(publication, article_blocks)

    # Compute the "effective section" for each source block.
    # For real topic blocks this is topic_to_section[topic_id]. For
    # synthetic <ditamap> navtitle blocks (no topic of their own) it's
    # the section of the *next* topic-bearing block in publication
    # order — that's the topic the navtitle labels. Without this the
    # article-side tab_label (which sits inside its panel and gets
    # section=panel-N) doesn't match the source-side navtitle (which
    # would otherwise have section=""), and SequenceMatcher emits a
    # spurious REPLACE on the navtitle.
    effective_section: List[str] = [""] * len(source_blocks)
    next_topic_section = ""
    for i in range(len(source_blocks) - 1, -1, -1):
        block = source_blocks[i]
        if block.topic_id == "<ditamap>":
            effective_section[i] = next_topic_section
        else:
            section = topic_to_section.get(block.topic_id, "")
            effective_section[i] = section
            next_topic_section = section

    def _src_key(idx: int) -> str:
        block = source_blocks[idx]
        return normalize_for_match(block.text) + f"\x00§{effective_section[idx]}\x00"

    def _upd_key(idx: int) -> str:
        text = updated_blocks[idx]
        section = ""
        if article_blocks is not None and idx < len(article_blocks):
            section = getattr(article_blocks[idx], "section_id", None) or ""
        return normalize_for_match(text) + f"\x00§{section}\x00"

    if topic_to_section:
        a = [_src_key(i) for i in range(len(source_blocks))]
        b = [_upd_key(j) for j in range(len(updated_blocks))]
    else:
        a = [normalize_for_match(b.text) for b in source_blocks]
        b = [normalize_for_match(t) for t in updated_blocks]

    matcher = SequenceMatcher(a=a, b=b, autojunk=False)
    ops: List[DiffOp] = []

    # For INSERT anchoring: track the most recent source block emitted
    # (either via EQUAL or REPLACE). At the start, this is None.
    last_source: Optional[Block] = None

    # Article-side content-word sets + normalized texts, used by the
    # DELETE safety net. Before auto-DELETEing a source block, scan
    # every article block: if any has near-identical content (the
    # source is still in the article — LCS just aligned it elsewhere),
    # don't delete. The check is INTENTIONALLY stricter than the
    # demotion threshold (15%): two unrelated paragraphs in the same
    # article can share 20-30% of their words just because they cover
    # the same domain (premium, subscribers, email, redeem…). We don't
    # want that to suppress a legitimate DELETE. The DELETE-safety
    # contract is "this exact content is still in the article" — so
    # require either very high Jaccard (≥ 0.7) or a substring match
    # after normalization.
    _all_article_word_sets = [_content_words(t) for t in updated_blocks]
    _all_article_norms = [normalize_for_match(t) for t in updated_blocks]

    # Track which article-side indices the LCS has already consumed
    # (via EQUAL or via the leading 1:1 pairs of a REPLACE segment).
    # The DELETE safety net must skip these indices when scanning for
    # "is this source content still in the article?" — otherwise a
    # source block whose duplicate sibling was already paired with an
    # article block would falsely look "still present" and escape
    # deletion. This was the bug behind the duplicate "(PDF)"/"(Word
    # Doc)" labels in the U.S. members ul not being deleted from the
    # Non-U.S. members section.
    _consumed_article_indices: set = set()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                _consumed_article_indices.add(j1 + offset)
        elif tag == "replace":
            paired = min(i2 - i1, j2 - j1)
            for offset in range(paired):
                _consumed_article_indices.add(j1 + offset)

    # Precompute: for each effective_section, the sorted list of
    # source block indices in that section. Used to pick a section-
    # matching INSERT anchor when the LCS's `last_source` lives in
    # the wrong section — without this, an INSERT for article content
    # in section X anchored at a source in section Y triggers the
    # cross-section refusal at apply time and the writer never sees
    # it land in topic X. Beta surfaced this on Article 4's post-tab
    # paragraph: it should route to post-tab_content.dita but was
    # anchored at Mobile.dita's last step.
    import bisect as _bisect
    _section_to_src_indices: Dict[str, List[int]] = {}
    if effective_section:
        for _i, _sec in enumerate(effective_section):
            _section_to_src_indices.setdefault(_sec, []).append(_i)

    def _section_matching_anchor(article_section: str, current_src_idx: int):
        """Return the source block whose section matches `article_section`
        and is closest in publication order to `current_src_idx`.
        Falls back to None when no such source exists."""
        if not article_section:
            return None
        indices = _section_to_src_indices.get(article_section, [])
        if not indices:
            return None
        pos = _bisect.bisect_left(indices, current_src_idx)
        # Prefer a candidate before current_src_idx (the natural
        # "anchor before insert" pattern).
        if pos > 0:
            return source_blocks[indices[pos - 1]]
        # Otherwise take the first candidate AFTER current — content
        # routes forward to its proper topic.
        return source_blocks[indices[pos]]

    def _source_block_appears_in_article(
        src_block: Block, src_idx: int,
    ) -> bool:
        src_words = _content_words(src_block.text)
        if not src_words:
            return False
        src_norm = normalize_for_match(src_block.text)
        src_section = effective_section[src_idx] if effective_section else ""
        for j, art_words in enumerate(_all_article_word_sets):
            if j in _consumed_article_indices:
                # This article block is already paired with some other
                # source block by LCS — it does NOT count as evidence
                # that THIS source block is still present.
                continue
            # Section-aware safety: when the source block lives in a
            # specific section (e.g. a tab panel) and the candidate
            # article block lives in a different section, the article
            # doesn't actually have this source's content in the
            # source's own section. Allow the DELETE — the writer
            # will see the matching content elsewhere as an INSERT
            # (cross-section refusal will surface that separately).
            # Beta surfaced this on Article 4: a Mobile-tab "Post an
            # update…" step wasn't being deleted because the article
            # showed similar text in the post-tab paragraph (no
            # section). That blocked the Mobile DELETE even though
            # the content had moved.
            if article_blocks is not None and topic_to_section:
                art_section = (
                    getattr(article_blocks[j], "section_id", None) or ""
                )
                if src_section != art_section:
                    continue
            if not art_words:
                continue
            inter = src_words & art_words
            union = src_words | art_words
            jaccard = (len(inter) / len(union)) if union else 0.0
            if jaccard >= _DELETE_SAFETY_JACCARD:
                return True
            # Substring fallback: if either side wholly contains the
            # other (after normalize), they're the same content with
            # one side having a prefix/suffix. Skip very short
            # candidates so a tiny "Note:" line doesn't match a long
            # paragraph that happens to start with it.
            art_norm = _all_article_norms[j]
            if len(src_norm) >= _DELETE_SAFETY_MIN_SUBSTRING_LEN and (
                src_norm in art_norm or art_norm in src_norm
            ):
                return True
        return False

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                src = source_blocks[i1 + offset]
                ops.append(
                    DiffOp(
                        kind=OpKind.EQUAL,
                        source_block=src,
                        updated_text=updated_blocks[j1 + offset],
                        updated_index=j1 + offset,
                        safe_to_apply=src.auto_update,
                    )
                )
                last_source = src

        elif tag == "delete":
            for offset in range(i2 - i1):
                src = source_blocks[i1 + offset]
                # Safety net: if the source block's content appears
                # ANYWHERE in the article (LCS may have aligned it
                # elsewhere — reordered sections, mass content shuffle),
                # don't emit a DELETE. The block is still in the
                # article, just at a different position; the article-
                # side instance will fire as an INSERT separately and
                # the writer ends up with both copies. That's preferable
                # to silently destroying content the writer didn't
                # actually remove.
                if _source_block_appears_in_article(src, i1 + offset):
                    last_source = src
                    continue
                ops.append(
                    DiffOp(
                        kind=OpKind.DELETE,
                        source_block=src,
                        updated_text=None,
                        updated_index=None,
                        safe_to_apply=src.auto_update,
                    )
                )
                last_source = src

        elif tag == "insert":
            anchor = last_source  # may be None for inserts before all source
            # Inserting AFTER a structural sibling (a row, a dlentry) does
            # not modify that sibling — it just adds a new one beside it.
            # Only a manual-review-zone anchor truly blocks the insert.
            anchor_safe = (
                (not anchor.in_manual_zone) if anchor is not None else True
            )
            for offset in range(j2 - j1):
                ops.append(
                    DiffOp(
                        kind=OpKind.INSERT,
                        source_block=None,
                        updated_text=updated_blocks[j1 + offset],
                        updated_index=j1 + offset,
                        anchor_block=anchor,
                        safe_to_apply=anchor_safe,
                    )
                )

        elif tag == "replace":
            src_len = i2 - i1
            upd_len = j2 - j1
            paired = min(src_len, upd_len)

            # Ambiguity is asymmetric. If src_len > upd_len, the article
            # collapsed multiple DITA blocks into fewer — we don't know
            # which DITA block any given new text "belongs" to, so the
            # 1:1 pairing is a guess. SKIP. If upd_len >= src_len, the
            # article expanded N blocks into N+more — the first N pairings
            # are still clean 1:1 maps and the remainder are clean
            # additions (handled as INSERTs below). Don't mark ambiguous
            # in that case; applying both produces the right shape.
            ambiguous = src_len > upd_len
            ambig_reason = (
                f"alignment ambiguity: {src_len} source block(s) vs "
                f"{upd_len} article block(s) in this region — review manually"
                if ambiguous else None
            )

            # 1:1 REPLACE for the paired prefix.
            #
            # SequenceMatcher's LCS will pair the leftover-unmatched
            # source and article blocks 1:1, even when the two have
            # essentially nothing in common. The most frequent cause
            # is the article inserting new rows/paragraphs in the
            # middle of an otherwise-unchanged sequence: the new
            # blocks consume nearby source blocks as REPLACE partners,
            # making it look like those source blocks were rewritten
            # when they were not. Detect this by content-word overlap
            # and demote no-overlap pairs to a pure INSERT — the
            # source block is left untouched (it likely still appears
            # later in the article and would be paired correctly there
            # if LCS had picked a different alignment).
            for offset in range(paired):
                src = source_blocks[i1 + offset]
                upd_text = updated_blocks[j1 + offset]

                # Cross-section demotion: when a replace pair spans
                # different tab sections (source in panel-1, article
                # block in no tab), the pair is fundamentally wrong
                # even if the text overlaps. The article-side content
                # has moved to a different region; the source needs
                # to DELETE (its region no longer has this content)
                # and the article block needs to INSERT (its new
                # region's topic gets the new content). Beta surfaced
                # this on Article 4: a Mobile-tab "Post an update…"
                # step was paired (by text overlap) with the post-tab
                # paragraph and the cross-section refusal made it stay
                # forever in Mobile.
                src_section = (
                    effective_section[i1 + offset]
                    if effective_section else ""
                )
                art_section = ""
                if article_blocks is not None and (j1 + offset) < len(article_blocks):
                    art_section = (
                        getattr(article_blocks[j1 + offset], "section_id", None)
                        or ""
                    )
                cross_section = (
                    bool(topic_to_section)
                    and src_section != art_section
                )

                if cross_section or not _looks_like_same_block(src.text, upd_text):
                    # Demote: emit DELETE for the source block (it has no
                    # match in the article) and INSERT for the article
                    # block (it has no match in the source). Without the
                    # DELETE the writer ends up with both the old AND
                    # the new content in the patched file — defeating the
                    # purpose of an automated assistant.
                    #
                    # Safety net: before emitting the DELETE, scan EVERY
                    # article block for a content-word match. LCS picks
                    # one alignment, but the same text can legitimately
                    # appear at a different position elsewhere in the
                    # article (e.g. a step that got reordered). If we
                    # find a match anywhere, the source block is still
                    # present in the article — don't delete it. Fall
                    # back to the old behavior (skip source, INSERT the
                    # article-side block separately).
                    #
                    # Section-aware anchor: if the article block has a
                    # specific section_id, prefer a source block in that
                    # same section as the INSERT anchor (so the patch
                    # engine routes the new content into the right .dita
                    # topic). Falls back to last_source when no match.
                    art_section = art_section  # already computed above
                    section_anchor = _section_matching_anchor(
                        art_section, i1 + offset,
                    )
                    if _source_block_appears_in_article(src, i1 + offset):
                        # Source block lives somewhere else in the
                        # article. Just emit the INSERT for the new
                        # article block; leave the source alone.
                        anchor = section_anchor or last_source
                        anchor_safe = (
                            (not anchor.in_manual_zone)
                            if anchor is not None else True
                        )
                        ops.append(
                            DiffOp(
                                kind=OpKind.INSERT,
                                source_block=None,
                                updated_text=upd_text,
                                updated_index=j1 + offset,
                                anchor_block=anchor,
                                safe_to_apply=anchor_safe,
                            )
                        )
                        continue
                    ops.append(
                        DiffOp(
                            kind=OpKind.DELETE,
                            source_block=src,
                            updated_text=None,
                            updated_index=None,
                            safe_to_apply=src.auto_update,
                            op_skip_reason=None,
                        )
                    )
                    anchor = section_anchor or last_source
                    anchor_safe = (
                        (not anchor.in_manual_zone)
                        if anchor is not None else True
                    )
                    ops.append(
                        DiffOp(
                            kind=OpKind.INSERT,
                            source_block=None,
                            updated_text=upd_text,
                            updated_index=j1 + offset,
                            anchor_block=anchor,
                            safe_to_apply=anchor_safe,
                        )
                    )
                    # Don't update last_source — the to-be-deleted source
                    # block isn't a confirmed anchor for downstream INSERTs.
                    continue

                # Escape hatch: an ambiguous segment can contain a 1:1
                # rewrite with very high content overlap (think a single
                # sentence reworded, surrounded by unrelated insertions).
                # Treat such pairs as safe — the segment-wide ambiguity
                # doesn't apply when this particular pair is unmistakable.
                pair_is_high_overlap = (
                    ambiguous
                    and _is_high_overlap_pair(src.text, upd_text)
                )
                pair_ambiguous = ambiguous and not pair_is_high_overlap
                pair_reason = (
                    ambig_reason
                    if pair_ambiguous else None
                )

                ops.append(
                    DiffOp(
                        kind=OpKind.REPLACE,
                        source_block=src,
                        updated_text=upd_text,
                        updated_index=j1 + offset,
                        safe_to_apply=src.auto_update and not pair_ambiguous,
                        op_skip_reason=pair_reason,
                    )
                )
                last_source = src

            # Leftover: more source than updated -> DELETEs.
            if src_len > upd_len:
                for offset in range(paired, src_len):
                    src = source_blocks[i1 + offset]
                    # A leftover DELETE inside an ambiguous replace
                    # segment has no article-side pair at all — it's
                    # not ambiguous, the source block clearly has no
                    # match in the article. Use the safety net (which
                    # is now section-aware) to confirm: if the source's
                    # content doesn't appear anywhere in the article
                    # within the same section, the DELETE is safe to
                    # auto-apply even though the surrounding segment
                    # produced ambiguous REPLACE pairs.
                    src_idx = i1 + offset
                    if not _source_block_appears_in_article(src, src_idx):
                        ops.append(
                            DiffOp(
                                kind=OpKind.DELETE,
                                source_block=src,
                                updated_text=None,
                                updated_index=None,
                                safe_to_apply=src.auto_update,
                                op_skip_reason=None,
                            )
                        )
                    else:
                        ops.append(
                            DiffOp(
                                kind=OpKind.DELETE,
                                source_block=src,
                                updated_text=None,
                                updated_index=None,
                                safe_to_apply=src.auto_update and not ambiguous,
                                op_skip_reason=ambig_reason,
                            )
                        )
                    last_source = src

            # Leftover: more updated than source -> INSERTs after last paired source.
            elif upd_len > src_len:
                anchor = last_source
                anchor_safe = (
                    (not anchor.in_manual_zone) if anchor is not None else True
                )
                for offset in range(paired, upd_len):
                    ops.append(
                        DiffOp(
                            kind=OpKind.INSERT,
                            source_block=None,
                            updated_text=updated_blocks[j1 + offset],
                            updated_index=j1 + offset,
                            anchor_block=anchor,
                            safe_to_apply=anchor_safe,
                        )
                    )

    return _dedupe_adjacent_inserts(_coalesce_delete_insert_pairs(ops))


def _coalesce_delete_insert_pairs(ops: List[DiffOp]) -> List[DiffOp]:
    """Coalesce a DELETE on block X plus an INSERT anchored at X into a
    single REPLACE on X.

    Beta surfaced this pattern on Article 4's post-tab content. The
    cross-section demotion produced:
       - DELETE post-tab_content.dita's old <note>
       - INSERT article's new "After changing…" paragraph, anchored at
         that same <note>
    At apply time, DELETEs run before INSERTs in reverse document
    order — so the DELETE removed the anchor element and the INSERT
    failed to resolve its xpath.

    Merging the pair into a REPLACE keeps the new content while
    swapping it into the same DITA position. Apply time then doesn't
    need to fall back through a missing anchor.
    """
    # Build an index: anchor_block id() -> list of INSERT op indices
    insert_idx_by_anchor: Dict[int, List[int]] = {}
    for i, op in enumerate(ops):
        if (
            op.kind == OpKind.INSERT
            and op.anchor_block is not None
        ):
            insert_idx_by_anchor.setdefault(
                id(op.anchor_block), []
            ).append(i)
    # For each DELETE whose source_block matches an INSERT anchor,
    # convert the DELETE into a REPLACE using the INSERT's text and
    # mark the INSERT for removal.
    to_drop: set = set()
    new_ops: List[DiffOp] = list(ops)
    for i, op in enumerate(ops):
        if op.kind != OpKind.DELETE or op.source_block is None:
            continue
        candidates = insert_idx_by_anchor.get(id(op.source_block), [])
        if not candidates:
            continue
        # Use the FIRST INSERT in op order (preserves article-side
        # forward order when multiple INSERTs target the same anchor).
        # Subsequent INSERTs keep their anchor — they'll attach as
        # siblings of the new REPLACEd element.
        insert_idx = candidates[0]
        ins_op = ops[insert_idx]
        # Build a REPLACE op that swaps the deleted source for the
        # inserted text. Safety + reason inherit from the DELETE's
        # safety (it's a real source-block change).
        new_ops[i] = DiffOp(
            kind=OpKind.REPLACE,
            source_block=op.source_block,
            updated_text=ins_op.updated_text,
            updated_index=ins_op.updated_index,
            safe_to_apply=op.safe_to_apply and ins_op.safe_to_apply,
            op_skip_reason=op.op_skip_reason or ins_op.op_skip_reason,
        )
        to_drop.add(insert_idx)
    return [op for k, op in enumerate(new_ops) if k not in to_drop]


def _dedupe_adjacent_inserts(ops: List[DiffOp]) -> List[DiffOp]:
    """Drop INSERTs that duplicate an adjacent op's text.

    Two failure modes to catch:

    1. Adjacent INSERT-INSERT with the same normalized text: the
       Help Center article literally has the same paragraph twice in
       a row (a copy-paste mistake). Writing both produces a duplicate
       row in the patched DITA.

    2. INSERT next to an EQUAL whose article-side text normalizes to
       the same value: the article repeats a line that ALSO matches a
       source block. LCS pairs one copy as EQUAL and the other as an
       INSERT, even though the second copy is just the article-side
       duplicate. The source block already covers it.

    In both cases, keep the first occurrence (or the EQUAL) and drop
    the redundant INSERT.
    """
    def _norm(text: Optional[str]) -> str:
        return normalize_for_match(text) if text else ""

    # First pass: collect normalized article-side text of every EQUAL,
    # so an INSERT with the same key can be detected even when separated
    # by a few ops on one side.
    keep = [True] * len(ops)
    last_emitted_key: Optional[str] = None

    for i, op in enumerate(ops):
        if op.kind == OpKind.INSERT and op.updated_text:
            key = _norm(op.updated_text)
            # Adjacent (previous kept op) is INSERT or EQUAL with same key?
            if key == last_emitted_key:
                keep[i] = False
                continue
            # Look ahead: does the NEXT op (skipping other dropped INSERTs)
            # have the same normalized text as this INSERT? If the next is
            # an EQUAL with that text, this INSERT is the writer-mistake
            # duplicate — drop it and let the EQUAL anchor the content.
            j = i + 1
            while j < len(ops) and not keep[j]:
                j += 1
            if j < len(ops):
                next_op = ops[j]
                next_key = _norm(next_op.updated_text) if next_op.updated_text else ""
                if next_key == key and next_op.kind in (OpKind.EQUAL, OpKind.INSERT):
                    keep[i] = False
                    continue
            last_emitted_key = key
        elif op.kind == OpKind.EQUAL and op.updated_text:
            last_emitted_key = _norm(op.updated_text)
        else:
            last_emitted_key = None

    return [op for op, k in zip(ops, keep) if k]


def summarize(ops: List[DiffOp]) -> dict:
    """Counts by op kind, useful for quick reports and tests."""
    summary = {kind.value: 0 for kind in OpKind}
    summary["unsafe"] = 0
    for op in ops:
        summary[op.kind.value] += 1
        if not op.safe_to_apply and op.kind != OpKind.EQUAL:
            summary["unsafe"] += 1
    return summary
