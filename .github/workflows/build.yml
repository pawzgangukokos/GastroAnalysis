name: Build GastroAnalytics

on:
  push:
    branches: [ main, master ]
  workflow_dispatch:

jobs:
  build:
    strategy:
      fail-fast: false
      matrix:
        include:
          - os: macos-latest
            artifact_name: GastroAnalytics-macOS-ARM
            windowed_flag: --windowed
          - os: windows-latest
            artifact_name: GastroAnalytics-Windows
            windowed_flag: --noconsole

    runs-on: ${{ matrix.os }}

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          python -m pip install pyinstaller

      - name: Build binary with PyInstaller
        run: |
          pyinstaller --noconfirm --clean \
            --name GastroAnalytics \
            --onefile \
            ${{ matrix.windowed_flag }} \
            --paths . \
            --hidden-import brand_service \
            --hidden-import analytics \
            --hidden-import importer \
            --hidden-import database \
            --hidden-import payout_analytics \
            --hidden-import payout_parser \
            --collect-all customtkinter \
            --collect-all matplotlib \
            main.py

      - name: Package macOS app (if macOS)
        if: runner.os == 'macOS'
        shell: bash
        run: |
          cd dist
          zip -r GastroAnalytics-macOS-ARM.zip GastroAnalytics.app

      - name: Upload Windows artifact
        if: runner.os == 'Windows'
        uses: actions/upload-artifact@v4
        with:
          name: ${{ matrix.artifact_name }}
          path: dist/GastroAnalytics.exe

      - name: Upload macOS artifact
        if: runner.os == 'macOS'
        uses: actions/upload-artifact@v4
        with:
          name: ${{ matrix.artifact_name }}
          path: dist/GastroAnalytics-macOS-ARM.zip
