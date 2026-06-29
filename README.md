# DITA Parity Assistant

A **deterministic**, **AI-free** tool that keeps DITA topics in sync with their HTML source articles. When a Help Center article gets edited, this tool finds the prose changes, applies the safe ones back into the converted DITA, and refuses anything it can't verify — with a written reason in the report.

> **About this portfolio copy.** This repository is a sanitized snapshot of a tool built for a technical-writing team. Internal brand references, proprietary Schematron rules, and internal-only documents have been replaced with generic placeholders so the engineering can stand on its own. The 129-test regression suite, the safety architecture, and the diff/patch engines are intact.

---

## Why this exists

Once a doc team converts an HTML article to DITA, the *next* article edit creates a fork. Either the writer hand-edits both copies forever, or the DITA falls behind. Most "sync" tools quietly overwrite — which is fine until it isn't.

This tool takes a different stance: **refuse rather than corrupt.** When the rules can't verify a change is safe, it surfaces the item with a written explanation and leaves the file untouched. The writer has the final call.

---

## What it does

1. **Reconstructs the publication** from the `.ditamap` in publication order, producing a flat list of "source blocks" tagged with topic, xpath, element tag, and ownership flags.
2. **Parses the article HTML** into a parallel list of typed blocks (paragraph, note, step, list_item, heading, tab_label, table_row) with section affinity and inline-emphasis spans.
3. **Aligns** both block streams with `difflib.SequenceMatcher` (Myers LCS) and classifies each non-equal pair as REPLACE, INSERT, or DELETE.
4. **Patches** the DITA in place where it's safe, refusing anything that hits a guard. Markup-preserving REPLACE rebuilds elements so inline `<uicontrol>`, `<xref>`, `<keyword>`, etc. survive a rewording.
5. **Validates** the patched files with Schematron rules ([schematron/style_rules.sch](schematron/style_rules.sch) — a small generic demo set you can replace with your own).
6. **Reports** APPLIED / SKIPPED / DETECTED / MAP_EDIT entries with reasons, a track-changes XML diff per file, and a Schematron rule-fail summary.

---

## Safety guards (why a refusal is good news)

| Guard | When it fires | Why |
|---|---|---|
| **Inline markup REPLACE** | Source has `<xref>`/`<uicontrol>`/etc. children that the article would discard | Avoids silently stripping semantically-rich markup |
| **Markup-preserving REPLACE** | (Upgrade) The article rewording still contains all original inline phrases | Rebuilds the element keeping markup, **applies the change** |
| **Media DELETE** | DELETE on an element with `<xref>`/`<image>`/`<object>`/`<fig>` children | The article parser doesn't capture iframe embeds — a clean DELETE would silently strip the media |
| **Title DELETE** | DELETE on `/title[1]` of any topic | Topic titles never disappear from real article updates |
| **Mass-deletion guard** | ≥3 DELETEs covering ≥50% of one topic's source blocks | Catches the "diff aligned nothing in this file" failure mode |
| **Cross-tab routing** | INSERT/REPLACE where the article block's `tabpanel` doesn't match the target topic's tab | Stops content for one tab landing in the wrong tab's file |
| **Reltable consolidation** | INSERTs after a "Related tasks"-style heading | Routes to a single MAP_EDIT pointer at the `.ditamap` |

Each refusal includes a written reason in the report. A SKIPPED REPLACE also surfaces the new article text as a paired DETECTED entry so it can be placed manually.

---

## Quick start

```powershell
# From the project root
python app/server.py
# then open http://localhost:8000/
```

Upload:
- A `.ditamap`
- Every `.dita` topic the map references
- Either a Help Center URL **or** a saved `article_source.html`

The server runs the pipeline, then redirects to a per-run HTML report under `output/runs/<run_id>/outputs/report.html`.

To point at a Help Center other than the demo default, set `HELP_CENTER_HOST`:

```powershell
$env:HELP_CENTER_HOST = "https://help.your-org.com"
python app/server.py
```

---

## Running the tests

```powershell
python -m unittest tests.test_regression_fixes
python -m unittest tests.test_html_parser_smoke
python -m unittest tests.test_normalize_typography
python -m unittest tests.test_note_prefix
```

`test_regression_fixes.py` is the codebase contract — 129 tests pinning every behavior reported during a real beta. If any of them start failing, that's a regression — investigate before merging.

---

## Building the desktop app

The team using this tool runs a single PyInstaller-bundled `.exe`. To rebuild:

```powershell
pyinstaller DitaParityAssistant.spec --clean --noconfirm
# → dist\DitaParityAssistant.exe
```

The exe is self-contained — no Python install needed on the writer's machine.

---

## What's covered

**Block elements:** `<p>`, `<note>` (all IM types incl. `othertype`), `<ul>`/`<ol>`/`<li>`, `<steps>`/`<step>`/`<cmd>`, `<steps-unordered>`, `<section>`/`<title>`, `<table>`/`<row>`/`<entry>`, `<dl>`/`<dlentry>`.

**Inline elements (preserved on rewording):** `<uicontrol>`, `<xref>`, `<image>`, `<keyword>`, `<wintitle>`, `<em>`, `<b>`. Article `<strong>`/`<em>`/`<i>`/`<b>` map to DITA `<em>` (with an APPLIED-with-warning so you can upgrade to `<uicontrol>` / `<wintitle>` / `<keyword>` if semantically more accurate).

**Topic affinity:** Article tab buttons (e.g. `Desktop` / `Mobile`) get matched against topic `<title>`s and ditamap `<topichead navtitle>`s, so content in `panel-1` only routes to the Desktop topic.

**Reltable awareness:** "Related tasks", "Learn more", "See also" sections fold into a single MAP_EDIT entry pointing at the ditamap's `<reltable>` rather than emitting noise per link.

## What's *not* covered (yet)

- `<fig>` / `<image>` insertion (figures with captions)
- `<codeblock>` / `<pre>`
- `<example>` blocks
- Topic specializations: `<glossentry>`, `<troubleshooting>`, `<learningContent>`
- Articles where the structure has been radically reorganized (the diff falls back to mass-deletion guard refusals)
- Articles in languages other than English (tested only with `xml:lang="en-US"`)
- Embedded video/iframe content that doesn't exist as an `<xref><image>` in the source DITA

---

## Repository tour

```
app/                    Python pipeline (parser, diff, patch, report)
tests/                  unittest regression suite (129 tests)
schematron/             example style rules (generic demo set)
information_model/      placeholder for your IM reference docs
docs/
  DEVELOPMENT.md        lint, CI, build notes
  WORKING_WITH_COPILOT.md  briefing prompt for AI coding assistants
ARCHITECTURE.md         engineering reference — pipeline, modules, rules
WRITER_GUIDE.md         writer-facing tool guide
DitaParityAssistant.spec  PyInstaller spec
launcher.py             exe entry point
```

For a deep dive on the pipeline, the diff engine, and every routing rule, read [ARCHITECTURE.md](ARCHITECTURE.md). For the writer-facing UX, read [WRITER_GUIDE.md](WRITER_GUIDE.md).

---

## Design philosophy

A handful of principles guided every decision:

- **Refuse rather than corrupt.** Every guard exists because a smarter heuristic would have been wrong on real content.
- **Deterministic and auditable.** No AI, no model calls, no randomness. Same inputs → same outputs.
- **YAGNI.** The codebase is deliberately small. A dead-code audit in 2026-06 removed ~225 lines of speculative UI work.
- **The tests are the contract.** Every behavior writers reported during beta is pinned by a regression test. The 129 tests are the spec; the code is the implementation.

---

## License

MIT — see [LICENSE](LICENSE).
