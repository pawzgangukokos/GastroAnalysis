# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ctk_datas = collect_data_files("customtkinter")
hiddenimports = [
    "customtkinter",
    "matplotlib.backends.backend_tkagg",
    "openpyxl",
    "pandas",
    "numpy",
    "pypdf",
    "pdfplumber",
    "fitz",
    "brand_service",
]
hiddenimports += collect_submodules("matplotlib")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=ctk_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GastroAnalytics",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="GastroAnalytics",
)

app = BUNDLE(
    coll,
    name="GastroAnalytics.app",
    icon=None,
    bundle_identifier="com.gastroanalytics.app",
)
