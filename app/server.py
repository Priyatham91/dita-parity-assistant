"""Tiny stdlib-only HTTP server for the DITA Parity Assistant.

Run from the project root:
    python app/server.py

Open: http://localhost:8000/

What it provides:
  GET  /            upload form (ditamap + .dita topics + article.txt)
  POST /run         accepts the upload, runs the pipeline, redirects to the
                    generated HTML report
  GET  /runs/<id>/… serves anything inside output/runs/<id>/ so the report
                    HTML and the patched .dita files render correctly

No third-party dependencies. Single-threaded; one user at a time. Designed
as a PoC, not a production service.
"""

from __future__ import annotations

import html
import http.server
import mimetypes
import socketserver
import sys
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.article_html_parser import expand_blocks_for_diff, parse_help_center_html  # noqa: E402
from app.diff_engine import diff, split_updated_article, summarize  # noqa: E402
from app.html_report import write_html_report  # noqa: E402
from app.map_parser import TopicRef, parse_ditamap_entries, parse_reltable_entries  # noqa: E402
from app.patch_engine import apply_ops  # noqa: E402
from app.publication_reconstructor import reconstruct  # noqa: E402
from app.report_generator import write_patch_report  # noqa: E402
from app.schematron_validator import validate_files  # noqa: E402


RUNS_DIR = PROJECT_ROOT / "output" / "runs"
HOST = "localhost"
PORT = 8000


# --- HTML templates ------------------------------------------------------- #

_FORM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DITA Parity Assistant</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: #f7f7f8; color: #1f2328; max-width: 760px; margin: 0 auto; padding: 32px; }}
  h1 {{ margin: 0 0 8px; font-size: 26px; }}
  p.lede {{ color: #6e7681; margin: 0 0 24px; }}
  .card {{ background: #fff; border: 1px solid #d0d7de; border-radius: 8px; padding: 20px;
           margin-bottom: 16px; }}
  label {{ display: block; font-weight: 600; margin: 14px 0 6px; font-size: 13px; }}
  .hint {{ font-weight: 400; color: #6e7681; font-size: 12px; margin-left: 6px; }}
  input[type=file] {{ display: block; width: 100%; padding: 8px; border: 1px solid #d0d7de;
                      border-radius: 6px; background: #fafbfc; font: inherit; }}
  button {{ margin-top: 18px; background: #0969da; color: #fff; border: none; padding: 10px 18px;
            font: inherit; font-weight: 600; border-radius: 6px; cursor: pointer; }}
  button:hover {{ background: #0858c2; }}
  button:disabled {{ background: #6e7681; cursor: wait; }}
  ul.runs {{ list-style: none; padding: 0; margin: 8px 0 0; }}
  ul.runs li {{ padding: 6px 0; border-bottom: 1px solid #f0f0f0; font-size: 13px; }}
  ul.runs li:last-child {{ border-bottom: none; }}
  ul.runs a {{ color: #0969da; text-decoration: none; }}
  ul.runs a:hover {{ text-decoration: underline; }}
  .muted {{ color: #6e7681; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px;
          background: #f0f0f0; padding: 1px 5px; border-radius: 3px; }}
</style>
</head>
<body>
<div style="display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap;">
  <h1 style="margin:0;">DITA Parity Assistant</h1>
  <a href="/guide" target="_blank" style="font-weight:600;color:#0969da;text-decoration:none;font-size:14px;">
    How to use this tool →
  </a>
</div>
<p class="lede" style="margin-top:8px;">Upload a converted DITA package and an updated source article. The assistant
will reconstruct the publication, diff it against the article, apply safe text changes,
and give you a parity report.</p>

<div class="card">
  <form action="/run" method="POST" enctype="multipart/form-data"
        onsubmit="document.getElementById('go').disabled=true;document.getElementById('go').textContent='Running…';">
    <label>DITA map <span class="hint">single <code>.ditamap</code> file</span></label>
    <input type="file" name="ditamap" accept=".ditamap,.xml" required>

    <label>DITA topic files <span class="hint">all <code>.dita</code> files referenced by the map</span></label>
    <input type="file" name="dita_files" accept=".dita" multiple required>

    <label>Updated article — choose ONE source</label>
    <p class="hint" style="margin: -2px 0 8px;">Either paste a Help Center URL <em>or</em> upload a plain-text article. URL is preferred — it preserves notes, lists, and inline markup.</p>
    <input type="url" name="article_url" placeholder="https://help.example.com/answer/..." style="width:100%;padding:8px;border:1px solid #d0d7de;border-radius:6px;font:inherit;margin-bottom:6px;">
    <input type="file" name="article" accept=".txt">

    <label style="display:flex;align-items:flex-start;gap:8px;margin-top:14px;cursor:pointer;font-weight:normal;">
      <input type="checkbox" name="dry_run" value="1" style="margin:3px 0 0;">
      <span>
        <strong>Dry run</strong> <span class="hint">— produce the report only; do not write patched <code>.dita</code> files</span>
        <span class="hint" style="display:block;margin-top:2px;">Tip: check this to review the report first. You can apply the changes in one click from the report itself.</span>
      </span>
    </label>

    <button type="submit" id="go">Run migration</button>
  </form>
</div>

<div class="card">
  <strong>Previous runs</strong>
  <ul class="runs">
    {previous_runs}
  </ul>
</div>

</body>
</html>
"""

_ERROR_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Error — DITA Parity Assistant</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: #f7f7f8; color: #1f2328; max-width: 800px; margin: 0 auto; padding: 32px; }}
  h1 {{ color: #cf222e; }}
  pre {{ background: #fff; border: 1px solid #d0d7de; border-radius: 6px; padding: 14px;
         overflow-x: auto; font-size: 12px; }}
  a {{ color: #0969da; }}
</style>
</head>
<body>
<h1>Run failed</h1>
<p>{summary}</p>
<pre>{detail}</pre>
<p><a href="/">← Back to upload</a></p>
</body></html>
"""


# --- Request handler ----------------------------------------------------- #

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "DitaParity/0.1"

    # Quieter access log.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write(
            "[%s] %s\n" % (self.log_date_time_string(), format % args)
        )

    # --- Routes --- #
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._serve_form()
            return
        if path == "/guide":
            self._serve_guide()
            return
        if path.startswith("/runs/"):
            self._serve_run_file(path[len("/runs/"):])
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/run":
            self._handle_run()
            return
        # /runs/<run_id>/migrate — promote a previous dry-run into a
        # real migration without re-uploading the inputs. The button
        # in the report header POSTs here so writers can act on the
        # dry-run output in one click. See _handle_migrate for details.
        if self.path.startswith("/runs/") and self.path.endswith("/migrate"):
            run_id = self.path[len("/runs/"):-len("/migrate")]
            self._handle_migrate(run_id)
            return
        # /runs/<run_id>/open-outputs — opens the run's outputs/ folder
        # in Windows Explorer. Browsers block plain file:// links from
        # an http://localhost origin, so we can't use a plain anchor;
        # the report's button POSTs here instead. See _handle_open_outputs.
        if self.path.startswith("/runs/") and self.path.endswith("/open-outputs"):
            run_id = self.path[len("/runs/"):-len("/open-outputs")]
            self._handle_open_outputs(run_id)
            return
        self.send_error(404)

    # --- Handlers --- #
    def _serve_form(self) -> None:
        body = _FORM_HTML.format(previous_runs=_render_previous_runs())
        self._send_html(200, body)

    def _serve_guide(self) -> None:
        body = _render_guide_html()
        self._send_html(200, body)

    def _serve_run_file(self, rel: str) -> None:
        # Resolve safely under RUNS_DIR; refuse anything outside it.
        try:
            target = (RUNS_DIR / rel).resolve()
            target.relative_to(RUNS_DIR.resolve())
        except (ValueError, OSError):
            self.send_error(403, "Path outside runs directory")
            return
        if not target.exists() or not target.is_file():
            self.send_error(404)
            return
        content_type, _ = mimetypes.guess_type(str(target))
        if content_type is None:
            content_type = "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_run(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self._serve_error("Expected multipart/form-data", content_type)
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)

        try:
            uploads = _parse_multipart(content_type, body)
        except Exception:  # noqa: BLE001
            self._serve_error("Failed to parse upload.", traceback.format_exc())
            return

        ditamap_uploads = uploads.get("ditamap", [])
        dita_uploads = uploads.get("dita_files", [])
        article_uploads = uploads.get("article", [])
        article_url_uploads = uploads.get("article_url", [])
        article_url = (
            article_url_uploads[0].content.decode("utf-8", errors="replace").strip()
            if article_url_uploads
            else ""
        )
        dry_run = bool(uploads.get("dry_run", []))

        if not ditamap_uploads:
            self._serve_error("Missing required upload.", "ditamap is required.")
            return
        if not article_uploads and not article_url:
            self._serve_error(
                "Missing article source.",
                "Provide either a Help Center URL or a .txt article upload.",
            )
            return

        run_id = (
            datetime.now().strftime("%Y%m%d_%H%M%S")
            + "_"
            + uuid.uuid4().hex[:6]
        )
        run_dir = RUNS_DIR / run_id
        inputs_dir = run_dir / "inputs"
        outputs_dir = run_dir / "outputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        outputs_dir.mkdir(parents=True, exist_ok=True)

        # Save all uploaded files under inputs/ using the original filenames.
        ditamap_path = _save_upload(ditamap_uploads[0], inputs_dir)
        for upload in dita_uploads:
            _save_upload(upload, inputs_dir)

        # Resolve the article: URL takes precedence if provided.
        article_path: Path
        article_source_label: str
        article_canonical_url: Optional[str] = None
        updated_blocks: List[str]
        article_origins: Optional[list] = None
        try:
            if article_url:
                article_path, updated_blocks, article_origins = (
                    _fetch_and_parse_article_url(article_url, inputs_dir)
                )
                article_source_label = article_url
                article_canonical_url = article_url
            else:
                article_path = _save_upload(article_uploads[0], inputs_dir)
                article_text = article_path.read_text(encoding="utf-8")
                updated_blocks = split_updated_article(article_text)
                article_source_label = article_path.name
                # Try to pull the canonical Help Center URL out of the
                # uploaded HTML so the report header still shows the
                # live article link even when the writer didn't paste
                # the URL.
                from app.article_html_parser import extract_canonical_url
                article_canonical_url = extract_canonical_url(article_text)
        except Exception:  # noqa: BLE001
            self._serve_error(
                "Failed to load the updated article.",
                traceback.format_exc(),
            )
            return

        try:
            self._run_pipeline(
                run_id=run_id,
                ditamap_path=ditamap_path,
                article_path=article_path,
                article_source_label=article_source_label,
                article_canonical_url=article_canonical_url,
                updated_blocks=updated_blocks,
                article_origins=article_origins,
                outputs_dir=outputs_dir,
                dry_run=dry_run,
            )
        except Exception:  # noqa: BLE001
            self._serve_error(
                "The pipeline raised an exception while processing your upload.",
                traceback.format_exc(),
            )
            return

        # Redirect to the freshly generated report.
        report_url = f"/runs/{run_id}/outputs/report.html"
        self.send_response(303)  # See Other
        self.send_header("Location", report_url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _run_pipeline(
        self,
        run_id: str,
        ditamap_path: Path,
        article_path: Path,
        article_source_label: str,
        article_canonical_url: Optional[str],
        updated_blocks: List[str],
        article_origins: Optional[list],
        outputs_dir: Path,
        dry_run: bool,
    ) -> None:
        """Run the diff/patch/report pipeline against already-resolved
        inputs. Shared by `_handle_run` (initial upload) and
        `_handle_migrate` (promote a dry-run in place)."""
        map_entries = parse_ditamap_entries(ditamap_path)
        reltable_entries = parse_reltable_entries(ditamap_path)
        topic_refs = [e for e in map_entries if isinstance(e, TopicRef)]
        publication = reconstruct(map_entries)
        ops = diff(publication, updated_blocks, article_blocks=article_origins)
        patch_report = apply_ops(
            ops, outputs_dir,
            article_blocks=article_origins,
            publication=publication,
            dry_run=dry_run,
            reltable_entries=reltable_entries,
        )
        schematron_dir = PROJECT_ROOT / "schematron"
        validation_report = validate_files(
            patch_report.files_written, schematron_dir
        )
        write_patch_report(
            patch_report,
            outputs_dir / "patch_report.txt",
            validation=validation_report,
            dry_run=dry_run,
        )
        write_html_report(
            outputs_dir / "report.html",
            map_path=ditamap_path,
            article_path=article_path,
            topic_refs=topic_refs,
            publication=publication,
            updated_block_count=len(updated_blocks),
            diff_summary=summarize(ops),
            report=patch_report,
            output_dir=outputs_dir,
            article_label=article_source_label,
            article_canonical_url=article_canonical_url,
            validation=validation_report,
            dry_run=dry_run,
            run_id=run_id,
        )
        # Dry-run cleanup: the patched .dita files had to exist on
        # disk for Schematron validation and the track-changes diff
        # rendering. Now that both reports are written, remove the
        # patched .dita files so a dry-run leaves only the reports
        # behind, as advertised.
        if dry_run:
            for f in patch_report.files_written:
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass

    def _handle_migrate(self, run_id: str) -> None:
        """Promote a saved dry-run into a real migration in place.

        Re-runs the pipeline with `dry_run=False` against the same
        inputs that were uploaded for `run_id`. Outputs (including
        report.html) are overwritten in the SAME `outputs/` folder —
        one run record per article, no dry-run/applied duplication.
        Beta feedback (2026-06-23): writers don't want to re-upload
        the same files twice just to commit a dry-run they already
        reviewed."""
        # Strict run_id sanity check: digits-underscores-letters only,
        # no path components. Stops "../foo" or absolute-path tricks.
        if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
            self.send_error(400, "Invalid run id")
            return

        run_dir = RUNS_DIR / run_id
        inputs_dir = run_dir / "inputs"
        outputs_dir = run_dir / "outputs"
        if not inputs_dir.is_dir():
            self._serve_error(
                "This run's inputs are gone.",
                f"Looked for {inputs_dir} but it's missing. The dry-run "
                "may have been deleted manually. Start a fresh run "
                "from the home page.",
            )
            return

        # Recover the inputs the original upload saved. Ditamap +
        # article filenames are determined by what the writer uploaded,
        # so glob for them.
        ditamap_candidates = sorted(inputs_dir.glob("*.ditamap"))
        if not ditamap_candidates:
            self._serve_error(
                "Couldn't find a .ditamap in this run's inputs.",
                f"Searched {inputs_dir}",
            )
            return
        ditamap_path = ditamap_candidates[0]

        # The article was saved as either article_source.html (URL
        # path or .html upload) or a plain .txt upload.
        article_html = inputs_dir / "article_source.html"
        article_txt_candidates = [
            p for p in inputs_dir.glob("*.txt")
            if p.name != "patch_report.txt"
        ]
        try:
            if article_html.exists():
                article_path = article_html
                article_text = article_html.read_text(
                    encoding="utf-8", errors="replace",
                )
                from app.article_html_parser import (
                    parse_help_center_html, expand_blocks_for_diff,
                    extract_canonical_url,
                )
                blocks = parse_help_center_html(article_text)
                updated_blocks, article_origins = expand_blocks_for_diff(blocks)
                article_canonical_url = extract_canonical_url(article_text)
                article_source_label = (
                    article_canonical_url or article_path.name
                )
            elif article_txt_candidates:
                article_path = article_txt_candidates[0]
                article_text = article_path.read_text(encoding="utf-8")
                updated_blocks = split_updated_article(article_text)
                article_origins = None
                article_source_label = article_path.name
                article_canonical_url = None
            else:
                self._serve_error(
                    "Couldn't find an article source in this run's inputs.",
                    f"Searched {inputs_dir}",
                )
                return
        except Exception:  # noqa: BLE001
            self._serve_error(
                "Failed to re-parse the saved article.",
                traceback.format_exc(),
            )
            return

        try:
            self._run_pipeline(
                run_id=run_id,
                ditamap_path=ditamap_path,
                article_path=article_path,
                article_source_label=article_source_label,
                article_canonical_url=article_canonical_url,
                updated_blocks=updated_blocks,
                article_origins=article_origins,
                outputs_dir=outputs_dir,
                dry_run=False,
            )
        except Exception:  # noqa: BLE001
            self._serve_error(
                "The pipeline raised an exception while promoting the dry run.",
                traceback.format_exc(),
            )
            return

        # Redirect back to the (now applied) report in the same run.
        report_url = f"/runs/{run_id}/outputs/report.html"
        self.send_response(303)  # See Other
        self.send_header("Location", report_url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_open_outputs(self, run_id: str) -> None:
        """Open the run's outputs/ folder in Windows Explorer.

        Browsers block `file://` navigation from an `http://localhost`
        origin, so the patched-files link in the applied-report panel
        used to do nothing on click. Writers had to manually copy the
        path. This endpoint sidesteps the block: the report button
        POSTs here and the server calls `os.startfile()` to open the
        folder."""
        # Same run_id sanity check as _handle_migrate — keep this
        # endpoint from being weaponised to open arbitrary paths.
        if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
            self.send_error(400, "Invalid run id")
            return
        target = RUNS_DIR / run_id / "outputs"
        if not target.is_dir():
            self.send_error(404, "Outputs folder not found for this run")
            return
        try:
            # os.startfile is Windows-only (and that's our target —
            # the assistant ships as a Windows .exe). On non-Windows
            # we fall back to no-op + 500 rather than guess.
            import os
            os.startfile(str(target.resolve()))
        except Exception:  # noqa: BLE001
            self._serve_error(
                "Couldn't open the outputs folder.",
                traceback.format_exc(),
            )
            return
        # 204 No Content — nothing to render, the writer's File
        # Explorer window has just popped up.
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- Helpers --- #
    def _send_html(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_error(self, summary: str, detail: str) -> None:
        body = _ERROR_HTML.format(
            summary=html.escape(summary),
            detail=html.escape(detail),
        )
        self._send_html(500, body)


# --- Multipart parsing (stdlib only) ------------------------------------ #

class _Upload:
    __slots__ = ("filename", "content")

    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self.content = content


def _parse_multipart(content_type: str, body: bytes) -> dict:
    """Parse a multipart/form-data body into {field_name: [_Upload, ...]}.

    Uses email.parser, which understands MIME multipart natively. We feed
    it a synthetic header so it knows the Content-Type and boundary.
    """
    prelude = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    msg = BytesParser(policy=default).parsebytes(prelude + body)

    out: dict = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename() or ""
        content = part.get_payload(decode=True) or b""
        out.setdefault(name, []).append(_Upload(filename=filename, content=content))
    return out


_HTTP_USER_AGENT = (
    "Mozilla/5.0 (DitaParityAssistant/0.1) "
    "AppleWebKit/537.36 (KHTML, like Gecko)"
)


def _fetch_and_parse_article_url(url: str, inputs_dir: Path) -> tuple:
    """Fetch a Help Center URL, save the raw HTML to inputs/, and return
    (saved_html_path, texts, origins) where texts feeds the diff engine
    and origins carries per-block metadata (cells for table rows etc.)."""
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(f"URL must start with http:// or https:// — got {url!r}")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _HTTP_USER_AGENT, "Accept": "text/html"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        raw = resp.read()
    # Save the raw HTML alongside the other inputs for provenance.
    saved = inputs_dir / "article_source.html"
    saved.write_bytes(raw)
    html_text = raw.decode("utf-8", errors="replace")
    blocks = parse_help_center_html(html_text)
    texts, origins = expand_blocks_for_diff(blocks)
    return saved, texts, origins


def _save_upload(upload: _Upload, dest_dir: Path) -> Path:
    """Save an upload to dest_dir using its original filename.

    Guard against path traversal: drop any directory components from the
    filename and refuse empty / suspicious names.
    """
    safe_name = Path(upload.filename).name  # strips any path components
    if not safe_name:
        raise ValueError(f"upload had no usable filename: {upload.filename!r}")
    target = dest_dir / safe_name
    target.write_bytes(upload.content)
    return target


# --- Previous-runs index ------------------------------------------------- #

def _render_previous_runs() -> str:
    if not RUNS_DIR.exists():
        return '<li class="muted"><em>No runs yet.</em></li>'
    items: List[str] = []
    for run_dir in sorted(RUNS_DIR.iterdir(), reverse=True):
        report = run_dir / "outputs" / "report.html"
        if not report.exists():
            continue
        items.append(
            f'<li><a href="/runs/{html.escape(run_dir.name)}/outputs/report.html">'
            f'{html.escape(run_dir.name)}</a></li>'
        )
        if len(items) >= 20:
            break
    return "\n".join(items) or '<li class="muted"><em>No runs yet.</em></li>'


# --- Writer guide rendering --------------------------------------------- #

def _render_guide_html() -> str:
    """Render WRITER_GUIDE.md as a styled HTML page.

    The .md file lives next to the app/ folder (i.e. under
    PROJECT_ROOT). In the bundled .exe, PROJECT_ROOT is patched by
    launcher.py to point at the PyInstaller bundle root, which also
    contains a copy of the guide.
    """
    guide_path = PROJECT_ROOT / "WRITER_GUIDE.md"
    if not guide_path.exists():
        return (
            "<!DOCTYPE html><html><body><h1>Guide not found</h1>"
            f"<p>Expected at <code>{html.escape(str(guide_path))}</code>.</p>"
            "<p><a href='/'>Back to the upload page</a></p>"
            "</body></html>"
        )

    try:
        import markdown  # type: ignore
        body_html = markdown.markdown(
            guide_path.read_text(encoding="utf-8"),
            extensions=["tables", "fenced_code"],
        )
    except Exception as exc:  # noqa: BLE001
        # Fall back to plain pre-formatted text if anything goes wrong.
        body_html = (
            f"<p><i>Couldn't render markdown ({html.escape(str(exc))}); "
            "showing the raw text instead.</i></p>"
            f"<pre>{html.escape(guide_path.read_text(encoding='utf-8'))}</pre>"
        )
    return _GUIDE_HTML.format(body=body_html)


_GUIDE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>How to use the DITA Parity Assistant</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 32px 24px; max-width: 820px; margin: 0 auto;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif; font-size: 15px; line-height: 1.6;
    color: #1f2328; background: #f7f7f8; }}
  h1 {{ font-size: 28px; margin: 0 0 16px; }}
  h2 {{ font-size: 20px; margin: 32px 0 12px; padding-bottom: 6px;
    border-bottom: 1px solid #d0d7de; }}
  h3 {{ font-size: 16px; margin: 24px 0 8px; }}
  h4 {{ font-size: 14px; margin: 20px 0 8px; color: #57606a; }}
  a {{ color: #0969da; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{ font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo,
    Consolas, monospace; font-size: 13px; background: #eaeef2;
    padding: 1px 6px; border-radius: 4px; }}
  pre {{ background: #f6f8fa; padding: 14px; border-radius: 8px;
    overflow-x: auto; border: 1px solid #d0d7de; }}
  pre code {{ background: transparent; padding: 0; }}
  table {{ border-collapse: collapse; margin: 12px 0; width: 100%;
    background: #ffffff; }}
  th, td {{ border: 1px solid #d0d7de; padding: 8px 12px; text-align: left;
    vertical-align: top; }}
  th {{ background: #f6f8fa; font-weight: 600; }}
  ul, ol {{ padding-left: 22px; }}
  li {{ margin: 4px 0; }}
  blockquote {{ margin: 12px 0; padding: 0 12px; color: #57606a;
    border-left: 4px solid #d0d7de; }}
  .top-nav {{ display: flex; justify-content: space-between;
    align-items: center; margin-bottom: 24px; padding-bottom: 12px;
    border-bottom: 1px solid #d0d7de; }}
  .top-nav a {{ font-weight: 600; }}
  .top-nav .logo {{ color: #57606a; font-weight: 600; }}
  .back-link {{ display: inline-block; margin-top: 40px; padding: 8px 14px;
    background: #ffffff; border: 1px solid #d0d7de; border-radius: 6px;
    font-weight: 600; }}
</style>
</head>
<body>
<nav class="top-nav">
  <span class="logo">DITA Parity Assistant</span>
  <a href="/">← Back to the upload page</a>
</nav>
{body}
<a class="back-link" href="/">← Back to the upload page</a>
</body></html>
"""


# --- Entry point --------------------------------------------------------- #

def main() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with socketserver.TCPServer((HOST, PORT), Handler) as httpd:
        print(f"DITA Parity Assistant — http://{HOST}:{PORT}/", flush=True)
        print("Ctrl+C to stop.", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")


if __name__ == "__main__":
    main()
