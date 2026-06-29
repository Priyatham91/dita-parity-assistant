"""Launcher for the DITA Parity Assistant.

Runs in two modes:

- Development: just `python launcher.py` — starts the server and opens
  the browser. Useful for debugging.
- Bundled `.exe`: PyInstaller wraps this script. Writers double-click
  the `.exe`, a console window appears with the URL, the browser
  opens automatically, and reports get written next to the `.exe`.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _setup_paths() -> tuple[Path, Path]:
    """Return (bundle_root, output_root).

    - bundle_root: directory containing the app package and
      bundled assets (schematron rules). In dev mode this is the
      folder containing launcher.py; in bundled mode it's the
      PyInstaller temp extraction dir (`sys._MEIPASS`).
    - output_root: where `output/runs/<timestamp>/` should land.
      In dev mode this matches bundle_root. In bundled mode it's
      the folder containing the `.exe` — so writers find their
      reports alongside the program they ran.
    """
    if getattr(sys, "frozen", False):
        bundle_root = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        output_root = Path(sys.executable).resolve().parent
    else:
        bundle_root = Path(__file__).resolve().parent
        output_root = bundle_root
    return bundle_root, output_root


def _open_browser_after_delay(seconds: float = 1.5) -> None:
    time.sleep(seconds)
    try:
        webbrowser.open("http://localhost:8000/")
    except Exception:
        # Browser autoload is best-effort — the URL is printed
        # to the console either way.
        pass


def main() -> int:
    bundle_root, output_root = _setup_paths()
    sys.path.insert(0, str(bundle_root))

    # Import after sys.path is set up. Patch the server's path
    # constants so reports go next to the .exe instead of inside
    # the bundle's temp extraction (which is deleted on exit).
    from app import server  # noqa: E402

    server.PROJECT_ROOT = bundle_root
    server.RUNS_DIR = output_root / "output" / "runs"
    server.RUNS_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 64)
    print("  DITA Parity Assistant")
    print("=" * 64)
    print(f"  Reports will be saved under:")
    print(f"    {server.RUNS_DIR}")
    print()
    print(f"  Your browser should open automatically in a moment.")
    print(f"  If it doesn't, open this URL manually:")
    print(f"    http://localhost:8000/")
    print()
    print(f"  Keep this window open while you use the tool.")
    print(f"  Close it (or press Ctrl+C) to stop the server.")
    print("=" * 64)
    print()

    threading.Thread(target=_open_browser_after_delay, daemon=True).start()

    try:
        server.main()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    except OSError as exc:
        # The usual cause: port 8000 already in use (another copy
        # of the .exe is already running, or some other tool is
        # listening). Tell the writer clearly.
        if "10048" in str(exc) or "address already in use" in str(exc).lower():
            print()
            print("=" * 64)
            print("  Could not start the server.")
            print(
                "  Port 8000 is already in use — probably another copy "
                "of the assistant is already running."
            )
            print("  Close the other window, then try again.")
            print("=" * 64)
            input("\nPress Enter to close this window. ")
            return 1
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
