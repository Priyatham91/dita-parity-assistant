# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the DITA Parity Assistant.

Build with:
    pyinstaller DitaParityAssistant.spec --clean --noconfirm

Output: dist/DitaParityAssistant.exe — a single-file Windows binary
that writers can double-click to launch the local server. Reports
will be written next to the .exe under output/runs/.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


# lxml powers the Schematron validation. Bundle its data files and
# pull in the submodules PyInstaller can't always trace via imports
# (notably lxml.isoschematron's XSLT resources).
lxml_datas = collect_data_files("lxml")
lxml_submodules = collect_submodules("lxml")

# isoschematron loads .xsl stylesheets at runtime; collect_data_files
# normally catches these but make it explicit so the build is stable.
isoschematron_datas = collect_data_files("lxml.isoschematron")


a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=[
        # Schematron rule file used by the validator at request time.
        ("schematron", "schematron"),
        # The writer guide, served at /guide inside the running tool.
        ("WRITER_GUIDE.md", "."),
    ] + lxml_datas + isoschematron_datas,
    hiddenimports=[
        "lxml.etree",
        "lxml._elementpath",
        "lxml.isoschematron",
        # python-markdown (with the tables and fenced_code extensions
        # used by the guide renderer). PyInstaller's analyzer doesn't
        # always walk lazy-loaded extension modules, so make them
        # explicit.
        "markdown",
        "markdown.extensions.tables",
        "markdown.extensions.fenced_code",
    ] + lxml_submodules,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim non-runtime dependencies. The information_model PDF
        # extracts and the test suite have no runtime use.
        "pytest",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="DitaParityAssistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX compression triggers some EDR heuristics; skip it
    upx_exclude=[],
    runtime_tmpdir=None,
    # Keep the console visible: writers see the URL and any error
    # messages. Hiding it would mean a silent crash if the port
    # is taken or imports fail.
    console=True,
    disable_windowed_traceback=False,
    icon=None,
)
