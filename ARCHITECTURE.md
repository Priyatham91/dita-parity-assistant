# Architecture & Logic Reference

Developer-facing reference for the DITA Parity Assistant. Covers the
end-to-end pipeline, what each module does, the data structures that
flow between them, every routing/safety rule encoded in the code, and
the commands to verify behavior with a fixture or a unit test.

If you only have time for one diagram, use the pipeline at the top of
[§2 — End-to-end pipeline](#2--end-to-end-pipeline) and the rules
catalog in [§7 — Diff engine logic](#7--diff-engine-logic) and
[§8 — Patch engine routing](#8--patch-engine-routing). Everything else
is reference-by-section.

For lint / CI / how-to-rebuild, see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
For the writer-facing guide, see [WRITER_GUIDE.md](WRITER_GUIDE.md).

---

## 1 — Quick orientation

**What the tool does.** Takes a live Help Center article HTML
plus the DITA topics + `.ditamap` that were migrated from the original
content. Produces a patched DITA tree where the safe, mechanical
content changes are auto-applied, and surfaces the rest as
needs-review advisories in an HTML report.

**How the tool is used.** A writer double-clicks
`dist/DitaParityAssistant.exe`. A local HTTP server starts on
`localhost:8000` (see [app/server.py](./app/server.py)). The writer
uploads the article HTML and the `.ditamap` + `.dita` files. The
server runs the pipeline below and writes the patched files +
`report.html` into `dist/output/runs/<timestamp>/outputs/`.

**Single-process, single-thread**, no background workers, no database.
Everything is one Python process; PyInstaller bundles it.

---

## 2 — End-to-end pipeline

```
   ┌────────────────────┐    ┌────────────────────┐
   │ article_source.html│    │   *.dita + .ditamap│
   └─────────┬──────────┘    └─────────┬──────────┘
             │                          │
             ▼                          ▼
  ┌───────────────────────┐  ┌──────────────────────┐
  │ article_html_parser   │  │ map_parser           │
  │ → [HtmlArticleBlock]  │  │ → TopicRef + MapLabel│
  └───────────┬───────────┘  │   + ReltableEntry    │
              │              └──────────┬───────────┘
              │                         │
              │                         ▼
              │              ┌──────────────────────┐
              │              │ publication_         │
              │              │ reconstructor        │
              │              │ → Publication(Blocks)│
              │              └──────────┬───────────┘
              │                         │
              ▼                         ▼
            ┌───────────────────────────────────┐
            │ diff_engine.diff(pub, texts, ...) │
            │ → List[DiffOp]                    │
            │   {EQUAL | REPLACE | DELETE |     │
            │    INSERT}                        │
            └─────────────────┬─────────────────┘
                              │
                              ▼
            ┌───────────────────────────────────┐
            │ patch_engine.apply_ops(ops, ...)  │
            │ → PatchReport + patched .dita     │
            └─────────────────┬─────────────────┘
                              │
                              ▼
            ┌───────────────────────────────────┐
            │ schematron_validator.validate_    │
            │ files(...) → ValidationReport     │
            └─────────────────┬─────────────────┘
                              │
                              ▼
            ┌───────────────────────────────────┐
            │ html_report.write_html_report     │
            │ + report_generator.write_         │
            │ patch_report → report.html +      │
            │ patch_report.txt                  │
            └───────────────────────────────────┘
```

The HTTP entry points that drive this are in
[`app/server.py`](./app/server.py):

- `POST /run` — initial upload. Saves files under `runs/<run_id>/inputs/`,
  resolves the article (URL fetch or upload), then calls `_run_pipeline`.
- `POST /runs/<run_id>/migrate` — "Run migration now" button on dry-run
  reports. Re-reads the same run's saved inputs and re-runs
  `_run_pipeline` with `dry_run=False`, **overwriting the same
  `outputs/` folder** so each article has a single run record. The
  report's panel handler in `app/html_report.py` (`_render_next_steps_panel`)
  posts here. A JS health check in `_SCRIPT_HTML` runs first so a writer
  viewing the report standalone (assistant closed) sees a clear alert
  rather than a "site can't be reached" page.

---

## 3 — Module map

| Module | Responsibility | Don't touch unless… |
|---|---|---|
| [`app/article_html_parser.py`](./app/article_html_parser.py) | HTML → `[HtmlArticleBlock]`. Recognizes paragraphs, lists, tabs, callouts, FAQ expandables, screenshots vs icons, link `(text, href)` pairs, emphasis phrases. | The Help Center markup changed (rare). |
| [`app/map_parser.py`](./app/map_parser.py) | `.ditamap` → `[TopicRef \| MapLabel]` for publication order; separately yields `[ReltableEntry]` for related-tasks/learn-more href detection. | A new map shape lands (e.g. `<topichead outputclass="...">` we haven't seen). |
| [`app/publication_reconstructor.py`](./app/publication_reconstructor.py) | Walks each topic's XML and emits a flat `Publication` of `Block`s. Synthesizes a block for each `MapLabel`. Computes `topic_to_section` for tab routing. | You need a new block type (e.g. a new structural element). |
| [`app/diff_engine.py`](./app/diff_engine.py) | LCS over publication blocks vs article blocks. Emits `DiffOp`s. Section-aware keys; ambiguity / cross-section / safety-net rules. | The diff is missing or over-firing changes. |
| [`app/patch_engine.py`](./app/patch_engine.py) | Mutates DITA. Applies REPLACE / DELETE / INSERT and emits a `PatchReport`. All apply-time safety guards live here. | New DITA shape needs routing, or a new advisory. |
| [`app/schematron_validator.py`](./app/schematron_validator.py) | Runs project Schematron rules on the patched files. | Schematron rules change. |
| [`app/html_report.py`](./app/html_report.py) | Renders the writer-facing report. | UI changes. |
| [`app/report_generator.py`](./app/report_generator.py) | Writes the side-by-side `patch_report.txt`. | (Same.) |
| [`app/server.py`](./app/server.py) | HTTP server, upload routing, run-directory layout. | Endpoint / wiring changes. |
| [`launcher.py`](./launcher.py) | PyInstaller entry point — just calls into `server.py`. | Almost never. |

---

## 4 — Key data structures

### `HtmlArticleBlock` ([article_html_parser.py](./app/article_html_parser.py))

| field | meaning |
|---|---|
| `text` | rendered text of the block (whitespace-normalized) |
| `kind` | `paragraph`, `note`, `step`, `unordered_step`, `list_item`, `table_row`, `heading`, `tab_label`, `expandable_header`, `tab_label_pending` |
| `note_kind` | `tip`, `important`, `warning`, `note`, etc. — set from CSS class AND headline phrase (§9) |
| `note_bullets` | populated when "Note:" + bullets get merged |
| `cells` / `cell_emphasis` | per-cell text / emphasis for `table_row` blocks |
| `links` | list of `(text, href)` pairs captured from `<a>` |
| `emphasis` | list of bold/italic phrases captured from `<strong>` / `<em>` |
| `section_id` | tab panel id (`panel-1-...`) when inside a tabpanel, `__post_tab__` after all tabs close, else `None` |
| `has_inline_image` | True for any `<img>`/`<li-icon>`/`<svg>` (icons OR screenshots) |
| `has_screenshot` | True ONLY for content screenshots (`class="article-content__image"`) |

### `Block` ([publication_reconstructor.py](./app/publication_reconstructor.py))

| field | meaning |
|---|---|
| `block_index` | 0-based position in the full publication |
| `topic_id` | href from the ditamap, or the literal `"<ditamap>"` for synthesized MapLabel blocks |
| `topic_path` | absolute path to the `.dita` file on disk |
| `element_xpath` | positional xpath used to find the element at apply time, e.g. `/concept/conbody/p[2]` |
| `element_tag` | local tag name (`p`, `note`, `title`, `cmd`, `row`, `li`, `dlentry`, …) |
| `text` | flattened text content (whitespace preserved at the block boundary) |
| `note_type` | `<note @type>` value for note blocks |
| `auto_update` | False → diff sets `safe_to_apply=False` on REPLACE/DELETE for this block |
| `skip_reason` | string surfaced when `auto_update=False` |
| `in_manual_zone` | block lives in Related-tasks / Learn-more zone — INSERT/REPLACE/DELETE all refused |
| `structural` | multi-child container (`<row>`, complex `<dlentry>`) — REPLACE refused |
| `xref_hrefs` | tuple of hrefs of every `<xref>` descendant. Used by the ambiguous-DELETE guard and the stale-href advisory |
| `map_depth` | depth in the `.ditamap` (0 = top-level topic). Used to detect when the publication leaves a tab topicgroup scope |

### `DiffOp` ([diff_engine.py](./app/diff_engine.py))

| field | meaning |
|---|---|
| `kind` | `EQUAL`, `REPLACE`, `DELETE`, `INSERT` |
| `source_block` | the publication `Block` being operated on (None for INSERTs) |
| `updated_text` | new text from the article (None for DELETE) |
| `updated_index` | position in the article-block list — used to fetch the `HtmlArticleBlock` for routing decisions in the patch engine |
| `anchor_block` | for INSERT: the publication `Block` the new content sits next to |
| `safe_to_apply` | False → routed to SKIPPED in the patch engine |
| `op_skip_reason` | string surfaced when `safe_to_apply=False` |

### `PatchResult` ([patch_engine.py](./app/patch_engine.py))

| field | meaning |
|---|---|
| `op` | the `DiffOp` (None for run-level advisories — see §10) |
| `category` | `APPLIED`, `SKIPPED`, `DETECTED`, `MAP_EDIT` |
| `reason` | populated for SKIPPED / DETECTED / MAP_EDIT |
| `warning` | populated for APPLIED entries with caveats |
| `topic_id` | optional — lets the HTML report file an advisory under a specific topic |

---

## 5 — Run directory layout

Every upload produces a directory under `dist/output/runs/<run_id>/`:

```
20260619_134222_65bc0d/
├── inputs/
│   ├── article_source.html                     ← copied from upload
│   ├── <map>.ditamap                            ← copied
│   ├── <topic_1>.dita                           ← copied
│   ├── …                                         ← copied
└── outputs/
    ├── <topic_1>.dita                           ← patched (empty on dry-run)
    ├── patch_report.txt                         ← plain-text summary
    └── report.html                              ← writer-facing UI
```

`inputs/` is also the source of truth for `POST /runs/<run_id>/migrate`
("Run migration now" button). When a writer dry-runs first and then
promotes, the migrate handler reads from this same `inputs/` directory
and overwrites `outputs/` in place. No fresh `run_id` is allocated —
keeps the run history a 1:1 mapping with articles instead of doubling
up dry-run + applied records.

To re-run a fixture without re-uploading, instantiate the pipeline
directly from the inputs/ folder — see [§11](#11--verifying-from-the-cli).

---

## 6 — Article HTML parser ([article_html_parser.py](./app/article_html_parser.py))

The parser is a state machine over `html.parser.HTMLParser`. Tags
either:

1. Open a *container* (collects body text into a buffer, emits a block
   at close). E.g. callouts, list items, table rows.
2. Open a *wrapper* (delegates emission to its child blocks, emits a
   fallback block only if nothing else did). E.g.
   `data-test-selector="preRenderedMarkup-container"`.
3. Open a *capture* (sets a flag on the innermost container — e.g.
   inline `<a>`, `<strong>`, `<img>`, `<li-icon>`).

### Notable behaviors (and the file:fixture that pins each)

| behavior | search term | regression test |
|---|---|---|
| Tab panel section_id stamping | `_panel_stack` | `PostTabContentDoesNotInheritTabBinding` |
| Post-tab section stamping (`__post_tab__`) | `_tabs_have_closed` | `PostTabContentRoutesToPostTabTopic` |
| Screenshot vs icon distinction | `article-content__image` | `ScreenshotPresenceAdvisory` |
| `li-icon` recognized as icon glyph | `("img", "picture", "svg", "li-icon")` | `IconPresenceAdvisory` |
| Forward inline-image flag when wrapper delegates | `entry.get("has_inline_image")` (inside `_pop_one`) | `ScreenshotPresenceAdvisory.test_screenshot_after_delegated_paragraph_is_captured` |
| FAQ expandable header (kind=`expandable_header`) | `article-content__collapsible-trigger-text` | `NewFaqQuestionAdvisory` |
| Headline-based note_kind upgrade (`Here's a tip` → tip) | `_note_kind_from_headline` | `NoteTypeUpgradesFromHeadline` |
| Callout headline text captured even when suppressed | `headline_capture` | (same) |
| Tab-label re-emission at panel-open time | `_pending_tab_labels` | n/a |
| Note label + bullets merge (`note_bullets`) | `expand_blocks_for_diff` | `NoteBulletsRebuild` (in regression file) |

### Why both `has_inline_image` and `has_screenshot`?

The screenshot advisory should fire on every position with a content
image (writer must place an `<image>` element). The icon advisory
should fire only on INSERT/REPLACE ops — every step that mentions a UI
button has an icon glyph, surfacing them all on EQUAL blocks would be
pure noise. So:

- `has_inline_image`: any icon or screenshot present.
- `has_screenshot`: content screenshot (class `article-content__image`).
- Icon advisory = `has_inline_image AND NOT has_screenshot` AND op kind ∈
  {INSERT, REPLACE}.

---

## 7 — Diff engine logic ([diff_engine.py](./app/diff_engine.py))

The diff is `difflib.SequenceMatcher` over keys, with post-processing.

### 7.1 LCS keys

When tab routing is active (`topic_to_section` is non-empty), each
key is `normalize_for_match(text) + "\x00§" + section_id + "§\x00"`.
Identical text in different tab sections has different keys, so LCS
can't pair them. This is what makes tab routing actually work — the
apply-time guard catches the rest.

### 7.2 The four opcode kinds and what we emit

| LCS opcode | What we emit | Notes |
|---|---|---|
| `equal` | `EQUAL` per paired block | Sets `last_source` for downstream INSERT anchoring |
| `delete` | `DELETE` per source block, **after** the safety net (§7.5) | |
| `insert` | `INSERT` per article block, anchored at `last_source` | |
| `replace` | Several rules — see §7.3 | |

### 7.3 Replace opcode rules

For a `replace` opcode covering `src_len` source blocks and `upd_len`
article blocks:

1. `ambiguous = src_len > upd_len`. The segment-wide flag.
2. For each paired position `(src[i], upd[i])` (i in 0..paired):
   - **High-overlap escape** (rescues a clear 1:1 pair from `ambiguous=True`):
     `_is_high_overlap_pair` returns True when EITHER:
       - Jaccard ≥ 0.55, OR
       - one side's content words are a subset of the other AND the
         smaller side has ≥ 2 content words (the "Click Save" case).
   - **Cross-section demotion**: if `src` section ≠ `upd` section, the
     pair is fundamentally wrong (LCS only matched by content). Emit
     DELETE for `src` + INSERT for `upd` with a section-matching anchor.
   - **`_looks_like_same_block` demotion**: if Jaccard < 0.15, emit
     DELETE for `src` + INSERT for `upd`. The "they share no content"
     case.
   - Otherwise emit REPLACE.
3. Leftover source blocks → DELETE. Each runs the safety net (§7.5)
   individually. If the safety net allows the delete, it's safe even
   when the surrounding segment is ambiguous.
4. Leftover article blocks → INSERT, anchored at `last_source`.

### 7.4 Section-matching INSERT anchor

When we cross-section-demote (§7.3 step 2.b), the INSERT shouldn't
anchor at the last source block we processed — that's in the wrong
section. Instead we look up `_section_matching_anchor(art_section, src_idx)`:

- Precompute `_section_to_src_indices[section] -> sorted list of src indices`.
- For a given article section + current src position, prefer the source
  block in matching section *just before* the current position; fall
  back to the *next* matching section block after, then to `last_source`.

This is what makes a post-tab article paragraph anchor at the post-tab
DITA topic, even when the previous source block was a tab step.

### 7.5 Safety net

`_source_block_appears_in_article(src, src_idx)` returns True when the
source block's content appears anywhere in the article that the LCS
hasn't already paired. When True, we suppress the DELETE.

**Section awareness**: a candidate article block in a different section
from `src` doesn't count. Beta surfaced this on the Mobile tab's
"Post an update..." step — text was in the article, but in a different
section, so the source step should still DELETE.

**Already-consumed exclusion**: an article block already paired (EQUAL
or in a replace's paired prefix) doesn't count. This is what allowed
the Non-U.S. Affidavit DELETEs to fire even though their U.S. siblings
matched article items.

### 7.6 DELETE+INSERT coalescing

Final post-pass in `diff()`: when a DELETE on block X plus an INSERT
anchored at X both exist, merge into a single REPLACE on X. Without
this, the apply-time order (DELETEs before INSERTs) would remove the
anchor and the INSERT would fail to resolve its xpath.

### 7.7 Adjacent-insert dedupe

`_dedupe_adjacent_inserts` drops trailing INSERTs whose text equals
the next paired block's text. Catches a Help Center copy-paste mistake
where the same paragraph appears twice in a row.

---

## 8 — Patch engine routing ([patch_engine.py](./app/patch_engine.py))

`apply_ops` orchestrates everything. Order matters:

1. Pre-compute mass-delete topics (§10.1).
2. Emit topic-level advisories (mass-delete, stale reltable, new FAQ,
   screenshot, icon).
3. Group mutating ops by topic; route reltable INSERTs to one
   consolidated MAP_EDIT.
4. For each topic, open the file once, run ops in this order:
   - **REPLACE** (no shift).
   - **DELETE** in reverse doc order (preserves earlier indices).
   - **INSERT** in reverse doc order. Anchors are **pre-resolved to
     element references before any DELETE runs** so they stay valid
     across sibling renumbering. ([Fix #15])
5. Write the file.

### 8.1 INSERT routing decisions

Dispatched by `_dispatch_insert` based on article block `kind`:

| article kind | handler | notable rules |
|---|---|---|
| `step` | `_insert_step_handler` | Case 1: in `<step>` ancestor → sibling step. Case 2: in `<taskbody>` → find/create `<steps>` and prepend. Case 3: concept/reference → `<ol><li>`. |
| `unordered_step` | `_insert_unordered_step_handler` | Same idea, with bullet/Term-Description and a route-into-existing-`<ul>` short-circuit (for prereqs). |
| `list_item` | `_insert_list_item_handler` → `_insert_li_into_list(..., "ul")` | Routes into `<prereq>` if parent is `<taskbody>` and prereq exists. |
| `note` | `_insert_note_handler` | Rebuilds `<note><p>…</p>` or `<note><ul>…` per IM 132. |
| `table_row` | `_insert_table_row_handler` | Builds `<row>` with cells per article-side `cells`. |
| `paragraph` | inline path | Title-anchor redirect when parent is concept/task/reference. |
| `heading` | `_insert_heading_handler` | Builds `<section><title>…</title></section>`. |

### 8.2 REPLACE handlers (special elements)

- **`<row>`**: `_apply_cell_aware_row_replace` rewrites only the cells
  that actually changed; preserves cells with block children
  (`<note>`, `<ul>`).
- **`<dlentry>`**: `_apply_dlentry_replace` splits the article text on
  the first `:`/`-`/`–`/`—`, validates the term still matches `<dt>`,
  rewrites the `<dd>` only. Preserves inline markup (`<uicontrol>`,
  `<xref>`) when each child's phrase still appears in the new
  definition; otherwise falls back to plain text with a warning.
- **`<note>`**: rebuilds children as `<p>` or `<ul><li>` based on
  article-side `note_bullets`. Updates `@type` from article
  `note_kind` ([Fix #16]).

### 8.3 Inline-markup preservation on regular elements

When a REPLACE has `had_children and not links`:
1. Try `_replace_preserving_inline_markup(element, new_text, emphasis)`.
2. If that returns False (one of the wrapped phrases isn't in the new
   text), the original `<uicontrol>`/`<xref>` can't be preserved. But
   the article-side emphasis can: clear children, re-run the same
   function with emphasis only so the new bolded phrases get `<em>`
   wraps. ([Fix #12])

### 8.4 Apply-time guards (refuse-and-surface)

| guard | what it refuses | reason |
|---|---|---|
| `_is_cross_section` | INSERT/REPLACE where article block's section ≠ target topic's section | "place in the correct tab manually" |
| `_has_media_children` | DELETE on a block containing `<image>`/`<object>`/`<fig>` | parser doesn't preserve embedded media |
| `_ambiguous_xref_sibling_delete` | DELETE on an `<xref>`-bearing item with a sibling sharing the same visible text but a different href | tool can't pick the winner |
| Topic `<title>` DELETE | DELETE on `/title[1]` | title removal is almost always misalignment |
| Mass-delete guard | All DELETEs in a topic with ≥80% body removal OR (title gone AND ≥50% removal) | likely retired topic; single advisory replaces N per-block lines |

### 8.5 INSERT routing into `<prereq>`

When `_insert_li_into_list` is called with `parent_tag ∈ {"taskbody",
"conbody", "refbody"}` and the parent has a child `<prereq>` with a
matching list (`<ul>` for prefer_tag=ul, `<ol>` for ol), the new `<li>`
goes inside that prereq's list — not as a sibling of `<prereq>`. This
prevents the invalid-DITA "ul outside prereq" output Beta surfaced.

---

## 9 — Note type handling (headline → @type)

The Help Center sometimes uses the generic `--note` CSS class but a
specific headline ("Here's a tip", "Important"). The DITA `<note
@type>` should follow the headline, not the class.

**Parser side** ([article_html_parser.py](./app/article_html_parser.py)):
- The `<h3 class="…callout__headline">` branch sets
  `entry["headline_capture"] = True` and stores a pointer to the
  enclosing callout entry.
- `handle_data` captures headline text into a parallel buffer **even
  when suppressed** (the body buffer is suppressed for marker callouts
  to avoid emitting "Here's a tip" as body content).
- `_pop_one` matches the captured headline against `_HEADLINE_TO_NOTE_KIND`
  and, if matched, updates the enclosing callout's `note_kind`.

**Patch side** ([patch_engine.py](./app/patch_engine.py)): the `<note>`
REPLACE handler reads `getattr(article_block, "note_kind", None)` and,
when present and different from `element.get("type")`, calls
`element.set("type", article_note_kind)`.

Marker phrases recognized (case-insensitive, apostrophe-insensitive):

```
here's a tip, heres a tip → tip
important to know, important → important
caution → caution
warning → warning
remember to, remember → remember
attention → attention
who can use this feature → permission
```

---

## 10 — Advisory catalog

All advisories are `PatchResult(op=None, category=DETECTED|MAP_EDIT)`.
They appear in the HTML report's needs-review / add-manually section.

### 10.1 Mass-delete advisory

Fires when EITHER:
- ≥80% of a topic's blocks queued for DELETE, OR
- The topic `<title>` is in DELETE AND ≥50% of body blocks are in DELETE.

When it fires, **all per-block DELETE PatchResults for that topic are
suppressed** (early-skip in the per-op loop). The single advisory tells
the writer to delete the file + the `<topicref>`.

### 10.2 Stale `<xref href>` advisory (topic body)

When a source block has `xref_hrefs` and the LCS pairs it as EQUAL with
an article block whose `links` hrefs don't match (after URL
normalization — see `_normalize_href_for_compare`), emit a topic-level
advisory listing every stale link.

### 10.3 Stale reltable href advisory (`.ditamap`)

Parallel scanner for `<reltable>` topicrefs in the `.ditamap`. Matches
article-side Related-tasks/Learn-more navtitles by text; flags hrefs
that don't match.

### 10.4 Reltable map-edit reminder

Fires ONLY when:
- The article has reltable items not present in the `.ditamap` reltable, OR
- The `.ditamap` has no reltable at all.

Otherwise suppressed. (Beta surfaced an unconditional reminder that
read as "action needed" even when nothing changed.)

### 10.5 Screenshot advisory

Lists every article block with `has_screenshot=True`. Tells the writer
to place the `<image>` element by hand (we don't know the asset href).

### 10.6 Icon advisory

Lists INSERT/REPLACE ops whose article block has `has_inline_image=True`
AND NOT `has_screenshot=True`. EQUAL blocks are excluded — the DITA
already has the icon.

### 10.7 New FAQ topic advisory

Each article-side `expandable_header` whose normalized text doesn't
match a source topic title.

---

## 11 — Verifying from the CLI

### 11.1 Run the regression suite

```bash
cd migration_assistant
python -m unittest discover -s tests -v
```

Every fix from the beta rounds has at least one test in
[tests/test_regression_fixes.py](./tests/test_regression_fixes.py).
Test class names map to feature areas — search by class name when you
want to know "what does the tool guarantee about X."

### 11.2 Run a single fixture without the server

Use the inputs/ folder of any preserved beta run. Inline script:

```python
import sys; sys.path.insert(0, ".")
from pathlib import Path
from app.article_html_parser import parse_help_center_html, expand_blocks_for_diff
from app.diff_engine import diff
from app.map_parser import parse_ditamap_entries, parse_reltable_entries
from app.patch_engine import apply_ops
from app.publication_reconstructor import reconstruct

base = Path("Beta test/Issue/20260619_134222_65bc0d/inputs")
ditamap = next(base.glob("*.ditamap"))
blocks = parse_help_center_html((base / "article_source.html").read_text(encoding="utf-8"))
texts, _ = expand_blocks_for_diff(blocks)
pub = reconstruct(parse_ditamap_entries(ditamap))
ops = diff(pub, texts, article_blocks=blocks)
report = apply_ops(
    ops, Path("/tmp/out"), article_blocks=blocks, publication=pub,
    reltable_entries=parse_reltable_entries(ditamap),
)
```

`report.results` is a flat list of `PatchResult`s; iterate to inspect
what fired and why.

### 11.3 Inspect a diff op's anchor and kind

```python
for op in ops:
    if op.source_block is not None and "Post an update" in op.source_block.text:
        print(op.kind, op.safe_to_apply, op.op_skip_reason)
```

### 11.4 Check what an article block's section / kind is

```python
for i, b in enumerate(blocks):
    print(i, b.kind, b.section_id, b.text[:80])
```

### 11.5 Regenerate a preserved run's `report.html`

The `_regen` pattern (used during dev to refresh after a code change):

1. Walk the `Beta test/.../inputs/` directory.
2. Run the pipeline above.
3. Pass `report` to `app.html_report.write_html_report(...)`.

Or just relaunch the rebuilt `dist/DitaParityAssistant.exe` and upload
the inputs again through the UI.

### 11.6 Rebuild the `.exe`

```bash
cd migration_assistant
python -m PyInstaller DitaParityAssistant.spec --noconfirm
```

The rebuilt binary lands at `dist/DitaParityAssistant.exe`. Close any
running instance first (file lock).

---

## 12 — Gotchas / non-obvious behavior

### XPath stability

DITA xpaths in `Block.element_xpath` are *positional* (`p[2]`, `li[3]`).
They go stale the moment a sibling is removed. The patch engine:

- Pre-resolves every INSERT op's anchor to an element reference
  **before any DELETE runs**.
- Uses `_find_parent_of(root, anchor_elem)` (tree walk) instead of
  `_find_parent_and_child(root, anchor_xpath)` (positional) when the
  anchor is from the cache.

Element references from `xml.etree.ElementTree` remain valid across
DELETEs on other elements — only positional xpaths break.

### LCS replace segments are not "rewrite pairs"

LCS produces a `replace` opcode for any region that's neither EQUAL
nor a pure delete / pure insert. It does **not** mean the pairs inside
are rewrites of each other. The `_looks_like_same_block` and
`_is_high_overlap_pair` checks decide that, with section-aware demotion
on top.

### `last_source` is not always the right INSERT anchor

LCS processes opcodes left-to-right. An `insert` opcode can appear
*before* the corresponding `delete` of the source block the article
content should replace (different (i1, j1) ordering). Always check
whether a section-matching anchor would be better; the section-aware
anchor logic in `diff_engine` handles this for cross-section demotions.

### Console encoding on Windows

Print statements with `→`, `▸`, `…`, smart quotes will crash on
default cp1252 consoles. Encode to ASCII with `errors="replace"` for
debug prints, or write to a file and read it back. The HTML report
uses UTF-8 and renders fine — only the dev-side prints are affected.

### Two unrelated functions named `_normalize_href`

- `_normalize_href_for_compare` ([patch_engine.py](./app/patch_engine.py))
  is used by the stale-href advisories — strips scheme/host/trailing-slash.
- `_normalize_href` (same file, later) is used when *generating*
  DITA xrefs — turns `/help/...` into a full URL. Don't confuse them.

### `note` is in `_MARKER_CALLOUT_KINDS`

The article-side headline `Here's a tip` is text content inside a
suppressed h3 (because the enclosing callout's note_kind is `note`,
which IS in the marker set). The headline-capture mechanism runs
**before** the suppress check in `handle_data` so the headline text is
still available for the note-type upgrade.

### When the article has fewer blocks than the source

The LCS produces a `replace` opcode covering both sides. The leftover
source blocks become DELETEs, but they live inside an ambiguous
segment, so safe_to_apply could be False. The fix is to run the safety
net per leftover block: if the source content doesn't appear in any
unconsumed article block in the matching section, the leftover DELETE
is unambiguous (it really has no article counterpart) — safe to apply
regardless of the segment-wide flag.

---

## 13 — Where each beta-round fix lives

| Fix | What it does | Module | Anchor test class |
|---|---|---|---|
| Callout headline capture inversion | only marker kinds suppress headline | parser | `FeatureCalloutHeadlineIsCaptured` |
| Dlentry separator equivalence | `:`/`-`/`–`/`—` normalize equal | reconstructor | `DlSeparatorEquivalence` |
| 50-char threshold on `_is_no_real_change` | long sentence removals not suppressed | patch_engine | `SentenceRemovalIsNotSuppressed` |
| Cell-aware row REPLACE | rewrite only changed cells | patch_engine | `CellAwareRowReplace` |
| Ambiguous-REPLACE high-overlap escape | rescue clear 1:1 from segment-wide refusal | diff_engine | `AmbiguousReplaceHighOverlapEscapeHatch` |
| Xref vs media in DELETE guard | only image/object/fig block DELETE | patch_engine | `XrefInDeletedParagraphIsNotMedia` |
| Section-aware DELETE safety net | consumed-article exclusion | diff_engine | `DeleteSafetyNetSkipsConsumedArticleBlocks` |
| Entire-topic-removed advisory | one advisory, suppress per-block | patch_engine | `MassDeleteAdvisorySurfaces`, `MassDeleteGuardSmarterThreshold` |
| Dlentry-aware REPLACE | split on separator, validate term, rewrite dd | patch_engine | `DlEntryAwareReplace` |
| Ambiguous-xref DELETE refusal | duplicate-text different-href siblings | patch_engine | `AmbiguousXrefSiblingDelete` |
| Stale href advisory | EQUAL pairing with href mismatch | patch_engine | `StaleHrefAdvisory` |
| Reltable href staleness | scan `.ditamap` reltable | map_parser + patch_engine | `ReltableStaleHrefAdvisory` |
| Screenshot vs icon distinction | content image vs glyph | parser + patch_engine | `ScreenshotPresenceAdvisory` |
| Tab-aware alignment | depth-scoped section binding | reconstructor + diff | `PostTabContentDoesNotInheritTabBinding` |
| FAQ new-question advisory | unmatched `expandable_header` | parser + patch_engine | `NewFaqQuestionAdvisory` |
| Reltable noise suppression | only flag when items missing from map | patch_engine | n/a (manual on Article 2) |
| Icon-presence advisory | INSERT/REPLACE on icon-bearing block | patch_engine | `IconPresenceAdvisory` |
| `<li-icon>` recognition | treat as inline image source | parser | `IconPresenceAdvisory` (relies on it) |
| INSERT into `<steps>` | new steps as `<step>`, not `<ol>` | patch_engine | `InsertStepIntoTaskbodyRoutesToSteps` |
| INSERT into `<prereq>` | new bullets inside prereq's ul | patch_engine | `InsertIntoPrereq` |
| Emphasis fallback `<em>` wrap | when markup preservation fails | patch_engine | `EmphasisFallbackWrapsInEm` |
| Section-aware DELETE safety + cross-section REPLACE demotion | cross-section pairs become DELETE+INSERT | diff_engine | `SectionAwareDeleteSafetyAndDemote` |
| Post-tab routing | content after tabs → post-tab.dita | parser + reconstructor + diff | `PostTabContentRoutesToPostTabTopic` |
| INSERT anchor pre-resolution | refs survive sibling DELETE renumbering | patch_engine | `InsertAnchorSurvivesDeletes` |
| Note type from headline | `Here's a tip` → `<note type="tip">` | parser + patch_engine | `NoteTypeUpgradesFromHeadline` |
| Subset escape for shortened text | Click Save case | diff_engine | `AmbiguousReplaceSubsetEscape` |

---

## 14 — Performance & limits

- All inputs fit in memory. No streaming.
- LCS is `O(n²)` over the longer of (source blocks, article blocks).
  Real articles produce a few hundred blocks; this is fine.
- The patched DITA files are written once per topic; no incremental
  writes.
- Schematron validation uses `lxml.isoschematron` — slowest step on a
  large publication, but bounded by `O(files × rules)`.

---

## 15 — How to add a new behavior

1. Pick the right layer (parser / reconstructor / diff / patch /
   report). The module map in §3 should tell you within 10 seconds;
   if you're unsure, the diff is for "what changed between source and
   article," the patch is for "how do we mutate the DITA safely."
2. Write the smallest test that fails today — use the synthetic
   `tempfile + write_text` pattern, NOT a real beta fixture (so the
   test runs on CI without input files). See any test class in
   `test_regression_fixes.py`.
3. Implement. Most behaviors land in 1–2 functions.
4. Run the suite: `python -m unittest discover -s tests`.
5. Spot-check the relevant beta report by regenerating its
   `report.html` (§11.5).
6. `python -m ruff check migration_assistant` before committing.

A new advisory? Pick the right insertion point in `apply_ops` (§8) —
they all follow the same pattern: scan ops/blocks/article_blocks,
collect a list, emit one `PatchResult(op=None, category=DETECTED, …)`.

---

## 16 — Tool retirement

This was designed as a one-time migration helper for a finite parity
backlog. Once the backlog is processed:

- Archive the codebase to a read-only location.
- Disable the CI workflow (`tests.yml`).
- Keep this ARCHITECTURE.md alongside the codebase as a record of
  why the tool existed and the decisions baked into it.
