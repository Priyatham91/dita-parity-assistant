# Working with GitHub Copilot on this project

A handoff guide for continuing development with **GitHub Copilot in VS Code**
(specifically Copilot Chat's *Agent* mode). The codebase already has the
guard rails an AI agent needs to iterate safely — this doc points to them
and shows the prompts that work.

---

## 1 — Before you start

### Licensing
Check with a IT first — there's likely an enterprise Copilot
licence already. Don't pay for a personal subscription if the company
covers it. If you're unsure, ask your manager or the IT helpdesk.

### Install
1. Install [Visual Studio Code](https://code.visualstudio.com/).
2. In VS Code: **Extensions** → search **GitHub Copilot** → install.
3. Also install **GitHub Copilot Chat** (separate extension).
4. Sign in with your GitHub account when prompted.

### Open the project
```powershell
cd path/to/dita-parity-ai
code .
```

### Verify Agent mode is enabled
- Open the Copilot Chat panel (left sidebar, the chat icon).
- At the bottom of the chat, switch the mode dropdown from **Ask** to
  **Agent**. If you don't see Agent in the dropdown, update the Copilot
  Chat extension.
- Pick a model. **Claude Sonnet** is the strongest match for this Python
  codebase; **GPT-4** also works well.

---

## 2 — How to brief the agent

The shape of a good first prompt for this repo:

```
You're working on the DITA Parity Assistant. The codebase contract is
tests/test_regression_fixes.py — 126 unit tests that pin down every
beta-reported behaviour. Run `python -m unittest tests.test_regression_fixes`
after every change. If any test goes red, you've regressed.

Project layout (flattened 2026-06-29):
- app/                ← Python sources (parser, diff, patch, report)
- tests/              ← unittest suite
- schematron/         ← project Schematron rules
- WRITER_GUIDE.md     ← writer-facing docs (bundled into the exe)
- ARCHITECTURE.md     ← engineering reference
- MAINTENANCE.md      ← runbook for common tasks

Now [your task here].
```

Pinning the test file as the *contract* is the single biggest thing that
keeps an AI agent honest on this repo.

---

## 3 — Workflows that work well

### Adding a new safety guard (e.g. a beta-reported bug)

```
A beta tester reported [description / screenshot]. The bug is in
app/<module>.py around line <N>. The expected behaviour is <X>.

1. Read the relevant module and the test class for that module in
   tests/test_regression_fixes.py.
2. Write a failing test that pins down the bug.
3. Fix the code.
4. Run `python -m unittest tests.test_regression_fixes` and confirm
   only the new test goes green and nothing else regresses.
5. Show me the diff before committing.
```

### Refactoring or restructuring

Be more cautious here:
```
I want to refactor [X] in app/<module>.py. Before making changes:
1. Read the full module.
2. List every call site of the functions you'll touch (use grep).
3. Tell me your plan and the blast radius. Don't edit yet.
```
Then approve or push back on the plan.

### Updating the writer-facing UI / report

```
Change the report's [behaviour] in app/html_report.py.

After editing:
1. Run the tests.
2. Render a fresh report against `dist/output/runs/<run-id>/inputs/`
   to verify visually. The report must remain standalone (no external
   assets, no localhost URLs except the /run /guide /runs endpoints).
3. Show me the relevant snippet of the rendered HTML.
```

### Rebuilding the .exe

Don't rebuild the .exe in every iteration — it's slow (~30–60s). Only
rebuild when you're ready to test the bundled writer guide or to ship
to a writer.

```powershell
cd migration_assistant
pyinstaller DitaParityAssistant.spec --clean --noconfirm
```

The exe ends up at `migration_assistant/dist/DitaParityAssistant.exe`.

---

## 4 — Project-specific facts the agent should know

Drop these into your initial brief so the agent doesn't have to discover
them:

- **`tests/test_regression_fixes.py`** has 126 tests; each `class` is one
  beta-reported regression. New regressions get a new class.
- **`expand_blocks_for_diff`** in `app/article_html_parser.py` is where
  article-side blocks are merged into shapes the diff engine can match
  against the DITA source. Subtle — test before changing.
- **`_NOTE_KIND_TO_DITA_ATTRS`** in `app/patch_engine.py` is the canonical
  mapping from article-side note kinds to DITA `@type` / `@othertype`.
  Every code path that writes note attributes should go through it.
- **The HTML report is standalone.** No external CSS / JS / images, no
  fetch() except the assistant health check. If an agent suggests
  pulling in a CDN, push back.
- **`Beta test/` fixtures** are the real-world test bed. After
  behaviour changes, re-run those and diff `.dita` outputs against the
  saved `outputs/` folder. All 5 should stay byte-identical unless you
  intended a change.
- **The Schematron rules** in `migration_assistant/schematron/` are
  project-internal. Don't move, remove, or rewrite them without
  checking with the writers / IM team.
- **The dry-run / migrate flow** is wired through `_handle_migrate` in
  `app/server.py`. The "Run migration now" button in the report posts
  to `/runs/<run_id>/migrate` and the JS health check in
  `app/html_report.py` (`__ditaParityCheckServer`) gates it.

---

## 5 — Common pitfalls to head off

Add these to your brief if the agent slips:

- **"Don't run destructive git commands."** No `git push --force`,
  no `git reset --hard`, no `git clean -f`, no `rm -rf` outside `_tmp/`
  scratch dirs. Show diffs and let you commit.
- **"Don't auto-commit."** Even if the change is small. You review
  before staging.
- **"Don't skip hooks."** No `--no-verify`. If a pre-commit hook fails,
  fix the underlying issue.
- **"Don't change the WRITER_GUIDE bundling path."** It's resolved at
  runtime in `app/server.py` and PyInstaller. Easy to break silently.
- **"Don't add external dependencies"** without flagging them. The
  project currently runs on stdlib + lxml + python-pptx + python-docx
  + markdown. Each new dep is a PyInstaller bundling risk.

---

## 6 — Useful one-line prompts

For when you don't want to write a paragraph:

| Goal | Prompt |
|---|---|
| Get oriented in a module | "Read app/patch_engine.py and give me the 5-bullet summary of what each main function does." |
| Find a bug | "A writer reports [X]. Trace the code path from the diff engine to the report renderer and tell me where it goes wrong." |
| Add a test | "Add a regression test to tests/test_regression_fixes.py that pins down the behaviour `<X>`. Use the existing class style. Run the tests." |
| Verify a fix didn't regress | "Run the full test suite and the Beta test fixture comparison. Report any drift." |
| Update the writer guide | "Update WRITER_GUIDE.md to reflect [X]. Keep the tone consistent with the existing sections." |
| Update both decks | "Update tools/build_demo_deck.py and build_lt_deck.py to reflect [X], then rebuild both .pptx files." |

---

## 7 — When Copilot isn't the right tool

A few things Copilot in VS Code still struggles with on this repo:

- **Long browsing across files.** It can do it, but you'll burn through
  context. For "audit every place X is referenced," use VS Code's
  built-in search first, then hand the list to Copilot.
- **Rendering and visually verifying the HTML report.** Copilot can
  read the HTML it produces and reason about structure, but you should
  open the actual `report.html` in a browser before believing it
  "looks right."
- **Long-running interactive sessions.** Copilot's chat history doesn't
  always survive across days. For multi-day work, save your prompts
  and context separately (a plain markdown notes file works fine).

---

## 8 — Updating this doc

If you discover a new pitfall or a prompt that works particularly well,
add it here. Future-you (or any other engineer who picks this up) will
thank you.
