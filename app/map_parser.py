"""Parse a .ditamap and yield topic references in publication order.

The .ditamap is the source of truth for topic ordering. We walk <topicref>
elements depth-first in document order, which is how an AEM Guides DITA
publication is rendered linearly.

We deliberately skip:
  - <reltable> subtrees: relationship tables are publishing metadata, not
    publication content. Their topicrefs are typically external URLs.
  - <topicmeta> subtrees: per-topicref metadata, not content.
  - topicrefs with scope="external" or scope="peer": point outside the
    local content set; we can't (and shouldn't) reconstruct them.
  - topicrefs with format other than "dita" / "dita-topic" / "dita-map":
    e.g. format="html" links cannot be parsed as DITA.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class TopicRef:
    topic_id: str          # stable id: the href as written in the map
    href: str              # raw href attribute
    resolved_path: Path    # absolute path on disk
    depth: int             # nesting depth in the map (0 = top-level)
    map_position: int      # global flat index in publication order


@dataclass(frozen=True)
class MapLabel:
    """A heading that lives in the .ditamap (e.g. <topichead><navtitle>),
    not in any topic body. Used as a synthesized block during publication
    reconstruction so the article's section headings align with it."""
    text: str
    depth: int
    map_position: int


@dataclass(frozen=True)
class ReltableEntry:
    """One link entry inside a <reltable> cell — typically a 'Related
    tasks' or 'Learn more' item with an external href. The patch
    engine uses this to detect href staleness against the article's
    same-labelled link (e.g. a 'Contact us' link whose URL changed)."""
    section: str   # "Related tasks" / "Learn more" / "See also" (from relcolspec @type)
    navtitle: str  # link display text
    href: str      # raw href attribute


SKIP_PARENT_TAGS = {"reltable", "topicmeta"}
DITA_FORMATS = {None, "", "dita", "dita-topic", "dita-map"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_ditamap(ditamap_path: Path) -> List[TopicRef]:
    """Backward-compatible: returns only the TopicRef entries."""
    return [e for e in parse_ditamap_entries(ditamap_path) if isinstance(e, TopicRef)]


def parse_ditamap_entries(ditamap_path: Path):
    """Walk the .ditamap and yield publication-order entries.

    Yields a list mixing TopicRef (a .dita file to load) and MapLabel
    (a topichead/topicgroup navtitle that exists only in the map).
    Reconstruction inserts a synthesized block for each MapLabel at its
    publication-order position so article-side section headings can
    align against it instead of producing false INSERTs.
    """
    ditamap_path = Path(ditamap_path)
    tree = ET.parse(ditamap_path)
    root = tree.getroot()

    base_dir = ditamap_path.parent
    entries: list = []

    def _navtitle_of(elem: ET.Element):
        # Look for <topicmeta>/<navtitle> or @navtitle attribute.
        for child in elem:
            if _local(child.tag) == "topicmeta":
                for grand in child:
                    if _local(grand.tag) == "navtitle":
                        text = (grand.text or "").strip()
                        if text:
                            return text
        nav_attr = elem.attrib.get("navtitle", "").strip()
        return nav_attr or None

    def walk(element: ET.Element, depth: int) -> None:
        for child in element:
            tag = _local(child.tag)

            if tag in SKIP_PARENT_TAGS:
                continue

            if tag == "topicref":
                href = child.attrib.get("href")
                scope = child.attrib.get("scope", "local")
                fmt = child.attrib.get("format")

                if href and scope == "local" and fmt in DITA_FORMATS:
                    resolved = (base_dir / href).resolve()
                    entries.append(
                        TopicRef(
                            topic_id=href,
                            href=href,
                            resolved_path=resolved,
                            depth=depth,
                            map_position=len(entries),
                        )
                    )
                    walk(child, depth + 1)

            elif tag in ("topichead", "topicgroup"):
                # No href, just a structural grouping. Emit its navtitle
                # as a MapLabel so the article's section heading (e.g.
                # "Features") can align against it.
                nav = _navtitle_of(child)
                if nav:
                    entries.append(
                        MapLabel(
                            text=nav,
                            depth=depth,
                            map_position=len(entries),
                        )
                    )
                walk(child, depth + 1)

            else:
                # Containers like <map>'s root or other passthroughs.
                walk(child, depth)

    walk(root, 0)
    return entries


def parse_reltable_entries(ditamap_path: Path) -> List[ReltableEntry]:
    """Walk the .ditamap's <reltable> subtrees and yield ReltableEntry
    records, one per non-source link cell. Skips the source column
    (linking="sourceonly"), since that just references the topic the
    reltable is attached to.

    Used by the patch engine to detect href staleness — when the
    article's "Related tasks" / "Learn more" section lists a link with
    the same display text but a different href, the DITA reltable is
    likely outdated (commonly due to the project's article-ID format
    change from /87951 → /a1342713).
    """
    ditamap_path = Path(ditamap_path)
    tree = ET.parse(ditamap_path)
    root = tree.getroot()
    entries: List[ReltableEntry] = []

    for reltable in root.iter():
        if _local(reltable.tag) != "reltable":
            continue
        # Map each relcolspec position to its section label.
        section_per_col: List[str] = []
        relheader = next(
            (c for c in reltable if _local(c.tag) == "relheader"), None,
        )
        if relheader is not None:
            for col in relheader:
                if _local(col.tag) != "relcolspec":
                    continue
                section_per_col.append(col.attrib.get("type", "").strip())
        for relrow in reltable:
            if _local(relrow.tag) != "relrow":
                continue
            col_idx = 0
            for relcell in relrow:
                if _local(relcell.tag) != "relcell":
                    col_idx += 1
                    continue
                section = (
                    section_per_col[col_idx]
                    if col_idx < len(section_per_col) else ""
                )
                col_idx += 1
                # Skip the sourceonly column (references the host topic).
                if col_idx <= len(section_per_col) and not section:
                    # Empty section label = source column.
                    continue
                for tref in relcell:
                    if _local(tref.tag) != "topicref":
                        continue
                    href = tref.attrib.get("href", "").strip()
                    if not href:
                        continue
                    navtitle = ""
                    for child in tref:
                        if _local(child.tag) == "topicmeta":
                            for grand in child:
                                if _local(grand.tag) == "navtitle":
                                    navtitle = "".join(
                                        grand.itertext()
                                    ).strip()
                                    break
                            break
                    entries.append(
                        ReltableEntry(
                            section=section, navtitle=navtitle, href=href,
                        )
                    )
    return entries
