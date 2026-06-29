# Update DITA topics from article changes

This tool helps you keep DITA topics in sync with their Help Center articles. When an article changes, the tool finds the differences and applies them to your DITA topics. You then review the report and confirm or adjust what it did.

Every decision the tool makes follows a rule you can trace in the report. The tool doesn't change anything it isn't sure about. Instead, it tells you what to do.

## Get the tool

Build `DitaParityAssistant.exe` from this repo (see [README.md](README.md)) or download a release. It's a single file — about 14 MB. You don't install anything. You can keep it on your desktop, a shared drive, or OneDrive.

The first time you run it, Windows might show **"Windows protected your PC."** Select **More info**, then **Run anyway**. After the first time, the warning won't appear again.

## When to use this tool

Use this tool when:

- The article has been updated since the conversion.
- The article has minor updates only.

Don't use this tool when:

- You're converting an article to DITA for the first time. The tool needs existing DITA to work from.
- The article has major updates. The tool will refuse most of the changes, and re-converting is faster.

## Before you start

Gather these files for the article you want to update:

- The DITA map (`.ditamap`).
- Every DITA topic (`.dita`) the map references.
- The Help Center URL for the updated article — or a saved copy of the article HTML.

## Run a migration

1. Double-click **DitaParityAssistant.exe**.
2. A small black window opens with the message *"Your browser should open automatically in a moment."* Leave this window open while you use the tool.
3. Your browser opens to the upload page. (If it doesn't, copy the URL the window shows — usually `http://localhost:8000/` — and paste it into your browser.)
4. Upload your DITA map.
5. Upload all DITA topic files.
6. Paste the Help Center URL for the updated article. (If you have an HTML file instead, leave the URL blank and upload the file.)
7. Select **Dry run** if you want to review the report before any `.dita` files are written. Recommended.
8. Select **Run migration**.

The page refreshes and shows your report when the tool finishes. Open the report in a new tab if you want to keep the upload form handy.

### After the report opens — what to do next

A panel at the top of the report tells you what to do.

- **Dry run** (yellow panel) — no `.dita` files were written. Review the report, then select **Run migration now** to apply the changes. The button writes the patched files into this same run's `outputs\` folder and reloads the report in its applied state. No need to re-upload anything.
  - The black assistant window must stay open while you click **Run migration now**. If you close it, the button shows a reminder to open the assistant.
- **Migration applied** (green panel) — the patched `.dita` files are in the run's `outputs\` folder. The full folder path is shown on the panel — click it to open. Copy the patched files into your DITA repo to replace the originals.
  - When you're ready for another article, select **Start a new migration** on the panel to go back to the upload form.

When you're done, close the small black window to stop the tool.

## Where your files go

The tool creates an `output\runs\<timestamp>\` folder right next to the `.exe`. Each run gets its own folder containing:

- `inputs\` — copies of everything you uploaded, kept safe.
- `outputs\` — the patched `.dita` files and the report.

Keep the originals you uploaded somewhere else too, just in case.

## Read your report

The report has four sections to review, in this order.

### 1. Summary

At the top, you see a tile for each kind of result:

| Tile | What it means |
|---|---|
| **Done** | Changes the tool made on its own. Skim to confirm. |
| **Needs your attention** | Changes the tool held back — either an existing DITA element it didn't trust enough to rewrite, or new article content it couldn't place safely. Review and apply by hand. |
| **Map update** | Changes that belong in the `.ditamap` (reltable), not in the topic files. |
| **Style/structure issues** | Schematron rules flagged something on the patched files. |

A high **Needs your attention** count isn't a failure. It means the tool found changes but wasn't confident enough to apply them on its own.

### 2. What the tool changed in each topic file

Each patched topic appears with a colored diff:

- **Green** is text the tool added.
- **Red, strikethrough** is text the tool removed.
- **Gray** is unchanged context for orientation.

Skim this first to get a feel for what changed. Open the patched `.dita` files only after the diff looks reasonable.

### 3. The change cards (Done / Needs your attention)

Each card explains one change in plain language. Look at:

- The header — for example, *"Reworded in Desktop"* or *"New content (needs your review) in Personalize invitations to connect."*
- **Currently in your DITA** vs **In the updated article** — the old text and the new text, side by side.
- The explanation below the diff — a short headline and a one-line action telling you what to do.
- The **Please verify** note (if it's there) — a warning that the tool made a guess you should confirm.

#### Warnings you'll see often

- **"Wrapped one or more bold/italic phrases from the article in `<em>`."** The tool used `<em>` as a safe default. Check whether `<uicontrol>` (UI element name), `<wintitle>` (window or page title), or `<keyword>` (technical term) is more accurate for each one, and update the tag if so.
- **"Kept the original UI element names, links, and keywords while rewording the surrounding text."** Confirm the formatting still wraps the right phrase.
- **"Dropped inline markup because the article wording no longer contains the wrapped phrase."** The original `<uicontrol>`/`<xref>` text isn't in the new sentence anymore. The tool applied the new text and added `<em>` for any article-side bold, but you may want to re-add `<uicontrol>` around UI element names.
- **"Updated `<note @type>` from the article's headline."** The article showed the callout as "Here's a tip" / "Important" / etc., so the tool changed the `@type` to match. Glance at the result to make sure the new type fits.

#### Reasons the tool holds something back

You'll see these in the **Needs your attention** tab:

- **The original text has special formatting.** The DITA wraps text in `<uicontrol>` or similar. The tool won't strip the formatting without you saying so.
- **This is a multi-paragraph callout.** The tool won't rewrite a `<note>` that has multiple paragraphs unless the article side gave it bullets too.
- **The tool won't delete a topic title.** If the article truly removed the topic, delete the file and update the `.ditamap` by hand.
- **"Entire topic appears removed from the live article."** ≥80% of a topic's content (or its title plus ≥50%) is missing. Delete the `.dita` file AND remove its `<topicref>` from the `.ditamap`.
- **This change crosses tab boundaries.** Content for one tab won't be inserted into a different tab's topic. The tool tells you which tab the new content actually belongs in.
- **This paragraph contains embedded media (`<image>`/`<object>`/`<fig>`).** The tool can't tell from the article whether the media should stay or go.
- **"Another sibling has identical link text but a different `<xref href>`."** Two source items look the same on the surface but link to different places. The tool can't pick a winner — review both and delete the one that doesn't apply.
- **"`<dlentry>` can't be auto-rewritten."** The dlentry has multiple `<dt>`/`<dd>` children, or the `<dd>` contains block structure (`<ul>`, `<note>`), or the term itself changed. Update manually.
- **"Alignment ambiguity — N source block(s) vs M article block(s)."** The article restructured this region enough that the tool can't safely pair rewrites. Look at the cards above and below for context.

#### Advisories you'll see in "Needs your attention"

- **"The live article has N screenshot(s) the tool did NOT add to the DITA."** The tool can't know the asset href in the catalog. Add each `<image>` element where the article shows the screenshot.
- **"The live article has an inline UI icon in N new or rewritten line(s)."** Same as above but for icon glyphs (`<li-icon>`, small SVGs). Place each `<image>` where the article shows the icon.
- **"N link(s) in '<topic>' may have outdated `<xref href>`s."** The DITA's href doesn't match the live article's link target. Common cause: the article-ID format changed from `/87951` to `/a1342713`. The tool never auto-rewrites hrefs — verify each one and update by hand.
- **"N reltable link(s) in the .ditamap may have outdated `<topicref href>`s."** Same idea, but for the `.ditamap`'s Related-tasks / Learn-more entries.
- **"N new FAQ-style question(s) in the live article have no matching .dita topic."** Create a new `.dita` topic for each AND add a `<topicref>` to the `.ditamap`.

If you want to see the technical details (xpath, element tag, op kind), select **Show technical details** at the top of the report.

### 4. Style and structure check

At the bottom of the report, you see any rule violations on the patched files. These usually flag things like missing short descriptions or notes that are too long. Address each one before you commit.

## Verify the changes

After every run, do these checks before you commit anything:

1. Open each patched `.dita` file and skim the **What the tool changed** view.
2. For each "Done" card with a **Please verify** note, confirm the markup choice.
3. For each "Needs your attention" card, decide whether to apply the change by hand.
4. Check the **Style and structure check** section for any new rule violations.

## What this tool now handles automatically

You may have seen older versions of this guide that called these out as manual. They're now in scope:

- **Definition lists (`<dlentry>`).** Simple dlentries (one `<dt>` + one text-only `<dd>`) get rewritten in place. The tool splits the article text on the first separator (`:` / `-` / `–` / `—`), keeps `<dt>` unchanged, and updates the `<dd>`. Complex dlentries (multiple terms, `<dd>` with nested lists) still surface for review.
- **Note type updates.** When the article shows a callout headline like "Here's a tip" or "Important", the tool updates `<note @type>` to match.
- **Post-tab content routing.** Content the article shows after the last tab panel routes to the post-tab `.dita` topic per the conversion convention.
- **Tab-aware change routing.** Each tab's content stays in its own `.dita` topic. The tool refuses cross-tab swaps.
- **Reltable + topic-body href staleness.** When the article's link target differs from the DITA's href, the tool surfaces it as a needs-review item.
- **New FAQ questions.** Surface as "new topic needed" advisories instead of disappearing into a generic paragraph insert.

## What this tool doesn't do yet

You'll need to update these by hand:

- Figures (`<fig>`)
- Code blocks (`<codeblock>`)
- Examples (`<example>`)
- New images or videos (the tool flags the position but you place the `<image>`).
- Articles in languages other than English
- Renaming a topic's `<title>` (when both `<dt>` and the title change, or when a topic title moved between topics).

The tool also doesn't work well when an article has been completely restructured. If you see lots of cards in **Needs your attention** and the diff view looks chaotic, you're probably better off re-converting from scratch.

## When something looks wrong

- **The tool missed a change you expected.** Check the **Needs your attention** tab first. The tool usually found the change but couldn't place it on its own. The new text is there for you to add by hand.
- **The tool changed something it shouldn't have.** Your original files are preserved in the run's `inputs\` folder. Copy them back. The patched files only live in `outputs\`.
- **A patched file looks empty.** This shouldn't happen with the current safety guards. If you see it, copy the original back from `inputs\` and file a bug report (see [Report a bug](#report-a-bug)).
- **The browser didn't open.** Look at the small black window. It shows the URL — usually `http://localhost:8000/`. Open a browser yourself and paste the URL in.
- **The black window says "Port 8000 is already in use."** Another copy of the tool is already running. Close the other black window first, then double-click the `.exe` again.

## Report a bug

If the tool does something unexpected, attach the run details to a bug report. The more you test and find bugs, the better the tool becomes.

### What to include

1. **A short description.** In a few sentences:
   - What were you trying to do?
   - What did the tool do that you didn't expect?
   - What did you expect instead?
2. **The full run folder, zipped.** Open the folder containing `DitaParityAssistant.exe`, navigate into `output\runs\`, find the folder whose name matches the timestamp of the broken run (for example, `20260610_182614_c01cd0\`), right-click it, then select **Send to > Compressed (zipped) folder**.
3. **The article URL**, if you used a URL instead of a saved HTML file.

### What's in the run folder

You're sending everything needed to reproduce the bug:

| File or folder | What it contains |
|---|---|
| `inputs\<your-files>` | The exact `.ditamap`, `.dita`, and `article_source.html` you uploaded. |
| `outputs\report.html` | The writer-friendly report you saw. |
| `outputs\patch_report.txt` | The engineer-readable report with full xpaths and reasons. This is the engineer-facing transcript. |
| `outputs\*.dita` | The patched topic files the tool produced. |

Nothing leaves your laptop unless you choose to send it.

### How to send it

- **Small zip (under 25 MB):** attach to email.
- **Larger zip:** drop it in a OneDrive folder and attach the link to the bug report.

## Tips

- **Use Dry run when you're not sure.** It produces the same report without writing any files. Review the report and select **Run migration now** on the yellow panel at the top when you're ready — no need to re-upload anything.
- **Run on one article at a time.** The tool processes one DITA map per run.
- **Find your past runs.** The home page lists previous runs. Select one to reopen its report. The patched files stay in `output\runs\<timestamp>\outputs\` next to the `.exe` for as long as you keep them.
- **Show technical details when you talk to engineering.** Toggle the checkbox at the top of the report to reveal xpaths and tag names. Engineers will ask for those.
- **The tool doesn't use AI.** Every decision follows a rule. If two runs on the same inputs disagree, that's a bug — please file an issue (see [Report a bug](#report-a-bug)).
