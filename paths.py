"""Where the app keeps its files, both from source and as a PyInstaller EXE.

APP_DIR: user data (config.json, data.json, data.xlsx, browser_profile/, .env, debug/, app.log).
         From source: the project folder, exactly as before.
         As the EXE:  %LOCALAPPDATA%\\MaBaLi (or MABALI_HOME), so settings, the login session
         and collected data survive restarts and upgrades (replacing the EXE folder).
RES_DIR: read-only bundled resources (index.html).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
SOURCE_DIR = Path(__file__).resolve().parent

if FROZEN:
    APP_DIR = Path(os.getenv("MABALI_HOME")
                   or Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "MaBaLi")
    APP_DIR.mkdir(parents=True, exist_ok=True)
else:
    APP_DIR = SOURCE_DIR
RES_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
