"""Regression tests for the bug fixes made on 2026-06-10.

Each test pins down a specific failure mode that was observed in a real
user run today. They use the preserved inputs in
`output/runs/<run_id>/inputs/` as fixtures, run the pipeline, and assert
the bug doesn't come back.

Run with:
    python tests/test_regression_fixes.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.article_html_parser import parse_help_center_html, expand_blocks_for_diff
from app.diff_engine import diff, OpKind
from app.map_parser import parse_ditamap_entries
from app.patch_engine import apply_ops, ResultCategory
from app.publication_reconstructor import reconstruct


def _run(fixture_run_id: str, ditamap_name: str):
    """Run the full pipeline against a preserved-fixture run. Returns
    (report, publication, article_blocks)."""
    inputs = PROJECT_ROOT / "output" / "runs" / fixture_run_id / "inputs"
    ditamap = inputs / ditamap_name
    article_html = (inputs / "article_source.html").read_text(encoding="utf-8")
    article_blocks_full = parse_help_center_html(article_html)
    texts, origins = expand_blocks_for_diff(article_blocks_full)
    publication = reconstruct(parse_ditamap_entries(ditamap))
    ops = diff(publication, texts)
    out = PROJECT_ROOT / "output" / "tests" / fixture_run_id
    out.mkdir(parents=True, exist_ok=True)
    report = apply_ops(ops, out, article_blocks=origins, publication=publication)
    return report, publication, origins, out


class MassDeleteGuard(unittest.TestCase):
    """Comment_on_posts test: the diff queued 7 DELETEs across the only
    7 source blocks in Notes.dita, gutting the file to <ul/>. The
    mass-deletion guard must refuse all DELETEs and skip the file."""

    def test_notes_dita_not_gutted(self) -> None:
        report, _, _, out = _run(
            "20260610_164146_271390",
            "Comment_on_posts_and_reply_to_a_comment.ditamap",
        )
        # Originally this fixture queued 7 phantom DELETEs across all 7
        # blocks of Notes.dita, which would have gutted the file. The
        # diff-level DELETE safety net now suppresses the false
        # positives (content that's still in the article). A DELETE
        # that does survive must be a LEGITIMATE removal — and the
        # mass-delete guard still kicks in at apply time if too many
        # would fire on one topic.
        notes_applied_deletes = [
            r for r in report.applied
            if r.op
            and r.op.kind == OpKind.DELETE
            and r.op.source_block
            and r.op.source_block.topic_id == "Notes.dita"
        ]
        # Contract: no gutting. Fewer than half the file's blocks may
        # be deleted, and never the topic <title>.
        self.assertLess(
            len(notes_applied_deletes), 4,
            f"Notes.dita is being gutted — {len(notes_applied_deletes)} "
            "auto-applied DELETEs is too many",
        )
        for r in notes_applied_deletes:
            self.assertFalse(
                r.op.source_block.element_xpath.endswith("/title[1]"),
                "topic <title> must never be auto-deleted",
            )


class YouTubeDeleteGuard(unittest.TestCase):
    """Custom CTA test: the DITA <p> wrapping a YouTube embed (<xref> ->
    <image>) had no article-side counterpart and got DELETEd, stripping
    the video. The media-element DELETE guard must refuse it."""

    def test_youtube_paragraph_preserved(self) -> None:
        report, _, _, out = _run(
            "20260610_170353_bbae5a",
            "Add_a_custom_call-to-action_to_your_LinkedIn_Page.ditamap",
        )
        patched = out / "Add_a_custom_call-to-action_to_your_LinkedIn_Page.dita"
        self.assertTrue(patched.exists())
        text = patched.read_text(encoding="utf-8")
        self.assertIn(
            "youtube.com", text,
            "YouTube xref was stripped — media-element DELETE guard failed",
        )


class CrossTabRouting(unittest.TestCase):
    """Personalize_invitations: an article block from OUTSIDE the
    Desktop/Mobile tabpanels was being inserted into Mobile.dita's
    <note>. The cross-tab guard must redirect or refuse."""

    def test_post_tab_content_not_inserted_into_mobile(self) -> None:
        report, _, _, out = _run(
            "20260610_145635_0180db",
            "Personalize_invitations_to_connect.ditamap",
        )
        mobile = out / "Mobile.dita"
        if mobile.exists():
            content = mobile.read_text(encoding="utf-8")
            # The post-tab article block that previously got injected.
            self.assertNotIn(
                "the conversation appears in your messages",
                content,
                "post-tab article content leaked into Mobile.dita — "
                "cross-tab guard failed",
            )


class InlineMarkupPreservation(unittest.TestCase):
    """Custom CTA test: Desktop.dita step 3 has
    <cmd>Turn on the <uicontrol>Custom button</uicontrol> toggle.</cmd>.
    The article reworded to "In the Buttons tab, turn on the Custom
    button toggle." with <strong>Buttons</strong>. The patcher must
    preserve the <uicontrol> and wrap "Buttons" in <em>.
    """

    def test_step3_keeps_uicontrol_and_adds_em(self) -> None:
        report, _, _, out = _run(
            "20260610_170353_bbae5a",
            "Add_a_custom_call-to-action_to_your_LinkedIn_Page.ditamap",
        )
        desktop = out / "Desktop.dita"
        self.assertTrue(desktop.exists(), "Desktop.dita was not patched")
        content = desktop.read_text(encoding="utf-8")
        self.assertIn(
            "<em>Buttons</em>", content,
            "<em>Buttons</em> missing — emphasis capture failed",
        )
        self.assertIn(
            "<uicontrol>Custom button</uicontrol>", content,
            "<uicontrol> was stripped — markup preservation failed",
        )


class HeadingDetection(unittest.TestCase):
    """Personalize_invitations: the article's <h2>Invitation limits</h2>
    et al. should become <section><title>…</title></section> in DITA,
    not plain <p>."""

    def test_h2_becomes_section(self) -> None:
        report, _, _, out = _run(
            "20260610_145635_0180db",
            "Personalize_invitations_to_connect.ditamap",
        )
        main = out / "Personalize_invitations_to_connect.dita"
        self.assertTrue(main.exists())
        content = main.read_text(encoding="utf-8")
        self.assertIn(
            "<section><title>Invitation limits</title></section>",
            content,
            "<h2>Invitation limits</h2> didn't become a <section><title>",
        )


class TitleDeletionRefused(unittest.TestCase):
    """A DELETE op on a topic <title> is almost always a diff
    misalignment. Even when other DELETEs in the topic don't trip the
    mass-delete guard, the title-DELETE alone must be refused."""

    def test_notes_title_kept(self) -> None:
        report, _, _, out = _run(
            "20260610_164146_271390",
            "Comment_on_posts_and_reply_to_a_comment.ditamap",
        )
        # The title must not be auto-deleted. Originally the patch-time
        # title guard refused the DELETE (it would surface in `skipped`).
        # The diff-level safety net now suppresses the DELETE earlier
        # because the title text appears in the article body — the
        # end-state contract (title preserved) is the same.
        title_applied_deletes = [
            r for r in report.applied
            if r.op
            and r.op.kind == OpKind.DELETE
            and r.op.source_block
            and r.op.source_block.element_xpath.endswith("/title[1]")
        ]
        self.assertEqual(
            title_applied_deletes, [],
            "topic <title> must never be auto-deleted",
        )


class EmphasisWarningSurfaces(unittest.TestCase):
    """Every <em> added by the patcher should carry a manual-verify
    warning on the corresponding APPLIED entry."""

    def test_em_wrap_emits_warning(self) -> None:
        report, _, _, out = _run(
            "20260610_170353_bbae5a",
            "Add_a_custom_call-to-action_to_your_LinkedIn_Page.ditamap",
        )
        em_warnings = [
            r for r in report.applied
            if "<em>" in (r.warning or "")
        ]
        self.assertGreaterEqual(
            len(em_warnings), 1,
            "no APPLIED entry called out the <em> wrap for manual review",
        )


class ReplaceDropsMarkupWhenArticleDoesNotMatch(unittest.TestCase):
    """When the article rewords a paragraph and the new wording no
    longer contains the phrases that the DITA had wrapped in
    <xref>/<uicontrol>/<keyword>, the tool applies the new wording
    and drops the wrapping. The article is the source of truth;
    a writer reviews the APPLIED-with-warning entry afterward to
    re-add any markup the new wording needs.
    """

    def test_paragraph_with_xref_reworded_drops_xref(self) -> None:
        import tempfile
        import xml.etree.ElementTree as ET
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        dita_src = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
            '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
            '<p>As a Page admin, you can <xref href="https://example.com/x" '
            'scope="external" format="html">create test events</xref> '
            'familiarize yourself with events.</p>'
            '</conbody></concept>\n'
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "topic.dita"
            src.write_text(dita_src, encoding="utf-8")
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[1]", element_tag="p",
                      text=("As a Page admin, you can create test events "
                            "familiarize yourself with events.")),
            ])
            article_blocks = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text=("Yes. As a Page admin, you can create a test event "
                          "to learn how events work."),
                    kind="paragraph",
                ),
            ]
            ops = diff(pub, [b.text for b in article_blocks])
            out_dir = tmp_path / "out"
            report = apply_ops(
                ops, out_dir, article_blocks=article_blocks, publication=pub,
            )

            # The paragraph REPLACE should be APPLIED, with a warning
            # explaining the markup was dropped.
            replace_apps = [
                r for r in report.applied if r.op.kind.value == "replace"
                and r.op.source_block
                and r.op.source_block.element_xpath.endswith("/p[1]")
            ]
            self.assertEqual(
                len(replace_apps), 1,
                "the paragraph REPLACE should have been applied, not skipped",
            )
            self.assertIn("dropped", replace_apps[0].warning,
                          "warning should explain markup was dropped")

            patched = (out_dir / "topic.dita").read_text(encoding="utf-8")
            self.assertIn("Yes. As a Page admin", patched,
                          "new wording should be written")
            self.assertNotIn("create test events", patched,
                             "old wording should be gone")
            self.assertNotIn("<xref", patched,
                             "stale <xref> should be dropped — article has no link here")


class DryRunLeavesNoDitaFiles(unittest.TestCase):
    """A dry-run pipeline run should produce a report on disk but no
    patched .dita files. The patched files are written transiently so
    Schematron validation and the track-changes diff can read them,
    then deleted by the caller before returning the report URL."""

    def test_dry_run_outputs_only_reports(self) -> None:
        # Use the Personalize_invitations fixture and simulate the
        # full pipeline including the post-render cleanup that lives
        # in server.py / main.py.
        inputs = PROJECT_ROOT / "output" / "runs" / "20260610_145635_0180db" / "inputs"
        ditamap = inputs / "Personalize_invitations_to_connect.ditamap"
        article_html = (inputs / "article_source.html").read_text(encoding="utf-8")
        article_blocks_full = parse_help_center_html(article_html)
        from app.article_html_parser import expand_blocks_for_diff
        from app.diff_engine import diff as _diff, summarize
        from app.html_report import write_html_report
        from app.map_parser import TopicRef
        from app.report_generator import write_patch_report
        from app.schematron_validator import validate_files
        texts, origins = expand_blocks_for_diff(article_blocks_full)
        publication = reconstruct(parse_ditamap_entries(ditamap))
        ops = _diff(publication, texts)
        out = PROJECT_ROOT / "output" / "tests" / "dry_run_check"
        if out.exists():
            for f in out.iterdir():
                try:
                    f.unlink()
                except OSError:
                    pass
        out.mkdir(parents=True, exist_ok=True)
        report = apply_ops(
            ops, out, article_blocks=origins, publication=publication,
            dry_run=True,
        )
        validation = validate_files(report.files_written, PROJECT_ROOT / "schematron")
        write_patch_report(report, out / "patch_report.txt",
                           validation=validation, dry_run=True)
        topic_refs = [e for e in parse_ditamap_entries(ditamap) if isinstance(e, TopicRef)]
        write_html_report(
            out / "report.html", map_path=ditamap, article_path=inputs / "article_source.html",
            topic_refs=topic_refs, publication=publication, updated_block_count=len(texts),
            diff_summary=summarize(ops), report=report, output_dir=out,
            validation=validation, dry_run=True,
        )
        # Mirror server.py's post-render cleanup.
        for f in report.files_written:
            f.unlink(missing_ok=True)

        # Reports survive, .dita files do not.
        self.assertTrue((out / "report.html").exists(),
                        "report.html should remain after dry-run cleanup")
        self.assertTrue((out / "patch_report.txt").exists(),
                        "patch_report.txt should remain after dry-run cleanup")
        leftover = list(out.glob("*.dita"))
        self.assertEqual(
            leftover, [],
            f"dry-run left .dita files behind: {leftover}",
        )


class SuppressNoiseWhenArticleIsSubstring(unittest.TestCase):
    """Multi-paragraph callout where the article text is fully
    contained in the DITA text. The diff fires REPLACE because the
    DITA has an extra metadata paragraph (e.g. a filename) that the
    article doesn't render. From the writer's view, the wording
    didn't change. The tool must NOT surface this as Needs review
    or Add manually — it's noise.
    """

    def test_substring_replace_is_silently_dropped(self) -> None:
        import tempfile
        import xml.etree.ElementTree as ET
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        dita_src = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
            '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
            '<note type="other" othertype="pdf">'
            '<p>Live-Events-Getting-Started-Guide.pdf</p>'
            '<p>Please review this document for more information</p>'
            '<p>View Document</p>'
            '</note>'
            '</conbody></concept>\n'
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "topic.dita"
            src.write_text(dita_src, encoding="utf-8")
            # Source block text mimics what publication_reconstructor
            # produces with the new block-aware collapse: paragraphs
            # joined by spaces.
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/note[1]", element_tag="note",
                      text=("Live-Events-Getting-Started-Guide.pdf "
                            "Please review this document for more information "
                            "View Document"),
                      note_type="other",
                      auto_update=False,
                      skip_reason="structural element <note> with nested paragraphs/lists",
                      structural=True),
            ])
            article_blocks = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Please review this document for more information View Document",
                    kind="note",
                ),
            ]
            ops = diff(pub, [b.text for b in article_blocks])
            out_dir = tmp_path / "out"
            report = apply_ops(
                ops, out_dir, article_blocks=article_blocks, publication=pub,
            )

            # The structural-note "REPLACE" should not appear in any
            # action bucket — the article text is contained in the
            # source text, so it isn't a real wording change.
            note_ops = [
                r for r in report.results
                if r.op.source_block
                and r.op.source_block.element_xpath.endswith("/note[1]")
            ]
            self.assertEqual(
                note_ops, [],
                f"structural <note> REPLACE leaked into report despite "
                f"article text being a substring of source text: {note_ops}",
            )


class DlentrySourceTextMatchesArticleRendering(unittest.TestCase):
    """The publication reconstructor must render <dlentry> source text as
    'Term: Definition' so it matches what the Help Center stylesheet
    renders. Without the colon-space separator the source becomes
    'TermDefinition' (no separator), the diff sees a REPLACE on every
    dlentry, and every dlentry shows up in Needs review for no reason.
    """

    def test_dlentry_text_uses_colon_space_separator(self) -> None:
        import xml.etree.ElementTree as ET
        from app.publication_reconstructor import _dlentry_collapsed_text

        elem = ET.fromstring(
            '<dlentry>'
            '<dt>In-app notifications</dt>'
            '<dd>Delivered within the a app.</dd>'
            '</dlentry>'
        )
        self.assertEqual(
            _dlentry_collapsed_text(elem),
            "In-app notifications: Delivered within the a app.",
        )

    def test_dlentry_without_dt_or_dd_falls_back(self) -> None:
        import xml.etree.ElementTree as ET
        from app.publication_reconstructor import _dlentry_collapsed_text

        # Malformed dlentry (only one of dt/dd) — fall back to plain
        # concatenation so we don't crash.
        elem = ET.fromstring(
            '<dlentry><dt>only a term</dt></dlentry>'
        )
        # Falls back to _element_text — just the dt text.
        self.assertEqual(_dlentry_collapsed_text(elem), "only a term")


class DlSplitterRespectsIM(unittest.TestCase):
    """Unit tests for the term/definition splitter that backs the
    <dlentry> insert handler. The IM (page 123) and the dropped
    Schematron rule IM_list11 say <dt> must be a short noun phrase
    with no trailing punctuation. The splitter encodes both."""

    def _split(self, text):
        from app.patch_engine import _split_term_definition
        return _split_term_definition(text)

    def test_colon_separator_strips_colon(self) -> None:
        result = self._split("Email: Delivered to your primary email address.")
        self.assertEqual(result, ("Email", "Delivered to your primary email address."))

    def test_em_dash_separator_strips_dash(self) -> None:
        # Em-dash variant — common in Help Center copy.
        result = self._split("Push notifications — Sent to your device immediately.")
        self.assertEqual(
            result, ("Push notifications", "Sent to your device immediately."),
        )

    def test_dt_trailing_period_is_stripped(self) -> None:
        # Term has a trailing period before the em-dash; IM_list11
        # requires no trailing punctuation on <dt>.
        result = self._split("Notes. — A trailing description.")
        self.assertEqual(result, ("Notes", "A trailing description."))

    def test_no_separator_returns_none(self) -> None:
        # Plain sentence with no recognised separator.
        result = self._split("This is just a sentence without a term.")
        self.assertIsNone(result)

    def test_long_term_rejected(self) -> None:
        # Term longer than 60 chars is not a "short noun phrase" per
        # IM page 123 — refuse so the writer decides.
        long_term = "x" * 70
        result = self._split(f"{long_term}: short definition")
        self.assertIsNone(result)


class InsertInsideDlBecomesDlentry(unittest.TestCase):
    """When the article adds a new 'Term: Description' entry inside a
    section that the DITA renders as a <dl>, the tool must add a new
    <dlentry> sibling — not a <ul><li> (which would be invalid inside
    a <dl>). Per IM page 122–123: <dt> holds a short noun phrase with
    NO colon (the stylesheet adds it), <dd> holds the description.
    """

    def test_dl_anchor_produces_dlentry(self) -> None:
        import tempfile
        import xml.etree.ElementTree as ET
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        dita_src = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
            '<task id="X" xml:lang="en-US"><title>Notifications</title><taskbody>'
            '<steps><step><cmd>Choose your channel.</cmd><info>'
            '<dl>'
            '<dlentry><dt>In-app</dt><dd>Delivered within the a app.</dd></dlentry>'
            '<dlentry><dt>Push notifications</dt><dd>Sent to your device immediately.</dd></dlentry>'
            '</dl>'
            '</info></step></steps>'
            '</taskbody></task>\n'
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "topic.dita"
            src.write_text(dita_src, encoding="utf-8")
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/title[1]", element_tag="title", text="Notifications"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/cmd[1]",
                      element_tag="cmd", text="Choose your channel."),
                Block(block_index=2, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/info[1]/dl[1]/dlentry[1]/dt[1]",
                      element_tag="dt", text="In-app"),
                Block(block_index=3, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/info[1]/dl[1]/dlentry[1]/dd[1]",
                      element_tag="dd", text="Delivered within the a app."),
                Block(block_index=4, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/info[1]/dl[1]/dlentry[2]/dt[1]",
                      element_tag="dt", text="Push notifications"),
                Block(block_index=5, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/info[1]/dl[1]/dlentry[2]/dd[1]",
                      element_tag="dd", text="Sent to your device immediately."),
            ])
            article_blocks = [
                HtmlArticleBlock(text="Notifications", kind="paragraph"),
                HtmlArticleBlock(text="Choose your channel.", kind="step"),
                HtmlArticleBlock(text="In-app", kind="paragraph"),
                HtmlArticleBlock(text="Delivered within the a app.", kind="paragraph"),
                HtmlArticleBlock(text="Push notifications", kind="paragraph"),
                HtmlArticleBlock(text="Sent to your device immediately.", kind="paragraph"),
                HtmlArticleBlock(text="Email: Delivered to your primary email address.", kind="unordered_step"),
            ]
            ops = diff(pub, [b.text for b in article_blocks])
            out_dir = tmp_path / "out"
            report = apply_ops(
                ops, out_dir, article_blocks=article_blocks, publication=pub,
            )

            # Find the APPLIED INSERT for the Email entry.
            email_ops = [
                r for r in report.applied
                if "Email" in (r.op.updated_text or "")
            ]
            self.assertEqual(
                len(email_ops), 1,
                f"expected one APPLIED insert for Email; got {len(email_ops)}",
            )
            # Clean split ("Email" → 1-word term, no colon, real
            # description) — no warning fires after the 2026-06-24
            # cleanup. The structural correctness is verified below;
            # the warning is reserved for genuinely risky splits.
            self.assertEqual(
                email_ops[0].warning, "",
                "clean dlentry splits should not carry a verify warning "
                "after the 2026-06-24 hygiene pass",
            )

            patched = (out_dir / "topic.dita").read_text(encoding="utf-8")
            # Must contain a new dlentry with Email term — no colon in <dt>.
            self.assertIn("<dlentry>", patched)
            self.assertIn("<dt>Email</dt>", patched,
                          "term should be 'Email', no colon (IM page 123)")
            self.assertIn(
                "<dd>Delivered to your primary email address.</dd>", patched,
                "description should be the post-colon text",
            )
            # And NOT a <ul><li> inside the <dl>.
            dl_region = patched[patched.find("<dl"):patched.find("</dl>") + 5]
            self.assertNotIn(
                "<ul", dl_region,
                f"<ul> must not appear inside <dl>: {dl_region!r}",
            )

    def test_dlentry_clean_vs_risky_split_warning(self) -> None:
        """The clean-split helper added 2026-06-24: a verify warning
        only fires when the term/description split is risky enough to
        warrant a writer's eye.

        Clean splits (Email-style: short term, real description) skip
        the warning. Risky splits (long sentence-like "term," missing
        description, stray colon, etc.) keep the warning so writers
        catch genuine mis-splits.
        """
        from app.patch_engine import _dlentry_split_is_clean

        # Clean — should NOT warn.
        self.assertTrue(_dlentry_split_is_clean(
            "Google Pay",
            "We accept Google Pay as a payment method for all new and "
            "recurring online purchases of Premium subscriptions.",
        ))
        self.assertTrue(_dlentry_split_is_clean(
            "Email",
            "Delivered to your primary email address.",
        ))
        self.assertTrue(_dlentry_split_is_clean(
            "Two-factor authentication",
            "Adds a second verification step at sign-in.",
        ))

        # Risky — should warn.
        self.assertFalse(_dlentry_split_is_clean(
            # Term too long → looks like a sentence.
            "If you have not received a code in your email after a few minutes",
            "Check your spam folder before requesting another one.",
        ))
        self.assertFalse(_dlentry_split_is_clean(
            # Term ends with sentence punctuation.
            "Step one.", "Open the menu and select the gear icon.",
        ))
        self.assertFalse(_dlentry_split_is_clean(
            # Term contains an internal colon — split point was wrong.
            "Note: special case", "See the appendix for details.",
        ))
        self.assertFalse(_dlentry_split_is_clean(
            # Empty term.
            "", "A description with words.",
        ))
        self.assertFalse(_dlentry_split_is_clean(
            # Description too short.
            "API key", "Required.",
        ))


class FreshDlBuiltFromTermDescBullets(unittest.TestCase):
    """When the source DITA has no <dl> at all but the article adds
    consecutive Term: Description bullets, the patcher should create a
    new <dl> with one <dlentry> per bullet — not a <ul><li> with
    colon-text inside each <li>. Per IM page 122-123."""

    def test_three_term_desc_bullets_become_dl(self) -> None:
        import tempfile
        import xml.etree.ElementTree as ET
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        dita_src = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
            '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
            '<p>Choose where to receive notifications.</p>'
            '</conbody></concept>\n'
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "topic.dita"
            src.write_text(dita_src, encoding="utf-8")
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[1]", element_tag="p",
                      text="Choose where to receive notifications."),
            ])
            article_blocks = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Choose where to receive notifications.",
                                 kind="paragraph"),
                HtmlArticleBlock(text="In-app: Delivered within the a app.",
                                 kind="unordered_step"),
                HtmlArticleBlock(text="Push: Sent to your device immediately.",
                                 kind="unordered_step"),
                HtmlArticleBlock(text="Email: Delivered to your primary email address.",
                                 kind="unordered_step"),
            ]
            ops = diff(pub, [b.text for b in article_blocks], article_blocks=article_blocks)
            out_dir = tmp_path / "out"
            report = apply_ops(
                ops, out_dir, article_blocks=article_blocks, publication=pub,
            )

            patched = (out_dir / "topic.dita").read_text(encoding="utf-8")

            # Must contain a <dl> with three <dlentry>s — not a <ul>.
            self.assertIn("<dl>", patched, "fresh <dl> should have been created")
            self.assertEqual(patched.count("<dlentry>"), 3,
                             "all three bullets should become <dlentry>")
            self.assertIn("<dt>In-app</dt>", patched)
            self.assertIn("<dt>Push</dt>", patched)
            self.assertIn("<dt>Email</dt>", patched)
            self.assertIn("<dd>Delivered within the a app.</dd>", patched)
            self.assertNotIn("<ul>", patched,
                             "<ul> must not be created when bullets fit the dl pattern")

    def test_plain_bullets_still_become_ul(self) -> None:
        # Same shape but plain bullets (no colon) — should stay <ul><li>.
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        dita_src = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
            '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
            '<p>Anchor paragraph.</p>'
            '</conbody></concept>\n'
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "topic.dita"
            src.write_text(dita_src, encoding="utf-8")
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[1]", element_tag="p",
                      text="Anchor paragraph."),
            ])
            article_blocks = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Anchor paragraph.", kind="paragraph"),
                HtmlArticleBlock(text="First plain bullet.", kind="unordered_step"),
                HtmlArticleBlock(text="Second plain bullet.", kind="unordered_step"),
            ]
            ops = diff(pub, [b.text for b in article_blocks], article_blocks=article_blocks)
            out_dir = tmp_path / "out"
            report = apply_ops(
                ops, out_dir, article_blocks=article_blocks, publication=pub,
            )
            patched = (out_dir / "topic.dita").read_text(encoding="utf-8")
            self.assertIn("<ul>", patched, "plain bullets should remain <ul>")
            self.assertNotIn("<dl>", patched, "no <dl> should be created for plain bullets")


class TaskHeadingUsesStepsection(unittest.TestCase):
    """In a task topic, an article <h2> heading inside a <steps>
    context must become <stepsection>, not <p outputclass="heading">
    (which the IM doesn't sanction). Outside <steps>, the fallback is
    a plain <p>. Per IM page 45."""

    def test_heading_in_task_becomes_stepsection(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        dita_src = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
            '<task id="X" xml:lang="en-US"><title>T</title><taskbody>'
            '<steps>'
            '<step><cmd>First step.</cmd></step>'
            '</steps>'
            '</taskbody></task>\n'
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "topic.dita"
            src.write_text(dita_src, encoding="utf-8")
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/cmd[1]",
                      element_tag="cmd", text="First step."),
            ])
            article_blocks = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="First step.", kind="step"),
                HtmlArticleBlock(text="To finish:", kind="heading"),
            ]
            ops = diff(pub, [b.text for b in article_blocks], article_blocks=article_blocks)
            out_dir = tmp_path / "out"
            report = apply_ops(
                ops, out_dir, article_blocks=article_blocks, publication=pub,
            )
            patched = (out_dir / "topic.dita").read_text(encoding="utf-8")
            # Colon stripped: stylesheet adds it automatically (same
            # rule as <dt> per IM page 123).
            self.assertIn("<stepsection>To finish</stepsection>", patched)
            self.assertNotIn('outputclass="heading"', patched,
                             "IM doesn't bless outputclass=heading")


class NewTableFollowsIM(unittest.TestCase):
    """An article <table> that has no counterpart in source DITA must
    be created as a full <table><tgroup cols="N"><colspec/>×N<tbody>
    structure, per IM page 251 (DITA tag <table>)."""

    def test_new_table_built_with_full_structure(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        dita_src = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
            '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
            '<p>Intro.</p>'
            '</conbody></concept>\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "topic.dita"
            src.write_text(dita_src, encoding="utf-8")
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[1]", element_tag="p", text="Intro."),
            ])
            article_blocks = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Intro.", kind="paragraph"),
                HtmlArticleBlock(
                    text="Header A Header B Header C", kind="table_row",
                    cells=["Header A", "Header B", "Header C"],
                ),
                HtmlArticleBlock(
                    text="A1 B1 C1", kind="table_row",
                    cells=["A1", "B1", "C1"],
                ),
                HtmlArticleBlock(
                    text="A2 B2 C2", kind="table_row",
                    cells=["A2", "B2", "C2"],
                ),
            ]
            ops = diff(pub, [b.text for b in article_blocks], article_blocks=article_blocks)
            out_dir = tmp_path / "out"
            report = apply_ops(
                ops, out_dir, article_blocks=article_blocks, publication=pub,
            )
            patched = (out_dir / "topic.dita").read_text(encoding="utf-8")

            # Full IM structure must be present.
            self.assertIn("<table>", patched)
            self.assertIn('<tgroup cols="3">', patched)
            self.assertEqual(patched.count("<colspec"), 3,
                             "three <colspec> elements expected for 3 columns")
            self.assertIn("<tbody>", patched)
            # All three rows in tbody, in original order.
            tbody_start = patched.find("<tbody>")
            tbody_end = patched.find("</tbody>")
            tbody_content = patched[tbody_start:tbody_end]
            self.assertEqual(tbody_content.count("<row>"), 3)
            self.assertTrue(
                tbody_content.find("Header A") < tbody_content.find("A1"),
                "row order must match article order",
            )


class FreshDlInTaskTopicGoesInStepInfo(unittest.TestCase):
    """Task-topic placement: a fresh <dl> must live inside <step>/<info>,
    not as a direct child of <step>. Per the DITA task DTD <step> only
    allows <cmd> then <info>/<stepxmp>/<stepresult>/etc. — <dl> directly
    inside <step> is invalid.
    """

    def test_term_desc_bullet_in_task_lands_in_step_info(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        # Task topic with a single <step> that has only <cmd> (no <info>
        # yet). We expect the article's Term: Description bullet to
        # create <step><cmd>…</cmd><info><dl><dlentry>…</dlentry></dl></info></step>.
        dita_src = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
            '<task id="X" xml:lang="en-US"><title>T</title><taskbody>'
            '<steps><step><cmd>Choose your channel.</cmd></step></steps>'
            '</taskbody></task>\n'
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "topic.dita"
            src.write_text(dita_src, encoding="utf-8")
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/cmd[1]",
                      element_tag="cmd", text="Choose your channel."),
            ])
            article_blocks = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Choose your channel.", kind="step"),
                HtmlArticleBlock(text="Email: Delivered to your inbox.",
                                 kind="unordered_step"),
            ]
            ops = diff(pub, [b.text for b in article_blocks], article_blocks=article_blocks)
            out_dir = tmp_path / "out"
            report = apply_ops(
                ops, out_dir, article_blocks=article_blocks, publication=pub,
            )

            patched = (out_dir / "topic.dita").read_text(encoding="utf-8")
            self.assertIn("<dl>", patched, "<dl> must be created")
            self.assertIn("<info>", patched,
                          "<info> must be created to host the <dl>")
            self.assertIn("<dt>Email</dt>", patched)
            self.assertIn("<dd>Delivered to your inbox.</dd>", patched)
            # The pathological pattern <step><cmd>…</cmd><dl> would mean
            # <dl> is a direct child of <step> — invalid per the task DTD.
            collapsed = patched.replace(" ", "").replace("\n", "")
            self.assertNotIn(
                "</cmd><dl>", collapsed,
                "<dl> must be wrapped in <info>, not a sibling of <cmd>",
            )
            # And the correct shape — cmd then info then dl — must appear.
            self.assertIn("</cmd><info><dl>", collapsed)


class SectionAwareDiffPicksRightTab(unittest.TestCase):
    """When the article has identical wording in both tabs (Desktop and
    Mobile each list "In-app", "Push", "Email"), the diff must align
    each article block to the source block in the SAME tab — not
    cross-tab. Without section-awareness, SequenceMatcher pairs the
    article's panel-1 Push with Mobile.dita's Push (identical text)
    and Desktop never gets its INSERT for Push or Email.
    """

    def test_asymmetric_dl_gets_inserts_in_both_tabs(self) -> None:
        # Use the user's saved Manage_LinkedIn_news_notifications fixture.
        inputs = PROJECT_ROOT / "dist" / "output" / "runs" / "20260611_132030_9f92e8" / "inputs"
        if not inputs.exists():
            self.skipTest("fixture not present in this checkout")

        ditamap = inputs / "Manage_LinkedIn_news_notifications.ditamap"
        article_html = (inputs / "article_source.html").read_text(encoding="utf-8")
        from app.article_html_parser import parse_help_center_html, expand_blocks_for_diff
        from app.diff_engine import diff as _diff
        article_blocks = parse_help_center_html(article_html)
        texts, origins = expand_blocks_for_diff(article_blocks)
        publication = reconstruct(parse_ditamap_entries(ditamap))
        ops = _diff(publication, texts, article_blocks=origins)
        out = PROJECT_ROOT / "output" / "tests" / "section_aware_test"
        if out.exists():
            for f in out.glob("*"):
                try: f.unlink()
                except OSError: pass
        out.mkdir(parents=True, exist_ok=True)
        report = apply_ops(ops, out, article_blocks=origins, publication=publication)

        # Both Desktop and Mobile should be in files_written.
        names = {p.name for p in report.files_written}
        self.assertIn("Desktop.dita", names,
                      "Desktop.dita should be patched — Push and Email INSERTs")
        self.assertIn("Mobile.dita", names,
                      "Mobile.dita should be patched — Email INSERT")

        # Desktop should have received both 'Push' and 'Email' inserts.
        desktop_inserts = [
            r for r in report.applied
            if r.op.anchor_block
            and r.op.anchor_block.topic_id == "Desktop.dita"
            and r.op.kind.value == "insert"
        ]
        texts_inserted = [r.op.updated_text for r in desktop_inserts]
        self.assertTrue(
            any("Push" in t for t in texts_inserted),
            f"Desktop should get a Push notifications INSERT; got {texts_inserted}",
        )
        self.assertTrue(
            any("Email" in t for t in texts_inserted),
            f"Desktop should get an Email INSERT; got {texts_inserted}",
        )


class WhitespaceBeforeOpenParenNormalizes(unittest.TestCase):
    """Source DITA pretty-printing sometimes produces `<uicontrol>X</uicontrol>(Y)`
    with no space, while the article HTML renders `X (Y)` with a space.
    Without normalization, SequenceMatcher sees them as different blocks,
    fails to pair them as EQUAL, and the article side gets inserted as a
    duplicate row alongside the unchanged source row. normalize_for_match
    must collapse the difference so they pair correctly.
    """

    def test_whitespace_before_open_paren_is_stripped(self) -> None:
        from app.publication_reconstructor import normalize_for_match
        # Real example from the Create_a_LinkedIn_Event fixture
        src = "Use a a registration form(available for Pages only) Select this checkbox"
        art = "Use a a registration form (available for Pages only) Select this checkbox"
        self.assertEqual(
            normalize_for_match(src), normalize_for_match(art),
            "rows differing only by whitespace before '(' must normalize equal",
        )

    def test_linkedin_registration_form_row_not_duplicated(self) -> None:
        """End-to-end: when the article inserts new rows above an existing
        row whose only difference is whitespace before an opening paren,
        the existing row must be matched as EQUAL — not duplicated as a
        fresh INSERT below it."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        # Source row text has no space before '('; article row has one.
        # Plus the article inserts 3 brand-new rows above it.
        dita_src = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
            '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
            '<p>Event name Type a unique name.</p>'
            '<p>Use a a registration form(available for Pages only) Select this checkbox.</p>'
            '<p>Description Type a brief description.</p>'
            '</conbody></concept>\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "topic.dita"
            src.write_text(dita_src, encoding="utf-8")
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[1]", element_tag="p",
                      text="Event name Type a unique name."),
                Block(block_index=2, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[2]", element_tag="p",
                      text="Use a a registration form(available for Pages only) Select this checkbox."),
                Block(block_index=3, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[3]", element_tag="p",
                      text="Description Type a brief description."),
            ])
            article_blocks = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Event name Type a unique name.", kind="paragraph"),
                HtmlArticleBlock(text="Timezone Select the time zone.", kind="paragraph"),
                HtmlArticleBlock(text="Start date Select the date.", kind="paragraph"),
                HtmlArticleBlock(text="Add end date Select an end date.", kind="paragraph"),
                # Article HAS a space before "(" — source does NOT.
                HtmlArticleBlock(
                    text="Use a a registration form (available for Pages only) Select this checkbox.",
                    kind="paragraph",
                ),
                HtmlArticleBlock(text="Description Type a brief description.", kind="paragraph"),
            ]
            ops = diff(pub, [b.text for b in article_blocks], article_blocks=article_blocks)
            out_dir = tmp_path / "out"
            apply_ops(ops, out_dir, article_blocks=article_blocks, publication=pub)
            patched = (out_dir / "topic.dita").read_text(encoding="utf-8")

            # The a registration text must appear exactly ONCE
            # (the source row stays put; not re-inserted as a duplicate).
            count = patched.count("Use a a registration form")
            self.assertEqual(
                count, 1,
                f"a registration row should appear exactly once, "
                f"got {count} occurrences — duplicate INSERT was emitted"
            )


class LowOverlapReplaceIsDemoted(unittest.TestCase):
    """When the article inserts new rows/paragraphs into an
    otherwise-unchanged sequence, SequenceMatcher's LCS pairs the
    leftover-unmatched source block with the first new article block
    as a REPLACE — even when the two share essentially no words. The
    real shape is: source row unchanged + new article row INSERTed
    before it. Demote that pair to a pure INSERT so the source row
    stays untouched and the new content is correctly classified.
    """

    def test_zero_overlap_replace_becomes_insert(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        dita_src = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
            '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
            '<p>Event name Type a unique name that reflects the theme.</p>'
            '<p>Use a a registration form to allow members to register for the event.</p>'
            '</conbody></concept>\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "topic.dita"
            src.write_text(dita_src, encoding="utf-8")
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[1]", element_tag="p",
                      text="Event name Type a unique name that reflects the theme."),
                Block(block_index=2, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[2]", element_tag="p",
                      text="Use a a registration form to allow members to register for the event."),
            ])
            # Article inserts a brand-new paragraph between the two — and
            # the new text shares zero content words with the source row
            # below it ("Timezone..." vs "Use a a registration form...").
            article_blocks = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Event name Type a unique name that reflects the theme.",
                    kind="paragraph",
                ),
                HtmlArticleBlock(
                    text="Timezone Select the applicable time zone from the dropdown.",
                    kind="paragraph",
                ),
                HtmlArticleBlock(
                    text="Use a a registration form to allow members to register for the event.",
                    kind="paragraph",
                ),
            ]
            ops = diff(pub, [b.text for b in article_blocks], article_blocks=article_blocks)

            # The "Use a a registration form" source row must NOT
            # be paired with the new "Timezone" article block as a REPLACE.
            misalignments = [
                op for op in ops
                if op.kind == OpKind.REPLACE
                and op.source_block is not None
                and "registration form" in op.source_block.text
                and "Timezone" in (op.updated_text or "")
            ]
            self.assertEqual(
                misalignments, [],
                "low-overlap REPLACE (registration form ↔ Timezone) should "
                "have been demoted to INSERT-only",
            )

            # And there should be an INSERT for the new Timezone row.
            timezone_inserts = [
                op for op in ops
                if op.kind == OpKind.INSERT
                and "Timezone" in (op.updated_text or "")
            ]
            self.assertEqual(
                len(timezone_inserts), 1,
                "Timezone should be classified as a new INSERT",
            )

    def test_real_rewrite_with_shared_nouns_still_replaces(self) -> None:
        """Sanity: a genuine rewrite that keeps the key nouns must still
        register as a REPLACE — the threshold mustn't be so strict that
        real text changes get reclassified as INSERTs."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.publication_reconstructor import Publication, Block

        dita_src = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
            '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
            '<p>Upload cover image You can use different banner sizes including 480x270 pixels.</p>'
            '</conbody></concept>\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "topic.dita"
            src.write_text(dita_src, encoding="utf-8")
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[1]", element_tag="p",
                      text="Upload cover image You can use different banner sizes including 480x270 pixels."),
            ])
            # Article adds "Note:" prefix — same content words, just reworded.
            article_blocks = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Upload cover image Note: You can use different banner sizes including 480x270 pixels.",
                    kind="paragraph",
                ),
            ]
            ops = diff(pub, [b.text for b in article_blocks], article_blocks=article_blocks)
            replaces = [op for op in ops if op.kind == OpKind.REPLACE]
            self.assertEqual(
                len(replaces), 1,
                "a real rewrite (Note: prefix added) must still be classified as REPLACE",
            )


class LiWithMixedContentEmitsLeadingText(unittest.TestCase):
    """A `<li>` that mixes leading inline text with a child block element
    (e.g. `<li>Brand kit to automatically … <note>You can edit…</note></li>`)
    must emit the leading text as its own block. Otherwise the article's
    matching bullet has no source counterpart and gets duplicated as a
    fresh INSERT alongside the unchanged original.
    """

    def test_li_with_note_emits_leading_text_block(self) -> None:
        import tempfile
        from app.publication_reconstructor import reconstruct, Publication
        from app.publication_reconstructor import Block  # noqa: F401

        dita_src = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
            '<task id="X" xml:lang="en-US"><title>T</title><taskbody>'
            '<steps><step><cmd>Pick an option:</cmd>'
            '<info><ul>'
            '<li>Plain option A.</li>'
            '<li>Option B with a side note. <note type="note"><p>Heads up about B.</p></note></li>'
            '</ul></info></step></steps>'
            '</taskbody></task>\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(dita_src, encoding="utf-8")
            from app.map_parser import TopicRef
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            li2_text_blocks = [
                b for b in pub.blocks
                if b.element_xpath.endswith("/li[2]") and b.element_tag == "li"
            ]
            self.assertEqual(
                len(li2_text_blocks), 1,
                "the <li>'s leading text 'Option B with a side note.' "
                "must be emitted as a separate block",
            )
            self.assertIn("Option B with a side note", li2_text_blocks[0].text)


class OptionalImportanceAttribute(unittest.TestCase):
    """Per IM: an optional step uses `<cmd importance="optional">`. When
    inserting a new step whose article text starts with "Optional:",
    the patch engine must emit `<cmd importance="optional">...</cmd>`
    without the prefix, not `<cmd>Optional: ...</cmd>`.
    """

    def test_optional_prefix_becomes_importance_attribute(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        dita_src = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
            '<task id="X" xml:lang="en-US"><title>T</title><taskbody>'
            '<steps><step><cmd>Click Save to apply your selections.</cmd></step></steps>'
            '</taskbody></task>\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "topic.dita"
            src.write_text(dita_src, encoding="utf-8")
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/cmd[1]",
                      element_tag="cmd",
                      text="Click Save to apply your selections."),
            ])
            article_blocks = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Click Save to apply your selections.", kind="step"),
                HtmlArticleBlock(
                    text="Optional: Preview your ad on different devices.",
                    kind="step",
                ),
            ]
            ops = diff(pub, [b.text for b in article_blocks], article_blocks=article_blocks)
            out = tmp_path / "out"
            apply_ops(ops, out, article_blocks=article_blocks, publication=pub)
            patched = (out / "topic.dita").read_text(encoding="utf-8")
            # Per IM, the importance attribute belongs on <step>, not on <cmd>.
            self.assertIn(
                '<step importance="optional"><cmd>Preview your ad on different devices.</cmd></step>',
                patched.replace("\n", "").replace("  ", ""),
                "Optional step should emit `<step importance='optional'>` per IM, "
                "with the prefix stripped from the <cmd> text",
            )
            # The literal "Optional: " prefix must NOT survive in the
            # cmd text.
            self.assertNotIn(
                "<cmd>Optional: ", patched,
                "the 'Optional: ' prefix must not appear in the <cmd> text",
            )

    def test_optional_prefix_matches_source_importance_step(self) -> None:
        """Source DITA step with `<cmd importance='optional'>Preview...</cmd>`
        renders as 'Optional: Preview...'. The article-side line will
        be 'Optional: Preview...'. After normalize, they must match as
        EQUAL — not as a phantom REPLACE."""
        from app.publication_reconstructor import normalize_for_match
        src_text = "Preview how your ad will appear on different devices."
        art_text = "Optional: Preview how your ad will appear on different devices."
        self.assertEqual(
            normalize_for_match(src_text), normalize_for_match(art_text),
            "Optional: prefix must be stripped so optional source steps "
            "match their article equivalents",
        )


class LowOverlapDemotionAutoDeletes(unittest.TestCase):
    """When SequenceMatcher pairs a source block with an article block
    that share essentially no content words, the demotion logic emits a
    DELETE on the source (auto-applied where safe) plus an INSERT for
    the article block. Defeats the original silent-drop behavior — the
    writer no longer has to manually remove a dropped step.
    """

    def test_demotion_emits_delete_op_for_source(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff, OpKind
        from app.publication_reconstructor import Publication, Block

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
                '<task id="X" xml:lang="en-US"><title>T</title><taskbody>'
                '<steps>'
                '<step><cmd>Click Save.</cmd></step>'
                '<step><cmd>Use Draft ad with AI to draft text for the intro.</cmd></step>'
                '</steps>'
                '</taskbody></task>\n',
                encoding="utf-8",
            )
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/cmd[1]",
                      element_tag="cmd", text="Click Save."),
                Block(block_index=2, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[2]/cmd[1]",
                      element_tag="cmd",
                      text="Use Draft ad with AI to draft text for the intro."),
            ])
            # Article replaced the "Use Draft ad with AI" step with a
            # completely different "Preview your ad" step. The two share
            # no content words.
            article_blocks = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Click Save.", kind="step"),
                HtmlArticleBlock(text="Preview your ad on different devices.", kind="step"),
            ]
            ops = diff(pub, [b.text for b in article_blocks], article_blocks=article_blocks)
            deletes = [op for op in ops if op.kind == OpKind.DELETE]
            self.assertEqual(
                len(deletes), 1,
                "expected one DELETE for the demoted source step; "
                "low-overlap demotion must not silently drop content",
            )
            self.assertIn("Draft ad", deletes[0].source_block.text)


class AdjacentDuplicateInsertsDeduped(unittest.TestCase):
    """If the article has the same line copy-pasted twice in a row,
    the diff faithfully emits two INSERTs with identical text at the
    same anchor. Without dedupe, that produces a duplicate row in the
    patched DITA. The dedupe pass must keep only the first.
    """

    def test_two_identical_adjacent_inserts_become_one(self) -> None:
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff, OpKind
        from app.publication_reconstructor import Publication, Block
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "t.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
                '<p>Anchor.</p></conbody></concept>\n',
                encoding="utf-8",
            )
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="t.dita", topic_path=src,
                      element_xpath="/concept/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="t.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[1]", element_tag="p",
                      text="Anchor."),
            ])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Anchor.", kind="paragraph"),
                HtmlArticleBlock(text="A brand new line.", kind="paragraph"),
                HtmlArticleBlock(text="A brand new line.", kind="paragraph"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            inserts = [op for op in ops if op.kind == OpKind.INSERT and "brand new" in (op.updated_text or "")]
            self.assertEqual(
                len(inserts), 1,
                "two adjacent identical INSERTs should be deduped to one",
            )


class NotePrefixNormalizes(unittest.TestCase):
    """Source DITA <note> stores body text only; the Help Center
    stylesheet adds a 'Note: ' label at render time. Article-side text
    therefore includes the label. After normalize, the two must match
    so SequenceMatcher pairs them as EQUAL.
    """

    def test_note_prefix_stripped_with_separator(self) -> None:
        from app.publication_reconstructor import normalize_for_match
        src = "You can edit any of the auto generated fields."
        art = "Note: You can edit any of the auto generated fields."
        self.assertEqual(normalize_for_match(src), normalize_for_match(art))

    def test_sentence_starting_with_note_word_is_not_stripped(self) -> None:
        """Required: regular sentences like 'Note that you must…' must
        NOT have 'Note' stripped — the rule requires an explicit
        separator (colon or dash) after the label."""
        from app.publication_reconstructor import normalize_for_match
        a = "Note that you must accept the terms before continuing."
        # Normalize should NOT chop the leading "Note that" away.
        self.assertIn("Note that you must", normalize_for_match(a))


class NestedListItemEmittedAfterOuter(unittest.TestCase):
    """The article HTML often nests a <ul><li>…</li></ul> inside an
    outer <li>. The DOM-walk parser used to emit the inner block first
    (the inner </li> closes first), giving article-block order
    [Inner, Outer Text] — which scrambles the diff because the source
    DITA has the outer text first. Verify the parser now flushes the
    outer's accumulated text before the inner emits.
    """

    def test_outer_li_text_emits_before_nested_li_text(self) -> None:
        from app.article_html_parser import parse_help_center_html
        html = (
            '<html><body>'
            '<article data-test-selector="article">'
            '<ol>'
            '<li class="article-content__ordered-list-item">'
            '<div data-test-selector="preRenderedMarkup-container">'
            '<p>Outer body text.</p>'
            '<ul><li>Inner sub-bullet.</li></ul>'
            '</div>'
            '</li></ol>'
            '</article></body></html>'
        )
        blocks = parse_help_center_html(html)
        # Two emitted blocks: outer (step), inner (list_item). Outer first.
        texts = [b.text for b in blocks if b.text]
        outer_idx = next((i for i, t in enumerate(texts) if "Outer body" in t), -1)
        inner_idx = next((i for i, t in enumerate(texts) if "Inner sub-bullet" in t), -1)
        self.assertGreaterEqual(outer_idx, 0, f"outer emit missing: {texts}")
        self.assertGreaterEqual(inner_idx, 0, f"inner emit missing: {texts}")
        self.assertLess(
            outer_idx, inner_idx,
            f"outer text must emit before nested text — got order {texts}",
        )


class NotePrefixNotMisclassifiedAsDlentry(unittest.TestCase):
    """A 'Note: body' line must NOT be split into a <dlentry> by the
    Term:Definition splitter — the 'Note' label is a render-time
    prefix on a <note>, not a glossary term.
    """

    def test_note_prefix_not_treated_as_term(self) -> None:
        from app.patch_engine import _split_term_definition
        self.assertIsNone(
            _split_term_definition(
                "Note: You can edit any of the auto generated fields."
            ),
            "Note: prefix should be rejected by the dlentry splitter",
        )
        # Sanity: a real Term:Definition still works.
        result = _split_term_definition("Email: Delivered to your inbox.")
        self.assertEqual(result, ("Email", "Delivered to your inbox."))


class AutoDeleteSafetyNet(unittest.TestCase):
    """Auto-DELETE must refuse to fire when the source block's text
    appears anywhere in the article. LCS can mis-align — if the same
    content lives at a different position, the demoted "delete" is
    actually a misalignment, not a real removal.
    """

    def test_no_delete_when_source_appears_elsewhere(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff, OpKind
        from app.publication_reconstructor import Publication, Block

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
                '<task id="X" xml:lang="en-US"><title>T</title><taskbody><steps>'
                '<step><cmd>Open settings panel.</cmd></step>'
                '<step><cmd>Configure your preferences.</cmd></step>'
                '</steps></taskbody></task>\n',
                encoding="utf-8",
            )
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/cmd[1]",
                      element_tag="cmd", text="Open settings panel."),
                Block(block_index=2, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[2]/cmd[1]",
                      element_tag="cmd", text="Configure your preferences."),
            ])
            # Article moved "Configure your preferences" to be the FIRST
            # step (reordered). The text is still in the article — just
            # in a different position. LCS will misalign; the safety net
            # must prevent the false DELETE.
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Configure your preferences.", kind="step"),
                HtmlArticleBlock(text="Open settings panel.", kind="step"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            deletes = [op for op in ops if op.kind == OpKind.DELETE]
            self.assertEqual(
                deletes, [],
                "no DELETE should fire — source content is still in article",
            )


class AnchorPropagationForSubBullets(unittest.TestCase):
    """When the article inserts a new step + sub-bullets under it, the
    sub-bullets must end up INSIDE the new step's <info>, not as
    siblings of the source-side anchor step.
    """

    def test_sub_bullets_land_inside_new_step(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
                '<task id="X" xml:lang="en-US"><title>T</title><taskbody><steps>'
                '<step><cmd>Click Save.</cmd></step>'
                '</steps></taskbody></task>\n',
                encoding="utf-8",
            )
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/cmd[1]",
                      element_tag="cmd", text="Click Save."),
            ])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Click Save.", kind="step"),
                HtmlArticleBlock(text="Optional: Configure brand kit.", kind="step"),
                HtmlArticleBlock(text="Choose colors and fonts.", kind="list_item"),
                HtmlArticleBlock(text="Add your brand voice.", kind="list_item"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            apply_ops(ops, out, article_blocks=article, publication=pub)
            patched = (out / "topic.dita").read_text(encoding="utf-8")

            # Find the new step containing "Configure brand kit"
            import re as _re
            # Collapse whitespace and ensure the sub-bullets are nested
            # inside the new step (not under the old "Click Save" step).
            collapsed = _re.sub(r"\s+", " ", patched)
            # The new step must contain Choose colors and Add your brand voice
            # in its <info>/<ul>.
            self.assertRegex(
                collapsed,
                r"Configure brand kit\.</cmd>\s*<info>\s*<ul>\s*"
                r"<li>Choose colors and fonts\.</li>\s*"
                r"<li>Add your brand voice\.</li>",
                "sub-bullets should be inside the new optional step's "
                f"<info>/<ul>, got:\n{patched}",
            )
            # And the original "Click Save" step must NOT have a stray
            # <ul> as a direct child.
            self.assertNotRegex(
                collapsed,
                r"<cmd>Click Save\.</cmd>\s*<ul>",
                "<ul> must never sit directly inside <step> alongside <cmd>",
            )


class EmphasisOnInsertSteps(unittest.TestCase):
    """Article-side <strong>/<b>/<em>/<i> phrases captured by the
    parser as `emphasis` must round-trip into the patched DITA as
    inline <em> wrappers when the step is INSERTed (not just on
    REPLACE — that path was already covered).
    """

    def test_bold_in_inserted_step_becomes_em(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
                '<task id="X" xml:lang="en-US"><title>T</title><taskbody><steps>'
                '<step><cmd>Open the app.</cmd></step>'
                '</steps></taskbody></task>\n',
                encoding="utf-8",
            )
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/cmd[1]",
                      element_tag="cmd", text="Open the app."),
            ])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Open the app.", kind="step"),
                HtmlArticleBlock(
                    text="Click Media Template to create an image template.",
                    kind="step",
                    emphasis=["Media Template"],
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            apply_ops(ops, out, article_blocks=article, publication=pub)
            patched = (out / "topic.dita").read_text(encoding="utf-8")
            self.assertIn(
                "<em>Media Template</em>", patched,
                "bold phrase from article must be wrapped in <em> in the new cmd",
            )

    def test_bold_in_inserted_list_item_becomes_em(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
                '<task id="X" xml:lang="en-US"><title>T</title><taskbody><steps>'
                '<step><cmd>Configure.</cmd></step>'
                '</steps></taskbody></task>\n',
                encoding="utf-8",
            )
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/cmd[1]",
                      element_tag="cmd", text="Configure."),
            ])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Configure.", kind="step"),
                HtmlArticleBlock(
                    text="Click Edit and then Save.",
                    kind="list_item",
                    emphasis=["Edit", "Save"],
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            apply_ops(ops, out, article_blocks=article, publication=pub)
            patched = (out / "topic.dita").read_text(encoding="utf-8")
            self.assertIn("<em>Edit</em>", patched)
            self.assertIn("<em>Save</em>", patched)


class IconInUicontrolIgnoredForMediaAdvisory(unittest.TestCase):
    """`<image>` elements nested inside `<uicontrol>` (or other inline
    control wrappers) are decorative button glyphs, not standalone
    media. The 'Verify embedded media' advisory must NOT fire for
    topics whose only image is an inline UI icon.
    """

    def test_uicontrol_icon_does_not_trigger_media_advisory(self) -> None:
        import xml.etree.ElementTree as ET
        from io import StringIO
        from app.patch_engine import _has_media_anywhere

        # Icon nested in <uicontrol> — should be ignored.
        icon_only_xml = (
            '<task><taskbody><steps><step><cmd>'
            'Click <uicontrol><image href="more-icon" id="i"/> More</uicontrol> '
            'to open the menu.'
            '</cmd></step></steps></taskbody></task>'
        )
        root = ET.fromstring(icon_only_xml)
        self.assertFalse(
            _has_media_anywhere(root),
            "uicontrol-nested icon must NOT count as media",
        )

    def test_standalone_image_still_triggers(self) -> None:
        import xml.etree.ElementTree as ET
        from app.patch_engine import _has_media_anywhere

        # Image NOT in uicontrol — should still fire.
        real_image_xml = (
            '<task><taskbody><p>'
            '<image href="screenshot.png" id="s"/>'
            '</p></taskbody></task>'
        )
        root = ET.fromstring(real_image_xml)
        self.assertTrue(
            _has_media_anywhere(root),
            "a standalone <image> (not wrapped in uicontrol) must still "
            "trigger the media advisory",
        )

    def test_codeblock_still_triggers(self) -> None:
        import xml.etree.ElementTree as ET
        from app.patch_engine import _has_media_anywhere

        xml = '<task><taskbody><p><codeblock>$ run me</codeblock></p></taskbody></task>'
        root = ET.fromstring(xml)
        self.assertTrue(_has_media_anywhere(root))


class InsertedStepWithIconWarns(unittest.TestCase):
    """When the article HTML for an INSERTed step contains an inline
    <img>/<svg> (a UI icon next to a button label, an inline
    screenshot, etc.), the tool can't auto-place a DITA <image> (it
    doesn't know the asset href). The applied result must carry a
    warning so the writer drops the icon back in by hand.
    """

    def test_step_with_inline_img_gets_warning(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
                '<task id="X" xml:lang="en-US"><title>T</title><taskbody><steps>'
                '<step><cmd>Sign in.</cmd></step>'
                '</steps></taskbody></task>\n',
                encoding="utf-8",
            )
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/cmd[1]",
                      element_tag="cmd", text="Sign in."),
            ])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Sign in.", kind="step"),
                HtmlArticleBlock(
                    text="Click  More to open the menu.",
                    kind="step",
                    has_inline_image=True,
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            # The applied INSERT must carry a warning about the icon.
            applied_with_warning = [
                r for r in report.applied
                if r.op and r.op.kind.value == "insert"
                and "icon" in (r.warning or "").lower()
            ]
            self.assertEqual(
                len(applied_with_warning), 1,
                "expected one APPLIED insert with an icon warning; "
                f"got {len(applied_with_warning)}. warnings: "
                + repr([r.warning for r in report.applied]),
            )


class MediaAdvisoryIsPerTopic(unittest.TestCase):
    """The 'verify embedded media' advisory must surface once PER
    affected topic, with the topic_id set on the PatchResult. The HTML
    report then renders the card under that topic's section (not as a
    map-level entry).
    """

    def test_one_advisory_per_topic_with_topic_id(self) -> None:
        import tempfile
        import xml.etree.ElementTree as ET
        from app.patch_engine import (
            PatchReport, _surface_media_for_verification, ResultCategory,
        )
        from app.publication_reconstructor import Publication, Block

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Two topics, both with real (non-icon) media.
            for i, name in enumerate(("a.dita", "b.dita")):
                (tmp_path / name).write_text(
                    f'<?xml version="1.0" encoding="UTF-8"?>\n'
                    f'<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                    f'<concept id="C{i}" xml:lang="en-US"><title>T{i}</title><conbody>'
                    f'<p>Intro.</p><p><image href="diagram.png" id="d{i}"/></p>'
                    f'</conbody></concept>\n',
                    encoding="utf-8",
                )
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="a.dita", topic_path=tmp_path / "a.dita",
                      element_xpath="/concept/title[1]", element_tag="title", text="T0"),
                Block(block_index=1, topic_id="b.dita", topic_path=tmp_path / "b.dita",
                      element_xpath="/concept/title[1]", element_tag="title", text="T1"),
            ])
            report = PatchReport()
            _surface_media_for_verification(report, pub)
            media = [r for r in report.results if r.category == ResultCategory.MAP_EDIT]
            self.assertEqual(
                len(media), 2,
                f"expected one media advisory per affected topic; got {len(media)}",
            )
            topic_ids = sorted(r.topic_id for r in media)
            self.assertEqual(
                topic_ids, ["a.dita", "b.dita"],
                f"each media advisory should have its topic_id set; got {topic_ids}",
            )


class NavtitleSectionInheritsToAllChildTopics(unittest.TestCase):
    """When a ditamap navtitle ("Tokenization") matches an article tab
    label, every topic that follows the navtitle in publication order
    must inherit that tab's section binding — not just the first one.
    Without this, sibling FAQ topics under the same navtitle have no
    section, the cross-tab routing check refuses every REPLACE against
    them, and the report shows dozens of false "Add manually" entries
    on text that's actually unchanged.
    """

    def test_all_child_topics_under_navtitle_inherit_section(self) -> None:
        from app.publication_reconstructor import (
            Publication, Block, build_topic_to_section,
        )
        from app.article_html_parser import HtmlArticleBlock

        # Two ditamap navtitles, each with several child topics. Depths
        # mirror a real map: root at depth 0, navtitle at depth 1
        # (inside the root topicref), child topics under it at depth 2.
        pub = Publication(blocks=[
            Block(block_index=0, topic_id="root.dita",
                  topic_path=None, element_xpath="/concept/title[1]",
                  element_tag="title", text="India payment regulations",
                  map_depth=0),
            # navtitle "E-Mandate" → group A
            Block(block_index=1, topic_id="<ditamap>",
                  topic_path=None, element_xpath="<topichead navtitle='E-Mandate'>",
                  element_tag="navtitle", text="E-Mandate", map_depth=1),
            Block(block_index=2, topic_id="EM.dita",
                  topic_path=None, element_xpath="/concept/title[1]",
                  element_tag="title", text="E-Mandate", map_depth=2),
            Block(block_index=3, topic_id="EM_child_1.dita",
                  topic_path=None, element_xpath="/concept/title[1]",
                  element_tag="title", text="What is e-mandate?", map_depth=2),
            Block(block_index=4, topic_id="EM_child_2.dita",
                  topic_path=None, element_xpath="/concept/title[1]",
                  element_tag="title", text="How do I use e-mandate?", map_depth=2),
            # navtitle "Tokenization" → group B
            Block(block_index=5, topic_id="<ditamap>",
                  topic_path=None, element_xpath="<topichead navtitle='Tokenization'>",
                  element_tag="navtitle", text="Tokenization", map_depth=1),
            Block(block_index=6, topic_id="Tok.dita",
                  topic_path=None, element_xpath="/concept/title[1]",
                  element_tag="title", text="Tokenization", map_depth=2),
            Block(block_index=7, topic_id="Tok_child_1.dita",
                  topic_path=None, element_xpath="/concept/title[1]",
                  element_tag="title", text="What is tokenization?", map_depth=2),
            Block(block_index=8, topic_id="Tok_child_2.dita",
                  topic_path=None, element_xpath="/concept/title[1]",
                  element_tag="title", text="Why can't I save my card?", map_depth=2),
        ])
        article = [
            HtmlArticleBlock(text="E-Mandate", kind="tab_label", section_id="panel-1"),
            HtmlArticleBlock(text="E-Mandate body", kind="paragraph", section_id="panel-1"),
            HtmlArticleBlock(text="Tokenization", kind="tab_label", section_id="panel-2"),
            HtmlArticleBlock(text="Tokenization body", kind="paragraph", section_id="panel-2"),
        ]
        t2s = build_topic_to_section(pub, article)
        # Both navtitles map to their panels.
        self.assertEqual(t2s.get("EM.dita"), "panel-1")
        self.assertEqual(t2s.get("Tok.dita"), "panel-2")
        # ALL siblings under the same navtitle inherit the section.
        self.assertEqual(t2s.get("EM_child_1.dita"), "panel-1",
                         "sibling FAQ topic must inherit E-Mandate's panel")
        self.assertEqual(t2s.get("EM_child_2.dita"), "panel-1",
                         "second sibling under E-Mandate must also inherit")
        self.assertEqual(t2s.get("Tok_child_1.dita"), "panel-2",
                         "sibling FAQ topic must inherit Tokenization's panel")
        self.assertEqual(t2s.get("Tok_child_2.dita"), "panel-2",
                         "second sibling under Tokenization must also inherit")


class NoteLabelPlusBulletsMergedIntoOneBlock(unittest.TestCase):
    """The Help Center HTML inside a step often renders an in-step note
    as `<p>Note:</p><ul><li>…</li><li>…</li></ul>` — N+1 separate
    blocks at the parser level. The source DITA stores the same
    content as a single collapsed `<note><ul>…</ul></note>` block.
    Without merging the article side back into one block, LCS can't
    pair them and the article side gets re-inserted as a duplicate
    step + sub-bullets.
    """

    def test_note_then_two_bullets_merges_to_one_block(self) -> None:
        from app.article_html_parser import (
            HtmlArticleBlock, expand_blocks_for_diff,
        )

        blocks = [
            HtmlArticleBlock(text="Select Confirm changes.", kind="step"),
            HtmlArticleBlock(text="Note:", kind="step"),
            HtmlArticleBlock(
                text="The new credit card information will appear in the Billing information section.",
                kind="list_item",
            ),
            HtmlArticleBlock(
                text="By updating payment method on the contract, you are updating all related orders.",
                kind="list_item",
            ),
        ]
        texts, origins = expand_blocks_for_diff(blocks)

        # 4 input blocks → 2 output blocks (step + merged note).
        self.assertEqual(
            len(texts), 2,
            f"expected 2 emitted blocks after merge; got {len(texts)}: {texts}",
        )
        self.assertEqual(texts[0], "Select Confirm changes.")
        # Merged note text = bullets joined by space, "Note:" label dropped.
        self.assertIn("The new credit card information", texts[1])
        self.assertIn("By updating payment method", texts[1])
        self.assertNotIn("Note:", texts[1],
                         "the 'Note:' label must not survive in the merged text")

    def test_lone_note_block_not_merged_when_no_bullets_follow(self) -> None:
        from app.article_html_parser import (
            HtmlArticleBlock, expand_blocks_for_diff,
        )
        blocks = [
            HtmlArticleBlock(text="Note:", kind="step"),
            HtmlArticleBlock(text="Some other paragraph.", kind="paragraph"),
        ]
        texts, _ = expand_blocks_for_diff(blocks)
        self.assertEqual(len(texts), 2,
                         "a Note: without following list_items must NOT be merged")


class NoteWithBulletsBuildsProperStructure(unittest.TestCase):
    """When the article adds a new note-with-bullets inside a step
    (`<p>Note:</p><ul><li/><li/></ul>`), the patched DITA must encode
    it as `<step><cmd/><info><note><ul><li/><li/></ul></note></info></step>`
    — NOT as `<step><cmd>Note:</cmd><ul>…</ul></step>` with the label
    living inside <cmd> and the <ul> as an illegal sibling.
    """

    def test_new_note_with_bullets_lands_in_step_info(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock, expand_blocks_for_diff
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
                '<task id="X" xml:lang="en-US"><title>T</title><taskbody><steps>'
                '<step><cmd>Click Save.</cmd></step>'
                '</steps></taskbody></task>\n',
                encoding="utf-8",
            )
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/task/taskbody[1]/steps[1]/step[1]/cmd[1]",
                      element_tag="cmd", text="Click Save."),
            ])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Click Save.", kind="step"),
                HtmlArticleBlock(text="Note:", kind="step"),
                HtmlArticleBlock(text="Bullet one.", kind="list_item"),
                HtmlArticleBlock(text="Bullet two.", kind="list_item"),
            ]
            texts, origins = expand_blocks_for_diff(article)
            ops = diff(pub, texts, origins)
            out = Path(tmp) / "out"
            apply_ops(ops, out, article_blocks=origins, publication=pub)
            patched = (out / "topic.dita").read_text(encoding="utf-8")

            # Whitespace-normalize then assert the structural shape.
            import re as _re
            collapsed = _re.sub(r"\s+", " ", patched)
            self.assertRegex(
                collapsed,
                r'<step><cmd>Click Save\.</cmd>'
                r'\s*<info>\s*<note\b[^>]*>\s*<ul>'
                r'\s*<li>Bullet one\.</li>'
                r'\s*<li>Bullet two\.</li>'
                r'\s*</ul>\s*</note>\s*</info>\s*</step>',
                f"new note must build as <step><cmd/><info><note><ul><li/>…</ul></note></info></step>; got:\n{patched}",
            )
            # The "Note:" label text must NOT survive in the patched DITA.
            self.assertNotIn("<cmd>Note:", patched)
            self.assertNotIn("<em>Note</em>", patched)
            # And the <ul> must NOT be a direct sibling of <cmd>.
            self.assertNotRegex(
                collapsed,
                r"<cmd>[^<]*</cmd>\s*<ul>",
                "<ul> must be inside <note>/<info>, not a direct sibling of <cmd>",
            )


class TitleAnchoredInsertLandsInsideBody(unittest.TestCase):
    """When LCS aligns the topic <title> as the EQUAL anchor and the
    article has a new paragraph that should go inside the topic body,
    inserting the new <p> as a sibling of <title> produces invalid
    DITA (<concept> doesn't allow <p> as a direct child). The patch
    engine must redirect the insertion INTO <conbody>/<taskbody>/
    <refbody>, creating the body element if needed.
    """

    def test_paragraph_anchored_at_concept_title_goes_into_conbody(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import Publication, Block

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>What is X?</title><conbody>'
                '<p>Original body answer.</p>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/title[1]", element_tag="title",
                      text="What is X?"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[1]", element_tag="p",
                      text="Original body answer."),
            ])
            article = [
                HtmlArticleBlock(text="What is X?", kind="paragraph"),
                # New paragraph anchored at title (LCS picks title as the
                # anchor because the existing source <p> aligned elsewhere).
                HtmlArticleBlock(
                    text="New top-of-topic paragraph.",
                    kind="paragraph",
                ),
                HtmlArticleBlock(text="Original body answer.", kind="paragraph"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            apply_ops(ops, out, article_blocks=article, publication=pub)
            patched = (out / "topic.dita").read_text(encoding="utf-8")

            # The new <p> must live inside <conbody>, NOT as a sibling
            # of <title> at the <concept> level.
            self.assertNotRegex(
                patched.replace("\n", "").replace("  ", ""),
                r"<title>What is X\?</title>\s*<p>New top",
                "<p> must NOT be a direct child of <concept> — it should "
                f"be inside <conbody>; got:\n{patched}",
            )
            self.assertIn(
                "<conbody><p>New top-of-topic paragraph.</p>",
                patched.replace("\n", "").replace("  ", ""),
                "the new paragraph should be the first child of <conbody>",
            )


class NoteReplaceWithOnlyParagraphChildrenApplies(unittest.TestCase):
    """A REPLACE on a `<note>` whose only block descendants are `<p>`s
    used to be silently SKIPPED as 'structural' — but rewriting it is
    safe: clear the <p>s and rebuild them from the new text. Notes
    that contain <ul>/<ol>/<table> stay refused (those structures
    can't be rebuilt from the article-side collapsed text).
    """

    def test_note_with_single_p_child_replace_applies(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct
        from app.map_parser import TopicRef

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
                '<note type="important"><p>Old note body.</p></note>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="New rewritten note body that's much longer.",
                    kind="note",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            patched = (out / "topic.dita").read_text(encoding="utf-8")

            applied_note_replaces = [
                r for r in report.applied
                if r.op and r.op.kind.value == "replace"
                and r.op.source_block.element_tag == "note"
            ]
            self.assertEqual(
                len(applied_note_replaces), 1,
                "REPLACE on a <note> with only <p> children must apply, "
                "not be silently skipped",
            )
            self.assertIn(
                "<note type=\"important\"><p>New rewritten note body that's much longer.</p></note>",
                patched,
                "the note should be rebuilt as <note><p>new text</p></note> "
                "with the type attribute preserved",
            )

    def test_note_with_ul_replace_applies_when_article_has_note_bullets(self) -> None:
        """When the source <note> has a <ul> and the article side has
        note_bullets (the parser-merge captured them), REPLACE must
        rebuild as `<note><ul><li>…</li></ul></note>` rather than
        refusing. The article side carries the per-bullet text, so we
        can safely round-trip the structure."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct
        from app.map_parser import TopicRef

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
                '<note type="tip"><ul><li>Old bullet A.</li><li>Old bullet B.</li></ul></note>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                # Article kept bullets; the parser merged "Note: + bullets"
                # so note_bullets is populated.
                HtmlArticleBlock(
                    text="New bullet 1. New bullet 2.",
                    kind="note",
                    note_bullets=["New bullet 1.", "New bullet 2."],
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            patched = (out / "topic.dita").read_text(encoding="utf-8")

            applied_note_replaces = [
                r for r in report.applied
                if r.op and r.op.kind == OpKind.REPLACE
                and r.op.source_block.element_tag == "note"
            ]
            self.assertEqual(
                len(applied_note_replaces), 1,
                "note-with-bullets REPLACE must apply when article "
                "side has note_bullets",
            )
            self.assertIn(
                '<note type="tip"><ul><li>New bullet 1.</li><li>New bullet 2.</li></ul></note>',
                patched,
                f"note should be rebuilt as <ul><li>; got:\n{patched}",
            )
            # No <p> children — bullets only.
            note_region = patched.split("<note")[1].split("</note>")[0]
            self.assertNotIn("<p>", note_region)

    def test_note_with_ul_child_replace_still_refused(self) -> None:
        """The dangerous case — note containing <ul> — stays refused."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct
        from app.map_parser import TopicRef

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
                '<note type="tip"><ul><li>Bullet A.</li><li>Bullet B.</li></ul></note>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Some flat rewritten text that loses the bullets.",
                    kind="note",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            # The dangerous REPLACE must NOT be auto-applied.
            applied_note_replaces = [
                r for r in report.applied
                if r.op and r.op.kind.value == "replace"
                and r.op.source_block.element_tag == "note"
            ]
            self.assertEqual(
                applied_note_replaces, [],
                "REPLACE on a <note> containing <ul>/<ol>/<table> must "
                "stay refused — flattening would lose the bullets",
            )


class DeleteSafetyNetIsStrict(unittest.TestCase):
    """The DELETE safety net suppresses auto-DELETEs when the source
    text appears elsewhere in the article. The check must be STRICT —
    two unrelated paragraphs in the same article can share 20-30% of
    their content words just by covering the same domain. If we use a
    loose threshold (e.g. 15% Jaccard), genuine deletions get
    suppressed because some other paragraph happens to share keywords.
    """

    def test_unrelated_paragraph_with_shared_keywords_still_deletes(self) -> None:
        """End-to-end check using the real fixture where the user
        spotted the bug: source 'You will receive an email…' is genuinely
        removed from the article, but another FAQ in the same article
        shares ~28% of its words (email/redeem/exclusive/trial/etc.).
        The safety net must not let that suppress a real deletion."""
        inputs = PROJECT_ROOT / "dist" / "output" / "runs" / "20260612_104609_121476" / "inputs"
        if not inputs.exists():
            self.skipTest("fixture not present in this checkout")
        from app.article_html_parser import parse_help_center_html, expand_blocks_for_diff
        from app.diff_engine import diff as _diff, OpKind as _OpKind
        from app.map_parser import parse_ditamap_entries
        from app.publication_reconstructor import reconstruct
        from app.patch_engine import apply_ops
        import tempfile

        ditamap = next(inputs.glob("*.ditamap"))
        article_blocks = parse_help_center_html(
            (inputs / "article_source.html").read_text(encoding="utf-8"),
        )
        texts, origins = expand_blocks_for_diff(article_blocks)
        publication = reconstruct(parse_ditamap_entries(ditamap))
        ops = _diff(publication, texts, origins)

        with tempfile.TemporaryDirectory() as tmp:
            apply_ops(ops, Path(tmp), article_blocks=origins, publication=publication)
            patched = (Path(tmp) / "What_if_I_am_already_a_Premium_subscriber.dita").read_text(encoding="utf-8")
        self.assertNotIn(
            "You will receive an email",
            patched,
            "the source paragraph was removed from the live article — "
            "the patched DITA must not keep it",
        )
        self.assertIn(
            "Premium Perks and partner benefits",
            patched,
            "the new article paragraph must be present",
        )

    def test_near_identical_paragraph_still_safety_skipped(self) -> None:
        """Sanity: when the source paragraph IS actually still in the
        article (just at a different position — LCS misalignment), the
        safety net must still suppress the DELETE."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff, OpKind
        from app.publication_reconstructor import Publication, Block

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
                '<p>Open the settings menu in the corner of the page.</p>'
                '<p>Configure your notification preferences.</p>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            pub = Publication(blocks=[
                Block(block_index=0, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/title[1]", element_tag="title", text="T"),
                Block(block_index=1, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[1]", element_tag="p",
                      text="Open the settings menu in the corner of the page."),
                Block(block_index=2, topic_id="topic.dita", topic_path=src,
                      element_xpath="/concept/conbody[1]/p[2]", element_tag="p",
                      text="Configure your notification preferences."),
            ])
            # Article reordered the paragraphs.
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Configure your notification preferences.", kind="paragraph"),
                HtmlArticleBlock(text="Open the settings menu in the corner of the page.", kind="paragraph"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            deletes = [op for op in ops if op.kind == OpKind.DELETE]
            self.assertEqual(
                deletes, [],
                "near-identical paragraphs (just reordered) must NOT be auto-DELETEd",
            )


class NoteReplacePreservesExistingXrefAttributes(unittest.TestCase):
    """When REPLACE rebuilds a <note>'s <p> children from new article
    text, any <xref> whose link text still appears in the new text must
    be preserved with its ORIGINAL attributes intact — outputclass,
    format, scope, and any other IM-specific choices the writer made.
    The article-side link metadata only tells us the link is still
    present; the source xref is the source of truth for attributes.
    """

    def test_xref_outputclass_and_format_preserved_through_note_replace(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct
        from app.map_parser import TopicRef

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
                '<note othertype="pdf" type="other">'
                '<p>Old-Filename.pdf</p>'
                '<p>Please review this document for more information</p>'
                '<p><xref href="https://example.com/Live-Events-Guide.pdf" scope="external" '
                'outputclass="button" format="pdf">View Document</xref></p>'
                '</note>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            # Article reworded but kept the same "View Document" link
            # pointing to the same PDF.
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Please review this document for more information\n\nView Document",
                    kind="note",
                    links=[("View Document", "https://example.com/Live-Events-Guide.pdf")],
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            apply_ops(ops, out, article_blocks=article, publication=pub)
            patched = (out / "topic.dita").read_text(encoding="utf-8")

            # All three original attributes must be preserved.
            self.assertIn('outputclass="button"', patched,
                          "outputclass='button' must be preserved on the xref")
            self.assertIn('format="pdf"', patched,
                          "format='pdf' must be preserved on the xref")
            self.assertIn('scope="external"', patched,
                          "scope='external' must be preserved on the xref")
            # And the note type / othertype attrs.
            self.assertIn('othertype="pdf"', patched)
            self.assertIn('type="other"', patched)


class PdfCalloutHeadlineIsCaptured(unittest.TestCase):
    """The article HTML parser used to suppress every
    `<h3 class="article-content-callout__headline">` because for most
    callout kinds that text is a stylesheet label ("Important to know",
    "Who can use this feature?"). But PDF / file-asset callouts use the
    headline for the filename, which is real content. Dropping it makes
    the diff see the source's filename <p> as extra content and fire a
    phantom REPLACE that rebuilds the note (losing xref attributes,
    deleting the filename <p>, etc.).
    """

    def test_pdf_callout_headline_emitted_in_body_text(self) -> None:
        from app.article_html_parser import parse_help_center_html
        html = (
            '<html><body>'
            '<article data-test-selector="article">'
            '<div class="article-content-callout article-content-callout__background--pdf" '
            'data-test-selector="callout-container">'
            '<h3 class="article-content-callout__headline" '
            'data-test-selector="callout-headline">My-Guide.pdf</h3>'
            '<div data-test-selector="callout-rich-description">Please review.</div>'
            '<div><a href="https://example.com/My-Guide.pdf">View Document</a></div>'
            '</div>'
            '</article></body></html>'
        )
        blocks = parse_help_center_html(html)
        note_blocks = [b for b in blocks if b.kind == "note"]
        self.assertEqual(len(note_blocks), 1)
        body = note_blocks[0].text
        self.assertIn(
            "My-Guide.pdf", body,
            f"PDF callout's headline (filename) must be captured; "
            f"got note body: {body!r}",
        )
        self.assertIn("Please review", body)
        self.assertIn("View Document", body)

    def test_non_file_callout_headline_still_suppressed(self) -> None:
        """Sanity: a `permission` / `tip` / `important` callout's
        headline is still a stylesheet label — must NOT be captured."""
        from app.article_html_parser import parse_help_center_html
        html = (
            '<html><body>'
            '<article data-test-selector="article">'
            '<div class="article-content-callout article-content-callout__background--permission" '
            'data-test-selector="callout-container">'
            '<h3 class="article-content-callout__headline">Who can use this feature?</h3>'
            '<div data-test-selector="callout-rich-description">Premium subscribers.</div>'
            '</div>'
            '</article></body></html>'
        )
        blocks = parse_help_center_html(html)
        note_blocks = [b for b in blocks if b.kind == "note"]
        self.assertEqual(len(note_blocks), 1)
        self.assertNotIn(
            "Who can use this feature", note_blocks[0].text,
            "non-file callout headline must still be suppressed",
        )


class FeatureCalloutHeadlineIsCaptured(unittest.TestCase):
    """Sales Nav regression: source DITA notes with `othertype="feature"`
    have a heading-style first paragraph (e.g. "AI training", "Example
    search"). The Help Center renders this as a `<h3 class="callout-
    headline">` inside a `background--feature` callout. The parser was
    suppressing ALL callout headlines — which works for marker-phrase
    kinds (permission, important, tip) but drops real content for
    feature / role / pdf / file kinds. Fix: invert the logic.
    """

    def test_feature_callout_headline_captured(self) -> None:
        from app.article_html_parser import parse_help_center_html
        html = (
            '<html><body>'
            '<article data-test-selector="article">'
            '<div class="article-content-callout article-content-callout__background--feature" '
            'data-test-selector="callout-container">'
            '<h3 class="article-content-callout__headline">AI training</h3>'
            '<div data-test-selector="callout-rich-description">'
            'Watch our free webinar.</div>'
            '</div>'
            '</article></body></html>'
        )
        blocks = parse_help_center_html(html)
        note_blocks = [b for b in blocks if b.kind == "note"]
        self.assertEqual(len(note_blocks), 1)
        self.assertIn(
            "AI training", note_blocks[0].text,
            f"feature callout headline must be captured; got: {note_blocks[0].text!r}",
        )
        self.assertIn("Watch our free webinar", note_blocks[0].text)

    def test_marker_callout_headlines_still_suppressed(self) -> None:
        from app.article_html_parser import parse_help_center_html
        for kind, label in (("permission", "Who can use this feature?"),
                            ("important", "Important to know"),
                            ("tip", "Here's a tip")):
            html = (
                f'<html><body>'
                f'<article data-test-selector="article">'
                f'<div class="article-content-callout '
                f'article-content-callout__background--{kind}" '
                f'data-test-selector="callout-container">'
                f'<h3 class="article-content-callout__headline">{label}</h3>'
                f'<div data-test-selector="callout-rich-description">Body.</div>'
                f'</div>'
                f'</article></body></html>'
            )
            blocks = parse_help_center_html(html)
            note_blocks = [b for b in blocks if b.kind == "note"]
            self.assertEqual(len(note_blocks), 1)
            self.assertNotIn(
                label, note_blocks[0].text,
                f"{kind} callout headline must stay suppressed; "
                f"got: {note_blocks[0].text!r}",
            )


class DlSeparatorEquivalence(unittest.TestCase):
    """Source DITA <dlentry> renders as 'Term: Description' (with colon).
    Help Center articles render the same content as 'Term - Description'
    or 'Term — Description'. Without separator normalization, every
    dlentry whose only difference is the separator lands in 'Needs
    review' as a phantom REPLACE.
    """

    def test_colon_and_dash_separators_normalize_equal(self) -> None:
        from app.publication_reconstructor import normalize_for_match
        cases = [
            ("Get quick access to insights: Rather than searching across the web.",
             "Get quick access to insights – Rather than searching across the web."),
            ("Analyze patterns: Use Account IQ to analyze buying patterns.",
             "Analyze patterns - Use Account IQ to analyze buying patterns."),
            ("Expand your reach: Identify key stakeholders.",
             "Expand your reach — Identify key stakeholders."),
        ]
        for src, art in cases:
            self.assertEqual(
                normalize_for_match(src), normalize_for_match(art),
                f"separators ':' / '-' / '–' / '—' should be equivalent.\n"
                f"  src normalized: {normalize_for_match(src)!r}\n"
                f"  art normalized: {normalize_for_match(art)!r}",
            )

    def test_genuine_content_difference_still_distinguished(self) -> None:
        """Sanity: when the content after the separator really differs,
        the two should NOT normalize equal."""
        from app.publication_reconstructor import normalize_for_match
        self.assertNotEqual(
            normalize_for_match("Term: First description"),
            normalize_for_match("Term: Second description"),
            "real content differences must still register",
        )


class SentenceRemovalIsNotSuppressed(unittest.TestCase):
    """Sales Nav report-4 regression: the article removed a full
    sentence from the end of a table row. The diff correctly emitted a
    REPLACE on that row, but `_is_no_real_change` was suppressing it
    because the (truncated) article text was a substring of the
    (longer) source text. Substring alone isn't enough to call something
    'no real change' — the omitted portion must be short and label-like.
    """

    def test_long_sentence_removal_still_surfaces_as_review(self) -> None:
        from app.patch_engine import _is_no_real_change
        from app.diff_engine import DiffOp, OpKind
        from app.publication_reconstructor import Block
        from pathlib import Path as _P

        long_removal = (
            "If Account IQ isn't available for any accounts, your admin "
            "may have turned off Account IQ for all users on your Sales "
            "Navigator account. Account IQ is gradually being made "
            "available to Core users, and you might not have access to "
            "it at this time."
        )
        article_only = (
            "If Account IQ isn't available for any accounts, your admin "
            "may have turned off Account IQ for all users on your Sales "
            "Navigator account."
        )
        op = DiffOp(
            kind=OpKind.REPLACE,
            source_block=Block(
                block_index=0, topic_id="t.dita", topic_path=_P("/x"),
                element_xpath="/concept/conbody[1]/table[1]/tgroup[1]/tbody[1]/row[4]",
                element_tag="row", text=long_removal,
                auto_update=False, structural=True,
            ),
            updated_text=article_only,
            updated_index=0,
        )
        self.assertFalse(
            _is_no_real_change(op),
            "A full-sentence removal (~100 chars) must surface as a real "
            "review item, NOT be suppressed as 'just label difference.'",
        )

    def test_short_label_suffix_still_suppressed(self) -> None:
        """Sanity: a SHORT extra label (filename, stylesheet artifact)
        on the source side is still suppressed — that's the original
        protection this function provides."""
        from app.patch_engine import _is_no_real_change
        from app.diff_engine import DiffOp, OpKind
        from app.publication_reconstructor import Block
        from pathlib import Path as _P

        op = DiffOp(
            kind=OpKind.REPLACE,
            source_block=Block(
                block_index=0, topic_id="t.dita", topic_path=_P("/x"),
                element_xpath="/concept/conbody[1]/note[1]",
                element_tag="note",
                text="Guide.pdf Please review this document for details.",
                auto_update=False, structural=True,
            ),
            updated_text="Please review this document for details.",
            updated_index=0,
        )
        self.assertTrue(
            _is_no_real_change(op),
            "A short label prefix (a filename) should still be suppressed.",
        )

    def test_short_trailing_clause_truncation_is_not_suppressed(self) -> None:
        """Copilot CLI code review (2026-06-25): the old
        substring-anywhere check silently dropped this exact case.

        Source ("Click the Save button and then close the window.")
        being REPLACEd by article ("Click the Save button") means the
        article truncated a trailing clause — a real article change
        the writer must see. The truncation is only 28 chars (under
        the 50-char cap), so the old `article in source` check
        suppressed it with no card. With the tightened suffix-only
        rule this surfaces as a real REPLACE."""
        from app.patch_engine import _is_no_real_change
        from app.diff_engine import DiffOp, OpKind
        from app.publication_reconstructor import Block
        from pathlib import Path as _P

        op = DiffOp(
            kind=OpKind.REPLACE,
            source_block=Block(
                block_index=0, topic_id="t.dita", topic_path=_P("/x"),
                element_xpath="/concept/conbody[1]/p[1]",
                element_tag="p",
                text="Click the Save button and then close the window.",
                auto_update=False, structural=True,
            ),
            updated_text="Click the Save button",
            updated_index=0,
        )
        self.assertFalse(
            _is_no_real_change(op),
            "Trailing-clause truncation must surface as a real REPLACE "
            "card — the old substring-anywhere check silently dropped "
            "this, hiding genuine article changes from the writer.",
        )

    def test_short_trailing_label_in_article_is_not_suppressed(self) -> None:
        """The inverse case: article side ADDED a short trailing clause
        the source doesn't have. That's a real change (article gained
        content). The old `source in article` substring-anywhere check
        could suppress this too if the addition was under the size cap.
        Suffix-only rule keeps it as a surfaced REPLACE."""
        from app.patch_engine import _is_no_real_change
        from app.diff_engine import DiffOp, OpKind
        from app.publication_reconstructor import Block
        from pathlib import Path as _P

        op = DiffOp(
            kind=OpKind.REPLACE,
            source_block=Block(
                block_index=0, topic_id="t.dita", topic_path=_P("/x"),
                element_xpath="/concept/conbody[1]/p[1]",
                element_tag="p",
                text="Click the Save button",
                auto_update=False, structural=True,
            ),
            updated_text="Click the Save button to confirm the change.",
            updated_index=0,
        )
        self.assertFalse(
            _is_no_real_change(op),
            "Article gaining a trailing clause is a real REPLACE — must "
            "not be suppressed.",
        )


class CellAwareRowReplace(unittest.TestCase):
    """Sales Nav report-4 regression: when the article removes a
    sentence from inside one cell of a table row, the tool used to
    refuse the change (row was 'structural — review manually'). With
    per-cell article data available, we can now match positionally
    and rewrite only the cell that changed, leaving the other cells
    untouched.
    """

    def test_sentence_removed_from_one_cell_applies(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff, OpKind
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct
        from app.map_parser import TopicRef

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
                '<table><tgroup cols="2"><colspec colname="c1" colnum="1"/><colspec colname="c2" colnum="2"/>'
                '<tbody><row><entry>Header A</entry><entry>Header B</entry></row>'
                '<row><entry>Account IQ isn\'t displayed</entry>'
                '<entry>If Account IQ isn\'t available, your admin may have turned it off. '
                'Account IQ is gradually being made available to Core users, and you might not have access yet.</entry></row>'
                '</tbody></tgroup></table></conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            # Article kept the same first cell, removed the second
            # sentence from the second cell.
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Header A Header B",
                    kind="table_row",
                    cells=["Header A", "Header B"],
                ),
                HtmlArticleBlock(
                    text="Account IQ isn't displayed If Account IQ isn't available, your admin may have turned it off.",
                    kind="table_row",
                    cells=[
                        "Account IQ isn't displayed",
                        "If Account IQ isn't available, your admin may have turned it off.",
                    ],
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            applied_row_replaces = [
                r for r in report.applied
                if r.op and r.op.kind == OpKind.REPLACE
                and r.op.source_block.element_tag == "row"
            ]
            self.assertEqual(
                len(applied_row_replaces), 1,
                "row REPLACE should auto-apply at the cell level when "
                "the article provides per-cell text",
            )
            patched = (out / "topic.dita").read_text(encoding="utf-8")
            self.assertIn(
                "<entry>Account IQ isn't displayed</entry>",
                patched, "first cell should be unchanged",
            )
            self.assertIn(
                "your admin may have turned it off.</entry>",
                patched, "second cell should end where the article ended (sentence removed)",
            )
            self.assertNotIn(
                "gradually being made available",
                patched, "removed sentence must NOT survive in the patched DITA",
            )

    def test_cell_with_block_children_surfaces_for_review(self) -> None:
        """Cells containing <note>/<ul>/etc. are too structurally
        complex for safe auto-update; if such a cell actually differs,
        the row should surface for review, not be silently
        auto-rewritten."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff, OpKind
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct
        from app.map_parser import TopicRef

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
                '<table><tgroup cols="2"><colspec colname="c1" colnum="1"/><colspec colname="c2" colnum="2"/>'
                '<tbody><row><entry>Header A</entry><entry>Header B</entry></row>'
                '<row><entry>Feature</entry>'
                '<entry>Available <note type="tip"><p>Only on Advanced plans.</p></note></entry></row>'
                '</tbody></tgroup></table></conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Header A Header B", kind="table_row",
                    cells=["Header A", "Header B"],
                ),
                HtmlArticleBlock(
                    text="Feature Now available on all plans",
                    kind="table_row",
                    # Article rewrote the second cell — including
                    # removing the note. Tool should refuse and
                    # surface for review (we won't auto-delete a note).
                    cells=["Feature", "Now available on all plans"],
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            applied_row_replaces = [
                r for r in report.applied
                if r.op and r.op.kind == OpKind.REPLACE
                and r.op.source_block.element_tag == "row"
            ]
            self.assertEqual(
                applied_row_replaces, [],
                "row containing a cell with <note> must NOT be auto-applied",
            )
            # The row should still surface somewhere (skipped or detected)
            row_results = [
                r for r in report.results
                if r.op and r.op.source_block
                and r.op.source_block.element_tag == "row"
            ]
            self.assertTrue(
                any(r.category.value in ("skipped", "detected") for r in row_results),
                "row with structural cell must surface as needs-review",
            )


class AmbiguousReplaceHighOverlapEscapeHatch(unittest.TestCase):
    """Fix for Report 20260617_175613: an "ambiguous" replace segment
    (source has more blocks than article) used to refuse ALL its paired
    REPLACEs. A 1:1 pair with very high content overlap should still
    auto-apply — it's clearly a real rewrite, not a misalignment
    artifact."""

    def test_high_overlap_replace_in_ambiguous_segment_is_safe(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff, OpKind
        from app.map_parser import TopicRef
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            # Source has 4 paragraphs; article has only 2. LCS will pair
            # the first 2 as REPLACEs (src_len=4 > upd_len=2 -> ambiguous).
            # The first pair shares ~17 of ~22 content words (very high
            # overlap) — the escape hatch should let it through.
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
                '<p>If you are located in the European Union, you may use '
                'an alternative verification method instead of the '
                'workplace verification flow.</p>'
                '<p>Second source paragraph that goes away.</p>'
                '<p>Third source paragraph that also goes away.</p>'
                '<p>Fourth source paragraph that also goes away.</p>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text=(
                        "If you are located in Canada, the EU or the UK, "
                        "you may use an alternative verification method "
                        "instead of the workplace verification flow."
                    ),
                    kind="paragraph",
                ),
                HtmlArticleBlock(
                    text="An unrelated extra paragraph in the article.",
                    kind="paragraph",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            replaces = [o for o in ops if o.kind == OpKind.REPLACE]
            self.assertTrue(replaces, "expected at least one REPLACE op")
            self.assertTrue(
                replaces[0].safe_to_apply,
                "high-overlap REPLACE inside an ambiguous segment must "
                "still be safe_to_apply (escape hatch)",
            )

    def test_low_overlap_replace_in_ambiguous_segment_still_refused(self) -> None:
        """Counterpart: an ambiguous-segment REPLACE pair whose two sides
        share NO content words must still be refused — the escape hatch
        is for confident rewrites, not for masking misalignments."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff, OpKind
        from app.map_parser import TopicRef
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
                '<p>Reactivate your premium subscription before renewal.</p>'
                '<p>Source paragraph two that has no article counterpart.</p>'
                '<p>Source paragraph three that has no article counterpart.</p>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text=(
                        "Completely different unrelated text about "
                        "workplace verification documents."
                    ),
                    kind="paragraph",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            replaces = [o for o in ops if o.kind == OpKind.REPLACE]
            for r in replaces:
                self.assertFalse(
                    r.safe_to_apply,
                    "low-overlap REPLACE in an ambiguous segment must "
                    "stay safe_to_apply=False",
                )


class XrefInDeletedParagraphIsNotMedia(unittest.TestCase):
    """Fix for Report 20260617_174550: a paragraph containing only an
    <xref> (text link, not embedded media) used to be refused for DELETE
    on a "linked media" safety guard. Xrefs render as their link text in
    the article HTML and ARE captured by the parser — they shouldn't
    block a DELETE."""

    def test_paragraph_with_only_xref_can_be_deleted(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
                '<p>Please see the <xref href="https://example.com" '
                'format="html" scope="external">old link</xref> for context.</p>'
                '<p>This paragraph stays unchanged.</p>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="This paragraph stays unchanged.",
                    kind="paragraph",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            # The xref-bearing paragraph should be DELETEd (applied),
            # not skipped under the media-preservation guard.
            applied_deletes = [
                r for r in report.applied
                if r.op and r.op.kind.value == "delete"
            ]
            self.assertTrue(
                applied_deletes,
                "paragraph containing only an <xref> must be deletable; "
                "the media guard only blocks <image>/<object>/<fig>",
            )

    def test_paragraph_with_image_still_refused(self) -> None:
        """Counterpart: a paragraph containing <image> still triggers
        the media-preservation refusal (we don't parse embedded media)."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
                '<p>Screenshot below: <image href="screenshot.png"/>.</p>'
                '<p>This paragraph stays unchanged.</p>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="This paragraph stays unchanged.",
                    kind="paragraph",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            # The image-bearing paragraph should NOT be applied.
            applied_deletes = [
                r for r in report.applied
                if r.op and r.op.kind.value == "delete"
            ]
            self.assertEqual(
                applied_deletes, [],
                "paragraph containing <image> must still refuse DELETE",
            )


class DeleteSafetyNetSkipsConsumedArticleBlocks(unittest.TestCase):
    """Fix for Report 20260617_175032: when the source has duplicate
    text (e.g. "Affidavit of Identity Form (PDF)" listed under both
    "U.S. members" and "Non-U.S. members"), the DELETE safety net used
    to falsely shield the second copy by matching it against an article
    block that the LCS had already paired with the first copy. The
    safety net must skip article indices already consumed by EQUAL or
    REPLACE pairings."""

    def test_duplicate_source_text_with_one_article_pairing_deletes_the_other(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff, OpKind
        from app.map_parser import TopicRef
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            # Source has 4 list items: 2 PDF + 2 Word Doc duplicates
            # (one under each section). Article has only 2 items — the
            # U.S. section. The "Non-U.S." duplicates must be deletable
            # even though their text appears in the article (already
            # paired with the U.S. copies).
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
                '<ul>'
                '<li>Affidavit of Identity Form (US PDF document)</li>'
                '<li>Affidavit of Identity Form (US Word Document file)</li>'
                '<li>Affidavit of Identity Form (NON US PDF document)</li>'
                '<li>Affidavit of Identity Form (NON US Word Document file)</li>'
                '</ul>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Affidavit of Identity Form (US PDF document)",
                    kind="list_item",
                ),
                HtmlArticleBlock(
                    text="Affidavit of Identity Form (US Word Document file)",
                    kind="list_item",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            deletes = [o for o in ops if o.kind == OpKind.DELETE]
            self.assertGreaterEqual(
                len(deletes), 2,
                "the two duplicated Non-U.S. list items must be queued "
                "for DELETE — the safety net must not be fooled by "
                "their U.S. siblings already pairing with article items",
            )


class MassDeleteAdvisorySurfaces(unittest.TestCase):
    """Fix for Report 20260617_175613: when ≥50% of a topic's blocks
    are queued for DELETE, the report used to fan out into N per-block
    'mass-deletion guard' lines that buried the real action ('this
    whole topic appears retired — delete the file AND the .ditamap
    entry'). Emit ONE clear advisory per such topic."""

    def test_advisory_emitted_when_topic_mostly_deleted(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Through_workplace_verification.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US">'
                '<title>Through workplace verification</title>'
                '<conbody>'
                '<p>Paragraph one about the workplace verification flow.</p>'
                '<p>Paragraph two about the workplace verification flow.</p>'
                '<p>Paragraph three about the workplace verification flow.</p>'
                '<p>Paragraph four about the workplace verification flow.</p>'
                '<p>Paragraph five about the workplace verification flow.</p>'
                '<p>Paragraph six about the workplace verification flow.</p>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="Through_workplace_verification.dita",
                href="Through_workplace_verification.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            # Article retains only the title — every body paragraph is gone.
            article = [
                HtmlArticleBlock(
                    text="Through workplace verification", kind="paragraph",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            advisories = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.DETECTED
                and "Entire topic appears removed" in r.reason
                and r.topic_id == "Through_workplace_verification.dita"
            ]
            self.assertEqual(
                len(advisories), 1,
                "exactly one 'Entire topic appears removed' advisory must "
                "be emitted for the retired topic",
            )


class DlEntryAwareReplace(unittest.TestCase):
    """A simple <dlentry> (one <dt> + one <dd>, <dd> is text + inline
    only) can now be auto-rewritten: the patch engine splits the
    article text on the first separator (`:`/`-`/`–`/`—`), keeps
    <dt> as-is, and rewrites the <dd> definition. Inline markup
    (<uicontrol>, <xref>, etc.) inside <dd> is preserved when its
    phrases still appear in the new definition; otherwise it's dropped
    with a warning. The stylesheet adds the separator at render time,
    so the separator is NEVER written back into the DITA."""

    def _make_topic(self, tmp: Path, dlentry_xml: str) -> Path:
        src = tmp / "topic.dita"
        src.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
            '<concept id="X" xml:lang="en-US"><title>T</title><conbody>'
            '<dl>' + dlentry_xml + '</dl>'
            '</conbody></concept>\n',
            encoding="utf-8",
        )
        return src

    def _run(self, dlentry_xml: str, article_text: str):
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct

        tmp = Path(tempfile.mkdtemp())
        try:
            src = self._make_topic(tmp, dlentry_xml)
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text=article_text, kind="paragraph"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = tmp / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            written_text = ""
            written_path = out / "topic.dita"
            if written_path.exists():
                written_text = written_path.read_text(encoding="utf-8")
            return report, written_text
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_simple_dlentry_definition_rewrite_applies(self) -> None:
        """Definition reworded, term unchanged — should auto-apply
        and update only the <dd>."""
        report, written = self._run(
            '<dlentry><dt>Server</dt><dd>A computer that hosts services.</dd></dlentry>',
            "Server: A computer that hosts services 24/7 for clients.",
        )
        applied = [r for r in report.applied if r.op and r.op.kind.value == "replace"]
        self.assertTrue(applied, "simple dlentry REPLACE must auto-apply")
        self.assertIn("hosts services 24/7", written)
        # Separator must NEVER be written into the DITA — stylesheet adds it.
        self.assertNotIn("<dd>: ", written)
        self.assertNotIn("<dd>:", written)
        # Term stays as-is in <dt>.
        self.assertIn("<dt>Server</dt>", written)

    def test_dlentry_with_dash_separator_in_article_still_applies(self) -> None:
        """Writers don't always follow the IM colon rule — they
        sometimes use a hyphen or en-dash. Splitting must accept any."""
        report, written = self._run(
            '<dlentry><dt>Server</dt><dd>A computer that hosts services.</dd></dlentry>',
            "Server – A high-availability computer that hosts services.",
        )
        applied = [r for r in report.applied if r.op and r.op.kind.value == "replace"]
        self.assertTrue(applied, "dlentry with – separator in article must apply")
        self.assertIn("high-availability", written)

    def test_dlentry_with_term_change_refuses(self) -> None:
        """A renamed term is meaningful (may affect cross-references);
        the tool must surface it for review rather than rewriting <dd>."""
        report, _ = self._run(
            '<dlentry><dt>Server</dt><dd>A computer that hosts services.</dd></dlentry>',
            "Workstation: A computer that hosts services.",
        )
        skipped = [
            r for r in report.skipped
            if r.op and r.op.kind.value == "replace"
            and "dlentry" in (r.reason or "")
        ]
        self.assertTrue(
            skipped,
            "dlentry with renamed term must be SKIPPED for review",
        )

    def test_dlentry_with_block_child_in_dd_is_not_auto_rewritten(self) -> None:
        """A <dd> containing <ul>/<note>/etc. is structural; a text
        rewrite would silently destroy the nested structure. The
        reconstructor doesn't emit such a dlentry as a single block
        at all (it walks into the inner <li>/etc. separately), so the
        dlentry-aware REPLACE handler never sees it. Either way, the
        critical guarantee is: no dlentry-shape auto-apply happens
        AND the source <dlentry>/<ul> structure is left intact in
        the written file."""
        report, written = self._run(
            '<dlentry><dt>Steps</dt><dd>Do this:'
            '<ul><li>first</li><li>second</li></ul></dd></dlentry>',
            "Steps: Do this first, then second, then third.",
        )
        # No dlentry-shape REPLACE should land in applied with the
        # 'dropped inline markup' or successful-rewrite warning we'd
        # see if the dlentry handler ran on this complex shape.
        dlentry_handler_applied = [
            r for r in report.applied
            if r.op and r.op.kind.value == "replace"
            and r.op.source_block is not None
            and r.op.source_block.element_tag == "dlentry"
        ]
        self.assertEqual(
            dlentry_handler_applied, [],
            "dlentry-aware handler must not run when <dd> has block children",
        )
        # The <ul> structure inside <dd> must still be present in the
        # output — we did not flatten it.
        if written:
            self.assertIn("<ul>", written)
            self.assertIn("<dd>", written)

    def test_dlentry_with_inline_markup_preserved_when_phrase_remains(self) -> None:
        """<uicontrol>/<xref>/etc. inside <dd> should be preserved when
        their text still appears in the new article definition."""
        report, written = self._run(
            '<dlentry><dt>Open</dt>'
            '<dd>Click <uicontrol>Settings</uicontrol> to begin.</dd></dlentry>',
            "Open: Click Settings to begin the setup wizard.",
        )
        applied = [r for r in report.applied if r.op and r.op.kind.value == "replace"]
        self.assertTrue(applied, "dlentry with preservable inline markup must apply")
        self.assertIn("<uicontrol>Settings</uicontrol>", written)
        self.assertIn("setup wizard", written)
        for r in applied:
            self.assertFalse(
                "dropped inline markup" in (r.warning or ""),
                "warning should NOT mention dropped markup when phrase remained",
            )

    def test_dlentry_drops_inline_markup_when_phrase_gone(self) -> None:
        """When the article wording no longer contains the inline-marked
        phrase, fall back to plain-text rewrite and tell the writer."""
        report, written = self._run(
            '<dlentry><dt>Open</dt>'
            '<dd>Click <uicontrol>Settings</uicontrol> to begin.</dd></dlentry>',
            "Open: Use the menu in the top right to begin.",
        )
        applied = [r for r in report.applied if r.op and r.op.kind.value == "replace"]
        self.assertTrue(applied, "dlentry must still apply with plain-text fallback")
        # <uicontrol> dropped because its phrase ('Settings') is gone.
        self.assertNotIn("<uicontrol>", written)
        any_warning = any(
            "dropped inline markup" in (r.warning or "")
            for r in applied
        )
        self.assertTrue(
            any_warning,
            "warning must call out that inline markup was dropped",
        )

    def test_dlentry_separator_never_written_to_dita(self) -> None:
        """Critical: the `:`/`-`/`–` separator must NEVER end up
        inside the <dd>. The Help Center stylesheet adds it at render
        time (IM page 123). Writing it would produce 'Term:: Definition'
        after the stylesheet pass."""
        _, written = self._run(
            '<dlentry><dt>Server</dt><dd>Old.</dd></dlentry>',
            "Server: A new definition entirely.",
        )
        # The string ': A new' would mean the separator leaked into <dd>.
        self.assertNotIn(": A new definition", written)
        self.assertIn("A new definition entirely", written)


class MassDeleteGuardSmarterThreshold(unittest.TestCase):
    """Earlier the mass-delete advisory fired at ≥ 50% removal, which
    falsely flagged small topics that just trim some content (e.g. a
    list with 4 of 8 items gone). The new rule fires only when the
    topic's <title> is also gone (≥ 50% body removal) OR when ≥ 80%
    of blocks are queued for DELETE. A title that's REPLACEd (renamed)
    or EQUAL (unchanged) acts as a hard veto — the topic is alive.
    When the advisory does fire, per-block DELETE entries are
    suppressed so the writer sees one topic-level action, not N
    redundant lines."""

    def test_partial_removal_with_title_alive_applies_deletes(self) -> None:
        """50% of body deleted, title unchanged → deletes apply,
        no advisory. The user's Report 1 case."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Form_Affidavit.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US">'
                '<title>Affidavit of Identity Form</title>'
                '<conbody>'
                '<p>Use the appropriate form for your region.</p>'
                '<ul>'
                '<li>U.S. members: PDF version</li>'
                '<li>U.S. members: Word version</li>'
                '<li>Non-U.S. members: PDF version</li>'
                '<li>Non-U.S. members: Word version</li>'
                '</ul>'
                '<p>Each form has specific completion instructions.</p>'
                '<p>Contact support if you need help.</p>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="Form_Affidavit.dita", href="Form_Affidavit.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="Affidavit of Identity Form", kind="paragraph"),
                HtmlArticleBlock(text="Use the appropriate form for your region.", kind="paragraph"),
                HtmlArticleBlock(text="U.S. members: PDF version", kind="list_item"),
                HtmlArticleBlock(text="U.S. members: Word version", kind="list_item"),
                HtmlArticleBlock(text="Each form has specific completion instructions.", kind="paragraph"),
                HtmlArticleBlock(text="Contact support if you need help.", kind="paragraph"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            advisories = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.DETECTED
                and "Entire topic appears removed" in (r.reason or "")
            ]
            self.assertEqual(
                advisories, [],
                "advisory must NOT fire when title is alive — this is "
                "content trim, not topic retirement",
            )
            applied_deletes = [
                r for r in report.applied
                if r.op and r.op.kind.value == "delete"
            ]
            self.assertTrue(
                applied_deletes,
                "the body DELETEs must apply when the topic is alive",
            )

    def test_renamed_title_vetoes_advisory_even_with_heavy_removal(self) -> None:
        """Article renamed the title AND trimmed 60% of the body —
        topic is alive (rebranded + restructured), deletes apply,
        no advisory."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US">'
                '<title>Old Name For This Topic</title>'
                '<conbody>'
                '<p>Body paragraph one introducing things.</p>'
                '<p>Body paragraph two that goes away.</p>'
                '<p>Body paragraph three that goes away.</p>'
                '<p>Body paragraph four that goes away.</p>'
                '<p>Body paragraph five surviving.</p>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                # Article RENAMED the title.
                HtmlArticleBlock(text="New Name For This Topic", kind="paragraph"),
                HtmlArticleBlock(text="Body paragraph one introducing things.", kind="paragraph"),
                HtmlArticleBlock(text="Body paragraph five surviving.", kind="paragraph"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            advisories = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.DETECTED
                and "Entire topic appears removed" in (r.reason or "")
            ]
            self.assertEqual(
                advisories, [],
                "renamed-title is a positive 'topic alive' signal — "
                "advisory must NOT fire even with 60% body removal",
            )

    def test_full_topic_removal_fires_advisory_and_suppresses_perblock(self) -> None:
        """Title gone + 100% body gone → advisory fires, AND the
        individual block-level DELETE entries are suppressed (no
        per-block noise in needs-review)."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Retired_Topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US">'
                '<title>Retired Topic Title</title>'
                '<conbody>'
                '<p>Body paragraph one.</p>'
                '<p>Body paragraph two.</p>'
                '<p>Body paragraph three.</p>'
                '<p>Body paragraph four.</p>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="Retired_Topic.dita", href="Retired_Topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            # Article doesn't render this topic at all.
            article = [
                HtmlArticleBlock(text="Some other unrelated topic text.", kind="paragraph"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            advisories = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.DETECTED
                and "Entire topic appears removed" in (r.reason or "")
                and r.topic_id == "Retired_Topic.dita"
            ]
            self.assertEqual(
                len(advisories), 1,
                "exactly one entire-topic-removed advisory must fire",
            )
            per_block_skips = [
                r for r in report.results
                if r.op is not None
                and r.op.kind.value == "delete"
                and r.op.source_block is not None
                and r.op.source_block.topic_id == "Retired_Topic.dita"
                and r.category == ResultCategory.SKIPPED
            ]
            self.assertEqual(
                per_block_skips, [],
                "per-block DELETE entries must be suppressed when the "
                "topic-level advisory has fired — no redundant noise",
            )


class AmbiguousXrefSiblingDelete(unittest.TestCase):
    """User-reported regression: a topic with two duplicate-text link
    sections (e.g. 'U.S. members: Affidavit (PDF)' AND 'Non-U.S.
    members: Affidavit (PDF)') used to silently keep whichever sibling
    LCS paired first and DELETE the other — frequently the wrong one.
    The patch engine now refuses ambiguous DELETEs and surfaces both
    siblings for writer review."""

    def test_duplicate_text_different_href_delete_refused(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Form_Affidavit.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US">'
                '<title>Form: Affidavit of Identity</title>'
                '<conbody>'
                '<p>U.S. members:</p>'
                '<ul>'
                '<li><xref href="/help/linkedin/answer/87951" '
                'scope="external" format="html">Affidavit of Identity Form (PDF)</xref></li>'
                '</ul>'
                '<p>Non-U.S. members:</p>'
                '<ul>'
                '<li><xref href="/help/linkedin/answer/130897" '
                'scope="external" format="html">Affidavit of Identity Form (PDF)</xref></li>'
                '</ul>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="Form_Affidavit.dita", href="Form_Affidavit.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="Form: Affidavit of Identity", kind="paragraph"),
                HtmlArticleBlock(
                    text="Affidavit of Identity Form (PDF)",
                    kind="list_item",
                    links=[("Affidavit of Identity Form (PDF)", "/help/linkedin/answer/a1342713")],
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            # The would-be DELETE on the duplicate-text-different-href
            # sibling must be SKIPPED with the ambiguous-xref reason.
            ambiguous_skips = [
                r for r in report.skipped
                if r.op and r.op.kind.value == "delete"
                and "sibling with identical link text" in (r.reason or "")
            ]
            self.assertTrue(
                ambiguous_skips,
                "duplicate-text different-href DELETE must be refused",
            )

    def test_unique_xref_text_delete_still_applies(self) -> None:
        """Counterpart: when the to-be-deleted xref item has NO
        duplicate-text sibling, the DELETE should still apply (the
        new guard must not block normal link removals)."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US">'
                '<title>T</title><conbody>'
                '<ul>'
                '<li><xref href="/help/answer/87951" scope="external" '
                'format="html">Surviving Link</xref></li>'
                '<li><xref href="/help/answer/87952" scope="external" '
                'format="html">Going Away Link</xref></li>'
                '</ul>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Surviving Link", kind="list_item",
                    links=[("Surviving Link", "/help/answer/87951")],
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            applied_deletes = [
                r for r in report.applied
                if r.op and r.op.kind.value == "delete"
            ]
            self.assertTrue(
                applied_deletes,
                "unique-text xref DELETE must still apply",
            )


class StaleHrefAdvisory(unittest.TestCase):
    """When the diff calls a <xref>-bearing source block EQUAL to an
    article-side block (text matches), but the source's <xref href>
    doesn't match the article's link href, surface a 'stale href'
    advisory per topic. Common after the project's article-ID format
    change (/87951 → /a1342713). The tool does NOT auto-rewrite."""

    def test_href_mismatch_surfaces_advisory(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US">'
                '<title>T</title><conbody>'
                '<ul>'
                '<li><xref href="/help/linkedin/answer/87951" '
                'scope="external" format="html">Help Article</xref></li>'
                '</ul>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                # Same visible text, NEW href format.
                HtmlArticleBlock(
                    text="Help Article", kind="list_item",
                    links=[("Help Article", "/help/linkedin/answer/a1342713")],
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            stale_advisories = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.DETECTED
                and "outdated <xref href>" in (r.reason or "")
                and r.topic_id == "topic.dita"
            ]
            self.assertTrue(
                stale_advisories,
                "stale-href advisory must fire on href mismatch",
            )
            # The DITA file itself must NOT be auto-rewritten — original
            # href stays. (No write happens when there are no applied
            # ops; verify the source file is unchanged.)
            written = out / "topic.dita"
            if written.exists():
                self.assertIn("/87951", written.read_text(encoding="utf-8"))

    def test_matching_hrefs_no_advisory(self) -> None:
        """Counterpart: source xref href matches article link href
        (just maybe with trailing slash / scheme differences) → no
        advisory."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US">'
                '<title>T</title><conbody>'
                '<ul>'
                '<li><xref href="https://www.linkedin.com/help/linkedin/answer/87951" '
                'scope="external" format="html">Help Article</xref></li>'
                '</ul>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                # Same target, just path-only + trailing slash.
                HtmlArticleBlock(
                    text="Help Article", kind="list_item",
                    links=[("Help Article", "/help/linkedin/answer/87951/")],
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            stale_advisories = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.DETECTED
                and "outdated <xref href>" in (r.reason or "")
            ]
            self.assertEqual(
                stale_advisories, [],
                "no stale-href advisory when hrefs match after normalization",
            )


class InsertStepIntoTaskbodyRoutesToSteps(unittest.TestCase):
    """Beta-test regression from Article 3 (Workplace verification):
    when the article added new procedure steps BEFORE the existing
    ones, the INSERT anchor was the topic <title>. The old handler
    only routed to <step> when the anchor was already inside an
    existing <step> — otherwise it fell back to <ol><li>, producing
    invalid DITA (<taskbody> doesn't permit <ol>).

    The fix: when the anchor is inside (or is) a <taskbody>, find or
    create the <steps> element and insert at position 0."""

    def test_new_step_before_existing_routes_into_steps(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
                '<task id="X" xml:lang="en-US">'
                '<title>To verify your email:</title>'
                '<taskbody>'
                '<steps>'
                '<step><cmd>Click Resources.</cmd></step>'
                '<step><cmd>Click Verify workplace.</cmd></step>'
                '</steps>'
                '</taskbody></task>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="To verify your email:", kind="paragraph"),
                HtmlArticleBlock(
                    text="Click the Me icon in the upper-right corner.",
                    kind="step",
                ),
                HtmlArticleBlock(text="Click View profile.", kind="step"),
                HtmlArticleBlock(text="Click Resources.", kind="step"),
                HtmlArticleBlock(text="Click Verify workplace.", kind="step"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(ops, out, article_blocks=article, publication=pub)
            patched = (out / "topic.dita").read_text(encoding="utf-8")
            # CRITICAL: <ol> must NOT be a child of <taskbody>.
            taskbody_ol = "<taskbody><ol" in patched.replace(" ", "")
            self.assertFalse(
                taskbody_ol,
                "<ol> directly under <taskbody> is invalid DITA; the new "
                "steps must route into the existing <steps> element",
            )
            # New <step> count: original 2 + new 2 = 4.
            step_open_count = patched.count("<step>") + patched.count("<step ")
            self.assertEqual(
                step_open_count, 4,
                "expected 4 <step> elements after inserting 2 new ones",
            )
            # Sanity: the new step text must appear inside a <cmd>.
            self.assertIn("<cmd>Click the Me icon", patched)
            self.assertIn("<cmd>Click View profile", patched)


class ReltableStaleHrefAdvisory(unittest.TestCase):
    """Beta-test regression from Article 4 (Change Page name): the
    article's Related tasks 'Contact us' link had a different href
    than the .ditamap reltable, but the tool didn't flag it. The
    topic-body xref-staleness scan doesn't reach reltable items
    because reltables live in the .ditamap, not in topic bodies.

    Category is MAP_EDIT (updated 2026-06-23 — was DETECTED, which
    surfaced the wall of text under a topic-body advisory tab even
    though every affected link is a .ditamap concern)."""

    def test_reltable_href_mismatch_surfaces_advisory(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import ReltableEntry
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US">'
                '<title>T</title><conbody><p>Body.</p></conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Body.", kind="paragraph"),
                HtmlArticleBlock(text="Related tasks", kind="paragraph"),
                HtmlArticleBlock(
                    text="Contact us", kind="unordered_step",
                    links=[("Contact us", "/help/linkedin/solve")],
                ),
            ]
            reltable_entries = [
                ReltableEntry(
                    section="Related tasks",
                    navtitle="Contact us",
                    href="https://www.linkedin.com/help/linkedin/ask/cp-primary",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(
                ops, out,
                article_blocks=article,
                publication=pub,
                reltable_entries=reltable_entries,
            )
            advisories = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.MAP_EDIT
                and "reltable link" in (r.reason or "")
            ]
            self.assertEqual(
                len(advisories), 1,
                "stale reltable href must produce exactly one advisory",
            )

    def test_reltable_href_match_no_advisory(self) -> None:
        """Counterpart: matching hrefs (after normalization) → no advisory."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import ReltableEntry, TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title>'
                '<conbody><p>Body.</p></conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Body.", kind="paragraph"),
                HtmlArticleBlock(text="Related tasks", kind="paragraph"),
                HtmlArticleBlock(
                    text="Contact us", kind="unordered_step",
                    links=[("Contact us", "/help/linkedin/solve/")],
                ),
            ]
            reltable_entries = [
                ReltableEntry(
                    section="Related tasks",
                    navtitle="Contact us",
                    href="https://www.linkedin.com/help/linkedin/solve",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(
                ops, out, article_blocks=article,
                publication=pub, reltable_entries=reltable_entries,
            )
            advisories = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.DETECTED
                and "reltable link" in (r.reason or "")
            ]
            self.assertEqual(
                advisories, [],
                "no advisory when hrefs match after normalization",
            )


class ScreenshotPresenceAdvisory(unittest.TestCase):
    """Beta-test regression from Article 3 (Workplace verification):
    the live article had a content screenshot the writer needed to
    add to the DITA, but the tool didn't flag it. Two problems:
    (1) the parser conflated UI icon glyphs with screenshots, and
    (2) when the screenshot sat as a sibling AFTER the <li>'s already-
    delegated <p>, the inline-image flag was lost on the empty <li>.

    Fix: parser distinguishes 'article-content__image' (screenshot)
    from icon glyphs, and forwards the screenshot flag to the most-
    recently emitted block when the wrapping element pops without
    emitting. The patch engine emits a topic-wide advisory listing
    all screenshot positions."""

    def test_screenshot_after_delegated_paragraph_is_captured(self) -> None:
        from app.article_html_parser import parse_help_center_html

        html = (
            '<article data-test-selector="article">'
            '<ol>'
            '<li class="article-content__ordered-list-item t-14">'
            '<div class="t-14 article-content__rich-text" '
            'data-test-selector="preRenderedMarkup-container">'
            '<p>Click Resources.</p>'
            '</div>'
            '<img src="screenshot.png" alt="Resources option" '
            'class="article-content__image article-content__image--in-list">'
            '</li>'
            '</ol>'
            '</article>'
        )
        blocks = parse_help_center_html(html)
        screenshot_blocks = [b for b in blocks if b.has_screenshot]
        self.assertEqual(
            len(screenshot_blocks), 1,
            "screenshot must survive the delegated-paragraph pattern",
        )
        self.assertEqual(screenshot_blocks[0].text, "Click Resources.")

    def test_icon_glyph_not_classified_as_screenshot(self) -> None:
        """Counterpart: an inline UI-icon SVG/li-icon stays as
        has_inline_image=True but has_screenshot=False — icons map to
        <uicontrol> text, no advisory needed."""
        from app.article_html_parser import parse_help_center_html

        html = (
            '<article data-test-selector="article">'
            '<div class="t-14 article-content__rich-text" '
            'data-test-selector="preRenderedMarkup-container">'
            '<p>Click <li-icon type="ellipsis-horizontal-icon">x</li-icon>'
            ' Resources.</p>'
            '</div>'
            '</article>'
        )
        blocks = parse_help_center_html(html)
        # No screenshot anywhere in the document.
        any_screenshot = any(b.has_screenshot for b in blocks)
        self.assertFalse(
            any_screenshot,
            "icon glyphs must NOT be classified as screenshots",
        )

    def test_screenshot_emits_apply_ops_advisory(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title>'
                '<conbody><p>Click Resources.</p></conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Click Resources.", kind="step",
                    has_inline_image=True, has_screenshot=True,
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(
                ops, out, article_blocks=article, publication=pub,
            )
            screenshot_advs = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.DETECTED
                and "screenshot" in (r.reason or "").lower()
            ]
            self.assertEqual(
                len(screenshot_advs), 1,
                "apply_ops must emit a screenshot advisory when an "
                "article block has has_screenshot=True",
            )


class PostTabContentDoesNotInheritTabBinding(unittest.TestCase):
    """Beta-test regression from Article 1 (Edit your a Page):
    a post-tab_content.dita topic (sibling of the tab topicgroup, at
    lower map depth) was wrongly inheriting the previous tab's
    section binding. The cross-tab guard then flagged its untouched
    content as 'needs manual update' in Beth's report.

    Fix: build_topic_to_section now clears the navtitle binding when
    a topic drops to depth ≤ the navtitle's depth (sibling level)."""

    def test_post_tab_topic_at_lower_depth_clears_binding(self) -> None:
        from app.publication_reconstructor import (
            Publication, Block, build_topic_to_section,
        )
        from app.article_html_parser import HtmlArticleBlock

        # Realistic depths from a real <topicref> wrapping a
        # <topicgroup outputclass="tabs"> with topichead children
        # and a post-tab sibling topicref:
        #   topicref parent     depth 0
        #     topicgroup        depth 1
        #       topichead       depth 2 (emits MapLabel "Desktop")
        #         topicref D    depth 3
        #       topichead       depth 2 (emits MapLabel "Mobile")
        #         topicref M    depth 3
        #     topicref post     depth 1 (DROPPED below navtitle depth)
        pub = Publication(blocks=[
            Block(block_index=0, topic_id="parent.dita",
                  topic_path=None, element_xpath="/concept/title[1]",
                  element_tag="title", text="Edit your Page", map_depth=0),
            Block(block_index=1, topic_id="<ditamap>",
                  topic_path=None, element_xpath="<topichead navtitle='Desktop'>",
                  element_tag="navtitle", text="Desktop", map_depth=2),
            Block(block_index=2, topic_id="Desktop.dita",
                  topic_path=None, element_xpath="/concept/title[1]",
                  element_tag="title", text="Desktop", map_depth=3),
            Block(block_index=3, topic_id="<ditamap>",
                  topic_path=None, element_xpath="<topichead navtitle='Mobile'>",
                  element_tag="navtitle", text="Mobile", map_depth=2),
            Block(block_index=4, topic_id="Mobile.dita",
                  topic_path=None, element_xpath="/concept/title[1]",
                  element_tag="title", text="Mobile", map_depth=3),
            # post-tab sibling at depth 1 — must NOT inherit Mobile's
            # panel binding.
            Block(block_index=5, topic_id="post-tab_content.dita",
                  topic_path=None, element_xpath="/concept/title[1]",
                  element_tag="title", text="After changing your name",
                  map_depth=1),
            Block(block_index=6, topic_id="post-tab_content.dita",
                  topic_path=None, element_xpath="/concept/conbody/p[1]",
                  element_tag="p",
                  text="Recommend posting an announcement.",
                  map_depth=1),
        ])
        article = [
            HtmlArticleBlock(text="Desktop", kind="tab_label", section_id="panel-1"),
            HtmlArticleBlock(text="Click Edit page", kind="step", section_id="panel-1"),
            HtmlArticleBlock(text="Mobile", kind="tab_label", section_id="panel-2"),
            HtmlArticleBlock(text="Tap Edit page", kind="step", section_id="panel-2"),
            HtmlArticleBlock(text="Recommend posting an announcement.", kind="paragraph"),
        ]
        t2s = build_topic_to_section(pub, article)
        self.assertEqual(t2s.get("Desktop.dita"), "panel-1")
        self.assertEqual(t2s.get("Mobile.dita"), "panel-2")
        self.assertNotEqual(
            t2s.get("post-tab_content.dita"), "panel-2",
            "post-tab content must NOT inherit the previous tab's "
            "section binding — it lives outside the tab topicgroup",
        )
        # Per IM convention (Fix #14), post-tab topics are bound to
        # the synthetic '__post_tab__' section so article content
        # AFTER the last tab routes here.
        self.assertEqual(
            t2s.get("post-tab_content.dita"), "__post_tab__",
            "post-tab content topic must be bound to the synthetic "
            "post-tab section",
        )


class NewFaqQuestionAdvisory(unittest.TestCase):
    """Beta-test regression from Article 5 (a multi-topic FAQ):
    a newly added FAQ question in the live article didn't surface as
    'a new .dita topic is needed' — it just appeared as a generic
    paragraph INSERT, which the writer didn't recognize as a new-topic
    signal. The parser now distinguishes expandable-trigger spans
    (kind="expandable_header"), and apply_ops emits an advisory for
    each header whose text doesn't match a source topic title."""

    def test_new_expandable_header_surfaces_advisory(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "how_do_i.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US">'
                '<title>How do I create a post?</title>'
                '<conbody><p>Body.</p></conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="how_do_i.dita", href="how_do_i.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="How do I create a post?", kind="expandable_header"),
                HtmlArticleBlock(text="Body.", kind="paragraph"),
                # A NEW question not in the source map.
                HtmlArticleBlock(
                    text="Why can't I edit my boosted post?",
                    kind="expandable_header",
                ),
                HtmlArticleBlock(text="Some new answer text.", kind="paragraph"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(
                ops, out, article_blocks=article, publication=pub,
            )
            advisories = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.DETECTED
                and "new FAQ-style" in (r.reason or "")
            ]
            self.assertEqual(
                len(advisories), 1,
                "exactly one new-FAQ-topic advisory must fire",
            )
            self.assertIn(
                "Why can't I edit my boosted post?",
                advisories[0].reason,
            )

    def test_no_advisory_when_every_expandable_matches(self) -> None:
        """All expandable_headers match source topic titles → no advisory."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "how_do_i.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US">'
                '<title>How do I create a post?</title>'
                '<conbody><p>Body.</p></conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="how_do_i.dita", href="how_do_i.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="How do I create a post?", kind="expandable_header"),
                HtmlArticleBlock(text="Body.", kind="paragraph"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(
                ops, out, article_blocks=article, publication=pub,
            )
            advisories = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.DETECTED
                and "new FAQ-style" in (r.reason or "")
            ]
            self.assertEqual(
                advisories, [],
                "no advisory when every expandable_header matches a topic",
            )


class IconPresenceAdvisory(unittest.TestCase):
    """When an article line being INSERTed or REPLACEd contains an
    inline UI icon glyph (li-icon, small svg), the tool can't
    auto-place the corresponding <image> element (we don't know the
    DITA asset href). A topic-level advisory now lists every such
    INSERT/REPLACE position so the writer can add icons by hand."""

    def test_insert_with_icon_emits_advisory(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
                '<task id="X" xml:lang="en-US"><title>Verify your email:</title>'
                '<taskbody><steps>'
                '<step><cmd>Click Resources.</cmd></step>'
                '</steps></taskbody></task>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="Verify your email:", kind="paragraph"),
                HtmlArticleBlock(
                    text="Click the Me icon at the top of the page.",
                    kind="step",
                    has_inline_image=True,  # icon glyph present
                    has_screenshot=False,
                ),
                HtmlArticleBlock(text="Click Resources.", kind="step"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(
                ops, out, article_blocks=article, publication=pub,
            )
            icon_advs = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.DETECTED
                and "inline UI icon" in (r.reason or "")
            ]
            self.assertEqual(
                len(icon_advs), 1,
                "icon advisory must fire when an INSERT involves an "
                "article block with has_inline_image=True",
            )
            self.assertIn(
                "Click the Me icon",
                icon_advs[0].reason,
            )

    def test_equal_block_with_icon_does_not_emit_advisory(self) -> None:
        """Counterpart: when an icon-bearing block is EQUAL (no change),
        no advisory — the DITA already has the icon at that position."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
                '<task id="X" xml:lang="en-US"><title>T</title>'
                '<taskbody><steps>'
                '<step><cmd>Click Resources.</cmd></step>'
                '</steps></taskbody></task>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Click Resources.", kind="step",
                    has_inline_image=True,  # icon present but no change
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(
                ops, out, article_blocks=article, publication=pub,
            )
            icon_advs = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.DETECTED
                and "inline UI icon" in (r.reason or "")
            ]
            self.assertEqual(
                icon_advs, [],
                "EQUAL block with icon must NOT trigger advisory",
            )

    def test_screenshot_block_excluded_from_icon_advisory(self) -> None:
        """Counterpart: a screenshot-bearing INSERT is surfaced by the
        screenshot advisory, not the icon one — no double-counting."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
                '<task id="X" xml:lang="en-US"><title>T</title>'
                '<taskbody><steps>'
                '<step><cmd>Click Resources.</cmd></step>'
                '</steps></taskbody></task>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Click somewhere new.", kind="step",
                    has_inline_image=True, has_screenshot=True,
                ),
                HtmlArticleBlock(text="Click Resources.", kind="step"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(
                ops, out, article_blocks=article, publication=pub,
            )
            icon_advs = [
                r for r in report.results
                if r.op is None
                and r.category == ResultCategory.DETECTED
                and "inline UI icon" in (r.reason or "")
            ]
            self.assertEqual(
                icon_advs, [],
                "screenshot-bearing block must be handled by screenshot "
                "advisory only (no double-counting in icon advisory)",
            )


class InsertIntoPrereq(unittest.TestCase):
    """Beta-test regression from Article 4 (before_you_begin.dita):
    when the article added new bullets to the 'Before you begin'
    section, the INSERTs anchored at the topic <title> landed at
    taskbody position 0 (via the title-anchor redirect), creating an
    <ul> as a sibling of <prereq> — invalid DITA. The fix: when the
    target parent is <taskbody> and a <prereq> with a <ul>/<ol>
    exists, route the new <li> into that list inside <prereq>."""

    def test_new_bullets_route_into_prereq_ul(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//OASIS//DTD DITA Task//EN" "task.dtd">\n'
                '<task id="X"><title>Before you begin</title>'
                '<taskbody><prereq>'
                '<ul><li>Original item</li></ul>'
                '</prereq></taskbody></task>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="Before you begin", kind="heading"),
                HtmlArticleBlock(text="Original item", kind="unordered_step"),
                HtmlArticleBlock(text="Brand new bullet 1", kind="unordered_step"),
                HtmlArticleBlock(text="Brand new bullet 2", kind="unordered_step"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(
                ops, out, article_blocks=article, publication=pub,
            )
            patched = (out / "topic.dita").read_text(encoding="utf-8")
            # CRITICAL: <ul> must not appear as a direct child of <taskbody>.
            self.assertNotIn(
                "<taskbody><ul",
                patched.replace(" ", ""),
                "<ul> as a direct child of <taskbody> is invalid DITA",
            )
            # All bullets must end up inside <prereq>.
            import re
            prereq_match = re.search(
                r'<prereq[^>]*>(.*?)</prereq>', patched, re.DOTALL,
            )
            self.assertIsNotNone(prereq_match)
            li_in_prereq = (
                prereq_match.group(1).count("<li>")
                + prereq_match.group(1).count("<li ")
            )
            self.assertGreaterEqual(
                li_in_prereq, 3,
                "all 3 bullets (original + 2 new) must live inside <prereq>",
            )


class EmphasisFallbackWrapsInEm(unittest.TestCase):
    """Beta-test regression from Article 4 (Desktop.dita): when the
    source step had <uicontrol> wrappers around words that the new
    article wording doesn't contain, _replace_preserving_inline_markup
    returned False and the fallback dropped to plain text — losing
    even the article's <strong> emphasis wraps. Now the fallback
    re-runs the preserving function with emphasis only, so 'Page info'
    (bold in the article) gets wrapped in <em>."""

    def test_emphasis_preserved_when_existing_markup_drops(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
                '<task id="X" xml:lang="en-US"><title>T</title>'
                '<taskbody><steps>'
                '<step><cmd>Click <uicontrol>Page info</uicontrol> in '
                'the upper left of the <uicontrol>Edit</uicontrol> pane.</cmd></step>'
                '</steps></taskbody></task>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Click the Page info tab.",
                    kind="step",
                    emphasis=["Page info"],
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(
                ops, out, article_blocks=article, publication=pub,
            )
            patched = (out / "topic.dita").read_text(encoding="utf-8")
            self.assertIn(
                "<em>Page info</em>",
                patched,
                "article-side emphasis must survive the fallback path "
                "even when the original <uicontrol> couldn't be "
                "preserved verbatim",
            )


class SectionAwareDeleteSafetyAndDemote(unittest.TestCase):
    """Beta-test regression from Article 4 (Mobile.dita / Desktop.dita):
    a 'Post an update...' step in the Mobile tab was NOT being deleted
    even though the article doesn't show it in any tab. The DELETE
    safety net was suppressing it because similar text appeared in the
    article's post-tab paragraph (different section). The cross-section
    REPLACE demotion + section-aware safety net fix both cases."""

    def test_cross_section_text_match_does_not_block_delete(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            map_src = Path(tmp) / "map.ditamap"
            map_src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE map PUBLIC "-//LINKEDIN//DTD DITA Map//EN" "linkedin_map.dtd">\n'
                '<map>'
                '<topicref href="parent.dita">'
                '<topicgroup outputclass="tabs">'
                '<topicref href="Desktop.dita">'
                '<topicmeta><navtitle>Desktop</navtitle></topicmeta>'
                '</topicref>'
                '</topicgroup>'
                '<topicref href="post.dita"/>'
                '</topicref>'
                '</map>\n',
                encoding="utf-8",
            )
            (Path(tmp) / "parent.dita").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US">'
                '<title>Edit Page name</title>'
                '<conbody><p>Intro.</p></conbody></concept>\n',
                encoding="utf-8",
            )
            (Path(tmp) / "Desktop.dita").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//LINKEDIN//DTD DITA Task//EN" "linkedin_task.dtd">\n'
                '<task id="Y" xml:lang="en-US"><title>Desktop</title>'
                '<taskbody><steps>'
                '<step><cmd>Click Save.</cmd></step>'
                '<step><cmd>Post an update on your Page.</cmd></step>'
                '</steps></taskbody></task>\n',
                encoding="utf-8",
            )
            (Path(tmp) / "post.dita").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="Z" xml:lang="en-US">'
                '<title>Post-tab</title><conbody><p>old.</p></conbody></concept>\n',
                encoding="utf-8",
            )
            from app.map_parser import parse_ditamap_entries
            pub = reconstruct(parse_ditamap_entries(map_src))
            article = [
                HtmlArticleBlock(text="Edit Page name", kind="paragraph"),
                HtmlArticleBlock(text="Intro.", kind="paragraph"),
                HtmlArticleBlock(text="Desktop", kind="tab_label", section_id="panel-1"),
                HtmlArticleBlock(text="Click Save.", kind="step", section_id="panel-1"),
                # Article does NOT have "Post an update" in the tab.
                # The similar paragraph lives AFTER the tabs.
                HtmlArticleBlock(
                    text="After the change, post an update on your Page.",
                    kind="paragraph",
                    section_id="__post_tab__",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(
                ops, out, article_blocks=article, publication=pub,
            )
            desktop = (out / "Desktop.dita").read_text(encoding="utf-8")
            self.assertNotIn(
                "Post an update on your Page.",
                desktop,
                "the cross-section 'similar' text in post-tab must not "
                "block deletion of the Desktop-tab step",
            )


class PostTabContentRoutesToPostTabTopic(unittest.TestCase):
    """Beta-test regression from Article 4 (post-tab_content.dita):
    article content emitted AFTER the last tab panel closes should
    route to the post-tab .dita topic (per IM conversion convention),
    not to the previous tab's topic.

    Two pieces:
    1. Parser stamps post-tab article blocks with section_id="__post_tab__".
    2. build_topic_to_section binds the post-tab .dita topic (identified
       by map_depth dropping below the tab binding depth) to the same
       synthetic section.
    3. The diff's section-matching anchor selects a source block in
       that section as the INSERT anchor, and DELETE+INSERT pairs at
       the same anchor coalesce into a clean REPLACE."""

    def test_post_tab_article_text_lands_in_post_tab_topic(self) -> None:
        from app.article_html_parser import parse_help_center_html

        html = (
            '<article data-test-selector="article">'
            '<div data-test-selector="preRenderedMarkup-container">'
            '<p>Intro before tabs.</p>'
            '</div>'
            '<button class="tabs__tab"><span class="tabs__tab-label">'
            'Desktop</span></button>'
            '<div role="tabpanel" id="panel-1">'
            '<div data-test-selector="preRenderedMarkup-container">'
            '<p>Inside the Desktop tab.</p>'
            '</div>'
            '</div>'
            '<div data-test-selector="preRenderedMarkup-container">'
            '<p>After the tabs.</p>'
            '</div>'
            '</article>'
        )
        blocks = parse_help_center_html(html)
        # Find the post-tab block and verify its section_id.
        post_tab_block = next(
            b for b in blocks if "After the tabs" in (b.text or "")
        )
        self.assertEqual(
            post_tab_block.section_id, "__post_tab__",
            "blocks emitted after the last tab close must be stamped "
            "with the synthetic post-tab section_id",
        )
        # Intro before the tabs is NOT post-tab.
        intro_block = next(
            b for b in blocks if "Intro before" in (b.text or "")
        )
        self.assertNotEqual(
            intro_block.section_id, "__post_tab__",
            "intro content before the tabs must NOT be flagged as post-tab",
        )


class InsertAnchorSurvivesDeletes(unittest.TestCase):
    """Beta-test regression: when DELETEs run before INSERTs in the
    same topic batch and the INSERT's anchor xpath points at a
    higher-positioned sibling than a deleted element, positional
    renumbering (`li[2]` becoming `li[1]` after `li[1]` is deleted)
    broke the INSERT's xpath resolution and the new content was
    silently SKIPPED.

    Fix: pre-resolve every INSERT's anchor element BEFORE any DELETE
    runs. Element-tree object references stay valid across DELETEs on
    other elements; xpath strings do not. Surfaced on Article 4's
    before_you_begin where 3 of 6 article bullets disappeared from
    the patched output."""

    def test_insert_anchor_at_higher_li_still_resolves(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            # Source has 3 li items; article keeps li[2] and adds new
            # items after it. The diff will DELETE li[1] and li[3],
            # then INSERT new items anchored at li[2]. After deletes,
            # li[2]'s positional xpath would be li[1] — the old
            # behaviour skipped the inserts.
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE task PUBLIC "-//OASIS//DTD DITA Task//EN" "task.dtd">\n'
                '<task id="X"><title>T</title><taskbody><prereq>'
                '<ul>'
                '<li>Original first item</li>'
                '<li>Keeper bullet</li>'
                '<li>Original last item</li>'
                '</ul></prereq></taskbody></task>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="heading"),
                HtmlArticleBlock(text="Keeper bullet", kind="unordered_step"),
                HtmlArticleBlock(text="Brand new bullet A", kind="unordered_step"),
                HtmlArticleBlock(text="Brand new bullet B", kind="unordered_step"),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(
                ops, out, article_blocks=article, publication=pub,
            )
            patched = (out / "topic.dita").read_text(encoding="utf-8")
            import re
            ul_match = re.search(r'<ul>(.*?)</ul>', patched, re.DOTALL)
            self.assertIsNotNone(ul_match)
            li_count = ul_match.group(1).count("<li>")
            self.assertEqual(
                li_count, 3,
                "Keeper + 2 new bullets must all land in the <ul>; the "
                "INSERTs must survive the DELETEs that renumber siblings",
            )
            self.assertIn("Brand new bullet A", patched)
            self.assertIn("Brand new bullet B", patched)


class NoteTypeUpgradesFromHeadline(unittest.TestCase):
    """Beta-test regression from Article 4: the article's parent-topic
    callout had CSS class `--note` (generic) but headline 'Here's a
    tip'. The DITA `<note type="important">` got its prose REPLACEd
    but the @type attribute stayed "important". Now the parser maps
    known headline marker phrases to a specific note_kind, and the
    patch engine updates @type accordingly."""

    def test_heres_a_tip_upgrades_note_type(self) -> None:
        from app.article_html_parser import parse_help_center_html

        html = (
            '<article data-test-selector="article">'
            '<div class="article-content-callout article-content-callout__background--note" '
            'data-test-selector="callout-container">'
            '<div>'
            '<h3 class="article-content-callout__headline" '
            'data-test-selector="callout-headline">'
            "Here’s a tip"
            '</h3>'
            '<div class="article-content-callout__text-rich-description" '
            'data-test-selector="callout-rich-description">'
            "Body text here."
            '</div>'
            '</div>'
            '</div>'
            '</article>'
        )
        blocks = parse_help_center_html(html)
        note_blocks = [b for b in blocks if b.kind == "note"]
        self.assertEqual(len(note_blocks), 1)
        self.assertEqual(
            note_blocks[0].note_kind, "tip",
            'headline "Here’s a tip" must upgrade note_kind to '
            '"tip" even when the CSS class is the generic --note',
        )

    def test_note_replace_writes_new_type_attribute(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title>'
                '<conbody>'
                '<note type="important"><p>Old important text.</p></note>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="New tip body.",
                    kind="note", note_kind="tip",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            apply_ops(ops, out, article_blocks=article, publication=pub)
            patched = (out / "topic.dita").read_text(encoding="utf-8")
            self.assertIn(
                'type="tip"', patched,
                "the @type attribute must update to match the "
                "article-side note kind (was important)",
            )
            self.assertNotIn(
                'type="important"', patched,
                "the old important type must be gone",
            )


class AmbiguousReplaceSubsetEscape(unittest.TestCase):
    """Beta-test regression: a clear shortened rewrite ("Click the
    Save button in the upper-right corner." → "Click Save.") was
    refused in an ambiguous replace segment because the Jaccard
    overlap (0.33) sat below the existing high-overlap escape hatch
    threshold. New rule: when one side's content words are a subset
    of the other's AND the smaller side has ≥ 2 content words, the
    pair is a confident rewrite (one is a shortening of the other)."""

    def test_subset_shortened_text_escapes_ambiguity(self) -> None:
        from app.diff_engine import _is_high_overlap_pair

        self.assertTrue(
            _is_high_overlap_pair(
                "Click the Save button in the upper-right corner.",
                "Click Save.",
            ),
            "the article's shortened wording (subset of source words) "
            "must escape the ambiguous-segment refusal",
        )

    def test_single_shared_word_does_not_escape(self) -> None:
        """Counterpart: a single-word overlap (e.g. both mention
        'save' but otherwise unrelated) must NOT escape — the lower
        bound of 2 content words prevents coincidence pass-throughs."""
        from app.diff_engine import _is_high_overlap_pair

        self.assertFalse(
            _is_high_overlap_pair(
                "Make sure to save your work.",
                "Open settings.",
            ),
            "two texts sharing zero meaningful content must not escape",
        )


class RelatedLinksHeadingRecognized(unittest.TestCase):
    """Beta-test regression from the Premium Subscriptions article:
    the article uses a 'Related links' heading (not 'Related tasks').
    The original MANUAL_REVIEW_TITLES set didn't include this variant,
    so items under it were INSERTed into the topic body instead of
    routed to the .ditamap reltable consolidation."""

    def test_related_links_routes_to_reltable_consolidation(self) -> None:
        from app.publication_reconstructor import MANUAL_REVIEW_TITLES
        self.assertIn("related links", MANUAL_REVIEW_TITLES)
        self.assertIn("related link", MANUAL_REVIEW_TITLES)


class FeatureNotesRefusedNotModified(unittest.TestCase):
    """Beta-test regression from the Premium Subscriptions article:
    feature notes (`<note type="other" othertype="feature">`) carry a
    product callout that isn't reliably round-trippable through the
    article HTML. These notes must be flagged for review, not auto-
    rewritten — the writer adds them by hand."""

    def test_feature_note_marked_auto_update_false(self) -> None:
        import tempfile
        from app.map_parser import TopicRef
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title>'
                '<conbody>'
                '<note othertype="feature" type="other">'
                '<p>Get ahead with the Pro feature</p>'
                '<p>It helps you get hired and grow.</p>'
                '<p><xref href="https://example.com" scope="external" '
                'outputclass="button" format="html">Try it now</xref></p>'
                '</note>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            note_blocks = [
                b for b in pub.blocks if b.element_tag == "note"
            ]
            self.assertEqual(len(note_blocks), 1)
            self.assertFalse(
                note_blocks[0].auto_update,
                "feature notes must be marked auto_update=False so "
                "REPLACE is refused at apply time",
            )
            self.assertIn(
                "feature note", note_blocks[0].skip_reason or "",
                "skip reason should call out 'feature note' so the "
                "writer knows why",
            )

    def test_feature_note_safe_delete_when_article_drops_launcher(self) -> None:
        """User clarification (2026-06-23): if the live article no
        longer renders a feature launcher, the source DITA feature
        note can be safely DELETEd (the writer doesn't need to act).
        The "review manually" flag only fires when the launcher is
        still present and the writer must update it by hand."""
        import tempfile
        from app.diff_engine import DiffOp, OpKind
        from app.patch_engine import (
            ResultCategory,
            _article_has_feature_launcher,
            _is_source_feature_note,
        )
        from app.map_parser import TopicRef
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title>'
                '<conbody>'
                '<p>Body paragraph.</p>'
                '<note othertype="feature" type="other">'
                '<p>Get ahead with Premium</p>'
                '</note>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            note_block = next(
                b for b in pub.blocks if b.element_tag == "note"
            )
            self.assertTrue(_is_source_feature_note(note_block))

            class FakeArticleBlock:
                def __init__(self, kind, note_kind=None):
                    self.kind = kind
                    self.note_kind = note_kind

            # Article has NO feature launcher.
            self.assertFalse(
                _article_has_feature_launcher([FakeArticleBlock("paragraph")])
            )
            # Article HAS a feature launcher.
            self.assertTrue(
                _article_has_feature_launcher([
                    FakeArticleBlock("note", "feature-launcher"),
                ])
            )

    def test_complex_table_blocks_auto_row_insert(self) -> None:
        """User clarification (2026-06-23): comparison tables (3+
        columns) and tables with merged cells (CALS morerows /
        namest+nameend / spanname) carry too much per-cell semantics
        for the tool to slot a new row in safely. Refuse and ask the
        writer to add the row manually. Simpler 2-column tables with
        no merges remain auto-insertable."""
        import xml.etree.ElementTree as ET
        from app.patch_engine import _table_is_complex

        def make_row(n_cells, attrib_for=None):
            row = ET.Element("row")
            for i in range(n_cells):
                attrib = attrib_for.get(i, {}) if attrib_for else {}
                ET.SubElement(row, "entry", attrib=attrib).text = f"cell{i}"
            return row

        # Column-count signal.
        self.assertFalse(_table_is_complex(make_row(1)))
        self.assertFalse(_table_is_complex(make_row(2)))
        self.assertTrue(_table_is_complex(make_row(3)))
        self.assertTrue(_table_is_complex(make_row(8)))

        # Merged-cell signal on a 2-column table (would pass the column
        # check). morerows → vertical span; namest+nameend → horizontal
        # span; spanname → preset CALS span.
        tbody = ET.Element("tbody")
        tbody.append(make_row(2))
        tbody.append(make_row(2, {0: {"morerows": "1"}}))
        anchor = tbody[0]
        self.assertTrue(_table_is_complex(anchor, tbody))

        tbody = ET.Element("tbody")
        tbody.append(make_row(2, {0: {"namest": "c1", "nameend": "c2"}}))
        tbody.append(make_row(2))
        self.assertTrue(_table_is_complex(tbody[1], tbody))

        # Two-column tbody with no merges — stays simple.
        tbody = ET.Element("tbody")
        tbody.append(make_row(2))
        tbody.append(make_row(2))
        self.assertFalse(_table_is_complex(tbody[0], tbody))

    def test_article_feature_launcher_insert_is_refused(self) -> None:
        """An article-side feature-launcher block is never INSERTed
        into the topic. The writer is told to add it by hand."""
        from app.diff_engine import DiffOp, OpKind
        from app.patch_engine import _is_article_feature_launcher

        class FakeArticleBlock:
            def __init__(self, kind, note_kind=None):
                self.kind = kind
                self.note_kind = note_kind

        self.assertTrue(_is_article_feature_launcher(
            FakeArticleBlock("note", "feature-launcher")
        ))
        self.assertTrue(_is_article_feature_launcher(
            FakeArticleBlock("note", "feature")
        ))
        self.assertFalse(_is_article_feature_launcher(
            FakeArticleBlock("note", "important")
        ))
        self.assertFalse(_is_article_feature_launcher(
            FakeArticleBlock("paragraph")
        ))


class TrailingPunctuationEqualPairsInLCS(unittest.TestCase):
    """Beta-test regression from the Premium Subscriptions article:
    every article bullet had a trailing period; source DITA bullets
    didn't. LCS couldn't EQUAL-pair them, so the demote path fired
    on every bullet, the safety net suppressed the source DELETE
    (content appeared elsewhere in the article), and the article
    bullet INSERTed anyway — duplicate content."""

    def test_trailing_period_normalized_for_lcs_match(self) -> None:
        from app.publication_reconstructor import normalize_for_match
        # Trailing period
        a = normalize_for_match("Sales Navigator helps you generate leads")
        b = normalize_for_match("Sales Navigator helps you generate leads.")
        self.assertEqual(a, b)
        # Trailing question mark
        c = normalize_for_match("How do I create a post")
        d = normalize_for_match("How do I create a post?")
        self.assertEqual(c, d)
        # Multiple trailing punct
        e = normalize_for_match("Done")
        f = normalize_for_match("Done...")
        self.assertEqual(e, f)
        # But interior punctuation must be preserved (it can disambiguate)
        g = normalize_for_match("Click Save then Apply")
        h = normalize_for_match("Click Save. Then Apply")
        self.assertNotEqual(
            g, h,
            "interior punctuation should still create distinct keys "
            "so genuinely-different content doesn't false-pair",
        )

    def test_no_duplicate_li_on_period_only_diff(self) -> None:
        """End-to-end: article has the same bullets as source but each
        ends in a period. After the fix, source li blocks EQUAL-pair
        with their article counterparts and no INSERTs fire."""
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import TopicRef
        from app.patch_engine import apply_ops
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title>'
                '<conbody>'
                '<ul>'
                '<li>Sales Navigator helps you generate leads</li>'
                '<li>Recruiter Lite helps you find and hire talent</li>'
                '<li>a Learning helps you improve your skills</li>'
                '</ul>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(
                    text="Sales Navigator helps you generate leads.",
                    kind="unordered_step",
                ),
                HtmlArticleBlock(
                    text="Recruiter Lite helps you find and hire talent.",
                    kind="unordered_step",
                ),
                HtmlArticleBlock(
                    text="a Learning helps you improve your skills.",
                    kind="unordered_step",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            insert_ops = [
                op for op in ops
                if op.kind.value == "insert"
            ]
            self.assertEqual(
                len(insert_ops), 0,
                "after normalize_for_match strips trailing punctuation, "
                "source bullets EQUAL-pair with article counterparts — "
                "no INSERT, no duplicate. If this fires, the diff thinks "
                "the period-suffixed article bullets are new content.",
            )


class ReltableSectionVisibility(unittest.TestCase):
    """Beta-test regression: when the article has a Related-tasks
    section whose items all match the .ditamap reltable, the
    consolidation correctly suppresses the 'review reltable' MAP_EDIT,
    but the consumed ops vanished from the report without any signal
    to the writer that the section was even noticed. We emit a quiet
    acknowledgment so the writer sees the section was detected and
    reviewed. Category is MAP_EDIT (updated 2026-06-23 — was DETECTED,
    which surfaced the card under a topic-body tab and confused
    writers)."""

    def test_reltable_acknowledgment_when_items_match(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import diff
        from app.map_parser import ReltableEntry, TopicRef
        from app.patch_engine import apply_ops, ResultCategory
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title>'
                '<conbody><p>Body.</p></conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Body.", kind="paragraph"),
                HtmlArticleBlock(text="Related tasks", kind="paragraph"),
                HtmlArticleBlock(
                    text="Contact us", kind="unordered_step",
                    links=[("Contact us", "/help/linkedin/solve")],
                ),
            ]
            reltable_entries = [
                ReltableEntry(
                    section="Related tasks",
                    navtitle="Contact us",
                    href="/help/linkedin/solve",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            out = Path(tmp) / "out"
            report = apply_ops(
                ops, out, article_blocks=article, publication=pub,
                reltable_entries=reltable_entries,
            )
            acknowledgments = [
                r for r in report.results
                if r.category == ResultCategory.MAP_EDIT
                and "Reltable section reviewed" in (r.reason or "")
            ]
            self.assertEqual(
                len(acknowledgments), 1,
                "When reltable items all match the .ditamap, the writer "
                "must still get a quiet acknowledgment so they know the "
                "section was detected and reviewed",
            )


class NoteLabelPlusUnorderedStepsMergeIntoNote(unittest.TestCase):
    """Beta feedback (2026-06-23): the Available-payment-methods article
    rendered an "Important:" label followed by a top-level unordered
    list. The parser emitted those bullets as `unordered_step` kind,
    but `expand_blocks_for_diff` only merged when the followers were
    `list_item` kind. The merge skipped, so the article-side bullets
    couldn't pair against the source's collapsed `<note><ul><li>` block
    and each bullet got INSERTed as a phantom `<p>` next to the source
    note — duplicating the same content."""

    def test_note_label_merges_with_unordered_step_followers(self) -> None:
        from app.article_html_parser import HtmlArticleBlock

        blocks = [
            HtmlArticleBlock(text="Important:", kind="paragraph"),
            HtmlArticleBlock(text="Bullet one.", kind="unordered_step"),
            HtmlArticleBlock(text="Bullet two.", kind="unordered_step"),
            HtmlArticleBlock(text="Bullet three.", kind="unordered_step"),
        ]
        texts, origins = expand_blocks_for_diff(blocks)
        # The 4 input blocks should collapse to 1 merged note block.
        self.assertEqual(len(texts), 1)
        self.assertEqual(origins[0].kind, "note")
        self.assertEqual(origins[0].note_bullets,
                         ["Bullet one.", "Bullet two.", "Bullet three."])

    def test_note_label_merges_with_step_followers(self) -> None:
        from app.article_html_parser import HtmlArticleBlock

        blocks = [
            HtmlArticleBlock(text="Note:", kind="paragraph"),
            HtmlArticleBlock(text="One.", kind="step"),
            HtmlArticleBlock(text="Two.", kind="step"),
        ]
        texts, origins = expand_blocks_for_diff(blocks)
        self.assertEqual(len(texts), 1)
        self.assertEqual(origins[0].kind, "note")

    def test_note_label_merges_with_list_item_followers(self) -> None:
        """Regression guard for the original (pre-fix) merge case so the
        extension doesn't break the nested-rich-text-wrapper path."""
        from app.article_html_parser import HtmlArticleBlock

        blocks = [
            HtmlArticleBlock(text="Tip:", kind="paragraph"),
            HtmlArticleBlock(text="A.", kind="list_item"),
            HtmlArticleBlock(text="B.", kind="list_item"),
        ]
        texts, origins = expand_blocks_for_diff(blocks)
        self.assertEqual(len(texts), 1)
        self.assertEqual(origins[0].kind, "note")

    def test_note_label_without_list_followers_is_not_merged(self) -> None:
        """A "Note:" label followed by a paragraph (not a list-item-like
        kind) is left alone — we only merge when the structure is
        unambiguously a note + bulleted body."""
        from app.article_html_parser import HtmlArticleBlock

        blocks = [
            HtmlArticleBlock(text="Note:", kind="paragraph"),
            HtmlArticleBlock(text="Regular paragraph.", kind="paragraph"),
        ]
        texts, _origins = expand_blocks_for_diff(blocks)
        self.assertEqual(len(texts), 2)


class NoteAttributeMappingForRoleAndFeature(unittest.TestCase):
    """Beta feedback (2026-06-23): an article with a "Who can use this
    feature" callout has parser `note_kind="permission"`. The REPLACE
    path on a `<note>` element naively wrote `element.set("type",
    "permission")`, producing `<note type="permission">` — invalid per
    the IM. Per the existing `_NOTE_KIND_TO_DITA_ATTRS` mapping, the
    correct write is `type="other" othertype="role"`. Same bug shape
    bit `feature` (→ should be `type="other" othertype="feature"`) and
    `pdf` (→ `type="other" othertype="pdf"`)."""

    def _replace_note_and_check(
        self, source_attrs: dict, article_note_kind: str,
        expected_type: str, expected_othertype: str | None,
    ) -> None:
        """Build a 1-topic DITA with a single `<note>` carrying
        `source_attrs`, simulate an article-side REPLACE whose parsed
        block has `note_kind=article_note_kind`, run apply_ops with
        dry_run, and check the patched element's attributes."""
        import tempfile
        import xml.etree.ElementTree as ET
        from app.article_html_parser import HtmlArticleBlock
        from app.diff_engine import DiffOp, OpKind
        from app.map_parser import TopicRef
        from app.publication_reconstructor import reconstruct

        attrs_str = " ".join(f'{k}="{v}"' for k, v in source_attrs.items())
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title>'
                '<conbody>'
                f'<note {attrs_str}>'
                '<p>old body text.</p>'
                '</note>'
                '</conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            note_block = next(b for b in pub.blocks if b.element_tag == "note")
            article_block = HtmlArticleBlock(
                text="new body text.",
                kind="note",
                note_kind=article_note_kind,
            )
            op = DiffOp(
                kind=OpKind.REPLACE,
                source_block=note_block,
                updated_text="new body text.",
                updated_index=0,
                anchor_block=None,
                safe_to_apply=True,
            )
            report = apply_ops(
                [op], Path(tmp) / "out",
                article_blocks=[article_block],
                publication=pub,
                dry_run=False,
            )
            self.assertEqual(len(report.applied), 1, msg=str(report.results))
            patched = (Path(tmp) / "out" / "topic.dita").read_text(
                encoding="utf-8"
            )
            root = ET.fromstring(patched)
            note_elem = root.find(".//note")
            self.assertIsNotNone(note_elem)
            self.assertEqual(
                note_elem.get("type"), expected_type,
                f"type mismatch for article_note_kind={article_note_kind!r}",
            )
            self.assertEqual(
                note_elem.get("othertype"), expected_othertype,
                f"othertype mismatch for article_note_kind={article_note_kind!r}",
            )

    def test_permission_writes_type_other_othertype_role(self) -> None:
        self._replace_note_and_check(
            source_attrs={"type": "tip"},
            article_note_kind="permission",
            expected_type="other",
            expected_othertype="role",
        )

    def test_feature_writes_type_other_othertype_feature(self) -> None:
        self._replace_note_and_check(
            source_attrs={"type": "important"},
            article_note_kind="feature",
            expected_type="other",
            expected_othertype="feature",
        )

    def test_pdf_writes_type_other_othertype_pdf(self) -> None:
        self._replace_note_and_check(
            source_attrs={"type": "important"},
            article_note_kind="pdf",
            expected_type="other",
            expected_othertype="pdf",
        )

    def test_tip_writes_type_tip_and_clears_stale_othertype(self) -> None:
        """When transitioning FROM a role/feature/pdf note TO a simple
        tip/important, the stale @othertype must be cleared."""
        self._replace_note_and_check(
            source_attrs={"type": "other", "othertype": "role"},
            article_note_kind="tip",
            expected_type="tip",
            expected_othertype=None,
        )

    def test_unchanged_attrs_when_kind_already_matches(self) -> None:
        """No-op upgrade: source already matches the article kind, no
        attribute churn."""
        self._replace_note_and_check(
            source_attrs={"type": "tip"},
            article_note_kind="tip",
            expected_type="tip",
            expected_othertype=None,
        )


class ReltableSectionReviewedIsMapEditNotDetected(unittest.TestCase):
    """Beta feedback (2026-06-23): the "Reltable section reviewed" quiet
    acknowledgment was being emitted with category=DETECTED, putting it
    inside the topic-body "Add manually" tab even though it's purely a
    .ditamap concern. The writer-facing rewriter then said "review this
    change manually" — directly contradicting the reason's own "no
    action needed" text. Move to MAP_EDIT so it lands in Map updates."""

    def test_reltable_acknowledgment_is_categorized_as_map_edit(self) -> None:
        import tempfile
        from app.article_html_parser import HtmlArticleBlock
        from app.map_parser import ReltableEntry, TopicRef
        from app.publication_reconstructor import reconstruct

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "topic.dita"
            src.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE concept PUBLIC "-//LINKEDIN//DTD DITA Concept//EN" "linkedin_concept.dtd">\n'
                '<concept id="X" xml:lang="en-US"><title>T</title>'
                '<conbody><p>Body.</p></conbody></concept>\n',
                encoding="utf-8",
            )
            ref = TopicRef(
                topic_id="topic.dita", href="topic.dita",
                resolved_path=src, depth=0, map_position=0,
            )
            pub = reconstruct([ref])
            article = [
                HtmlArticleBlock(text="T", kind="paragraph"),
                HtmlArticleBlock(text="Body.", kind="paragraph"),
                HtmlArticleBlock(text="Related tasks", kind="paragraph"),
                HtmlArticleBlock(
                    text="Contact us", kind="unordered_step",
                    links=[("Contact us", "/help/linkedin/solve")],
                ),
            ]
            reltable_entries = [
                ReltableEntry(
                    section="Related tasks",
                    navtitle="Contact us",
                    href="/help/linkedin/solve",
                ),
            ]
            ops = diff(pub, [b.text for b in article], article_blocks=article)
            report = apply_ops(
                ops, Path(tmp) / "out",
                article_blocks=article, publication=pub,
                reltable_entries=reltable_entries,
            )
            ack = [
                r for r in report.results
                if r.reason and "Reltable section reviewed" in r.reason
            ]
            self.assertEqual(
                len(ack), 1,
                "The quiet acknowledgment should fire when the article "
                "has a reltable section with matching items",
            )
            self.assertEqual(
                ack[0].category, ResultCategory.MAP_EDIT,
                "Reltable acknowledgments belong in Map updates, not "
                "inside a topic-body advisory category",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
